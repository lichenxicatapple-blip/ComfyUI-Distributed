#!/usr/bin/env bash
set -euo pipefail

SSH_ARGS=("$@")

if [ ${#SSH_ARGS[@]} -eq 0 ]; then
    echo "Usage: $0 <ssh-args>"
    echo "Example: $0 root@213.173.102.212 -p 16957 -i ~/.ssh/id_ed25519"
    exit 1
fi

REPO_URL="https://github.com/if-ai/ComfyUI-Distributed.git"
REPO_BRANCH="feat/cat"
PLUGIN_DIR_NAME="ComfyUI-Distributed"

run_remote() {
    ssh "${SSH_ARGS[@]}" "$1"
}

log() {
    echo "==> $1"
}

# Step 1: Test SSH connection
log "Testing SSH connection..."
if ! run_remote "echo ok" >/dev/null 2>&1; then
    echo "ERROR: SSH connection failed. Check your arguments: ${SSH_ARGS[*]}"
    exit 1
fi
log "SSH connection OK."

# Step 2: Find ComfyUI directory
log "Searching for ComfyUI installation under /workspace..."
COMFYUI_DIR=$(run_remote "find /workspace -maxdepth 4 -name main.py -path '*/ComfyUI/*' 2>/dev/null | head -1 | xargs -r dirname")

if [ -z "$COMFYUI_DIR" ]; then
    echo "ERROR: ComfyUI not found under /workspace."
    exit 1
fi

MATCH_COUNT=$(run_remote "find /workspace -maxdepth 4 -name main.py -path '*/ComfyUI/*' 2>/dev/null | wc -l" | tr -d ' ')
if [ "$MATCH_COUNT" -gt 1 ]; then
    echo "WARNING: Found $MATCH_COUNT ComfyUI installations, using: $COMFYUI_DIR"
fi

log "Found ComfyUI at: $COMFYUI_DIR"

CUSTOM_NODES_DIR="$COMFYUI_DIR/custom_nodes"
PLUGIN_PATH="$CUSTOM_NODES_DIR/$PLUGIN_DIR_NAME"

# Step 3: Install or update plugin
log "Installing/updating $PLUGIN_DIR_NAME..."
PLUGIN_EXISTS=$(run_remote "[ -d '$PLUGIN_PATH/.git' ] && echo yes || echo no")

if [ "$PLUGIN_EXISTS" = "yes" ]; then
    log "Plugin exists, pulling latest changes..."
    run_remote "cd '$PLUGIN_PATH' && git checkout -- . && git clean -fd && git fetch origin && git checkout $REPO_BRANCH && git pull origin $REPO_BRANCH"
else
    log "Cloning plugin..."
    run_remote "cd '$CUSTOM_NODES_DIR' && git clone -b $REPO_BRANCH $REPO_URL"
fi

# Step 4: Install dependencies
log "Installing Python dependencies..."
VENV_ACTIVATE=$(run_remote "find '$COMFYUI_DIR' -maxdepth 3 -path '*/bin/activate' -name activate 2>/dev/null | head -1")

PIP_CMD=""
if [ -n "$VENV_ACTIVATE" ]; then
    log "Found venv: $VENV_ACTIVATE"
    PIP_CMD="source '$VENV_ACTIVATE' && pip"
else
    log "No venv found, using system pip."
    PIP_CMD="pip3"
fi

HAS_REQUIREMENTS=$(run_remote "[ -f '$PLUGIN_PATH/requirements.txt' ] && echo yes || echo no")
if [ "$HAS_REQUIREMENTS" = "yes" ]; then
    run_remote "$PIP_CMD install -r '$PLUGIN_PATH/requirements.txt'"
else
    log "No requirements.txt found, skipping."
fi

# Step 5: Configure CORS
log "Configuring CORS..."
ARGS_FILE="$COMFYUI_DIR/comfyui_args.txt"
HAS_ARGS_FILE=$(run_remote "[ -f '$ARGS_FILE' ] && echo yes || echo no")

if [ "$HAS_ARGS_FILE" = "yes" ]; then
    CORS_ALREADY=$(run_remote "grep -q -- '--enable-cors-header' '$ARGS_FILE' && echo yes || echo no")
    if [ "$CORS_ALREADY" = "yes" ]; then
        log "CORS header already configured."
    else
        run_remote "echo '--enable-cors-header' >> '$ARGS_FILE'"
        log "Added --enable-cors-header to $ARGS_FILE."
    fi
else
    log "No comfyui_args.txt found at $ARGS_FILE, skipping CORS config."
    log "You may need to manually add --enable-cors-header to the startup command."
fi

# Step 6: Configure extra model paths
# RunPod templates often have models in a separate ComfyUI installation
log "Checking for external model directories..."
EXTRA_YAML="$COMFYUI_DIR/extra_model_paths.yaml"

HAS_LOCAL_CKPTS=$(run_remote "find '$COMFYUI_DIR/models/checkpoints' -type f -name '*.safetensors' -o -name '*.ckpt' 2>/dev/null | head -1")

if [ -z "$HAS_LOCAL_CKPTS" ]; then
    EXTERNAL_MODELS_DIR=$(run_remote "find /workspace -maxdepth 4 -type d -name models -path '*/ComfyUI/*' 2>/dev/null | while read d; do
        [ \"\$d\" = '$COMFYUI_DIR/models' ] && continue
        find \"\$d/checkpoints\" -type f \\( -name '*.safetensors' -o -name '*.ckpt' \\) 2>/dev/null | head -1 | grep -q . && echo \"\$d\" && break
    done")

    if [ -n "$EXTERNAL_MODELS_DIR" ]; then
        log "Local checkpoints empty, found models at: $EXTERNAL_MODELS_DIR"
        YAML_EXISTS=$(run_remote "[ -f '$EXTRA_YAML' ] && echo yes || echo no")
        ALREADY_CONFIGURED="no"
        if [ "$YAML_EXISTS" = "yes" ]; then
            ALREADY_CONFIGURED=$(run_remote "grep -q '$EXTERNAL_MODELS_DIR' '$EXTRA_YAML' && echo yes || echo no")
        fi

        if [ "$ALREADY_CONFIGURED" = "yes" ]; then
            log "extra_model_paths.yaml already points to $EXTERNAL_MODELS_DIR."
        else
            SUBDIRS=$(run_remote "ls -1 '$EXTERNAL_MODELS_DIR'" | tr '\n' ' ')
            log "Writing extra_model_paths.yaml with subdirs: $SUBDIRS"
            YAML_CONTENT="shared_models:\n    base_path: $EXTERNAL_MODELS_DIR/"
            for subdir in $SUBDIRS; do
                YAML_CONTENT="$YAML_CONTENT\n    $subdir: $subdir"
            done
            run_remote "printf '$YAML_CONTENT\n' > '$EXTRA_YAML'"
            log "Created $EXTRA_YAML."
        fi
    else
        log "No external model directories found."
    fi
else
    log "Local checkpoints exist, no extra model paths needed."
fi

# Step 7: Restart ComfyUI
log "Restarting ComfyUI..."

LOG_FILE="/tmp/comfyui-restart.log"

run_remote "PID=\$(ps aux | grep '[m]ain.py.*--listen' | awk '{print \$2}'); [ -n \"\$PID\" ] && kill \$PID && echo \"Killed PID: \$PID\" || echo 'No ComfyUI process found'"
sleep 2

# Activate venv if available, then start ComfyUI in background
START_CMD="cd '$COMFYUI_DIR'"
if [ -n "$VENV_ACTIVATE" ]; then
    START_CMD="$START_CMD && source '$VENV_ACTIVATE'"
fi
START_CMD="$START_CMD && nohup python main.py --listen 0.0.0.0 --enable-cors-header > '$LOG_FILE' 2>&1 &"

ssh -f "${SSH_ARGS[@]}" "$START_CMD"
log "ComfyUI starting..."

# Step 8: Verify startup
log "Waiting for ComfyUI to start..."
sleep 8

PROCESS_ALIVE=$(run_remote "ps aux | grep '[m]ain.py.*--listen' | grep -q . && echo yes || echo no")
if [ "$PROCESS_ALIVE" != "yes" ]; then
    echo "ERROR: ComfyUI process not found after restart."
    echo "Last log output:"
    run_remote "tail -20 '$LOG_FILE' 2>/dev/null || echo 'No log file found.'"
    exit 1
fi

SERVER_STARTED=$(run_remote "grep -q 'Starting server' '$LOG_FILE' 2>/dev/null && echo yes || echo no")
if [ "$SERVER_STARTED" = "yes" ]; then
    log "ComfyUI server started successfully."
else
    log "Process is running but 'Starting server' not yet seen in logs. It may still be loading models."
    log "Recent log output:"
    run_remote "tail -10 '$LOG_FILE' 2>/dev/null || echo 'No log file found.'"
fi

log "Deploy complete."
