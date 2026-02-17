#!/usr/bin/env python3
"""Sync custom nodes: parse workflow, detect missing nodes, install from repos."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.error

NODE_MAP_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main"
    "/extension-node-map.json"
)
CACHE_TTL = 86400  # 24 hours


def extract_node_types(workflow_path):
    """Parse workflow JSON, return set of node type names.

    Supports both UI format (nodes[].type) and API format (values with class_type).
    """
    with open(workflow_path) as f:
        data = json.load(f)

    types = set()

    # UI format: top-level "nodes" list
    if isinstance(data.get("nodes"), list):
        for node in data["nodes"]:
            if "type" in node:
                types.add(node["type"])

    # API format: dict of id -> {class_type: ...}
    for key, value in data.items():
        if isinstance(value, dict) and "class_type" in value:
            types.add(value["class_type"])

    return types


def fetch_object_info(base_url):
    """Call /object_info on a ComfyUI instance, return set of installed node types."""
    url = f"{base_url}/object_info"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(f"Failed to reach {url}: {e}") from e
    return set(data.keys())


def fetch_node_map(cache_dir, no_cache=False):
    """Download or load cached extension-node-map.json, build reverse mapping.

    Returns dict: {node_type: git_url}
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "extension-node-map.json")

    use_cache = (
        not no_cache
        and os.path.exists(cache_path)
        and (time.time() - os.path.getmtime(cache_path)) < CACHE_TTL
    )

    if use_cache:
        with open(cache_path) as f:
            raw = json.load(f)
    else:
        print(f"Downloading node map from ComfyUI-Manager...")
        try:
            req = urllib.request.Request(NODE_MAP_URL)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_bytes = resp.read()
                raw = json.loads(raw_bytes.decode())
            with open(cache_path, "wb") as f:
                f.write(raw_bytes)
        except (urllib.error.URLError, OSError) as e:
            raise ConnectionError(
                f"Failed to download node map: {e}"
            ) from e

    # raw format: {git_url: [[node_type_strings], {metadata}]}
    # Build reverse: {node_type: git_url}
    # Multiple repos can claim the same node (forks, bundles, re-implementations).
    # We keep the first (alphabetical) and track conflicts for user review.
    reverse = {}
    conflicts = {}  # node_type -> [git_url, ...]
    for git_url in sorted(raw.keys()):
        entry = raw[git_url]
        if not isinstance(entry, list) or not entry:
            continue
        node_list = entry[0] if isinstance(entry[0], list) else entry
        for nt in node_list:
            if isinstance(nt, str):
                if nt in reverse:
                    conflicts.setdefault(nt, [reverse[nt]]).append(git_url)
                else:
                    reverse[nt] = git_url
    return reverse, conflicts


def resolve_missing_repos(missing_types, node_map, conflicts, dry_run=False):
    """Group missing types by repo URL.

    For nodes claimed by multiple repos, prompts the user to choose.
    In dry_run mode, conflicting nodes are skipped (not silently assigned).
    Returns (repos: {git_url: [node_types]}, unknown: [node_types],
             conflicting: [node_types])
    """
    repos = {}
    unknown = []
    conflicting = []
    for nt in sorted(missing_types):
        url = node_map.get(nt)
        if not url:
            unknown.append(nt)
            continue
        alts = conflicts.get(nt, [])
        if len(alts) > 1:
            if dry_run:
                conflicting.append(nt)
                continue
            url = _prompt_choose_repo(nt, alts)
            if url is None:
                continue
        repos.setdefault(url, []).append(nt)
    return repos, unknown, conflicting


def _prompt_choose_repo(node_type, candidates):
    """Ask user to pick the correct repo for a conflicting node type."""
    print(f"\n  Multiple repos provide '{node_type}':")
    for i, url in enumerate(candidates, 1):
        name = url.rstrip("/").split("/")[-1]
        print(f"    [{i}] {name}  ({url})")
    print(f"    [0] Skip this node")
    while True:
        try:
            choice = input(f"  Choose [1-{len(candidates)}, 0 to skip]: ").strip()
            n = int(choice)
            if n == 0:
                return None
            if 1 <= n <= len(candidates):
                return candidates[n - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Invalid choice, enter 0-{len(candidates)}")


def install_local(repos, custom_nodes_dir, pip_args, pip_env=None, dry_run=False):
    """Git clone repos into custom_nodes_dir, pip install requirements.

    Returns (installed: [repo_name], failed: [(repo_name, error)])
    """
    os.makedirs(custom_nodes_dir, exist_ok=True)
    installed = []
    failed = []

    for i, (git_url, node_types) in enumerate(repos.items(), 1):
        repo_name = git_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        dest = os.path.join(custom_nodes_dir, repo_name)

        status = f"  [{i}/{len(repos)}] {repo_name}"

        already_cloned = os.path.isdir(dest)

        if already_cloned and dry_run:
            print(f"{status} ... Already cloned, would install deps")
            continue

        if dry_run:
            print(f"{status} ... Would install")
            continue

        print(f"{status} ... ", end="", flush=True)
        try:
            if not already_cloned:
                subprocess.run(
                    ["git", "clone", "--depth=1", git_url, dest],
                    check=True, capture_output=True, text=True,
                )
            req_file = os.path.join(dest, "requirements.txt")
            if os.path.exists(req_file):
                subprocess.run(
                    pip_args + ["install", "-r", req_file],
                    check=True, capture_output=True, text=True,
                    env=pip_env,
                )
                verb = "pip OK" if already_cloned else "Cloned + pip OK"
                print(verb)
            else:
                if already_cloned:
                    print("Already cloned (no requirements.txt)")
                else:
                    print("Cloned (no requirements.txt)")
            installed.append(repo_name)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or e.stdout or str(e)).strip().split("\n")[-1]
            print(f"FAILED: {msg}")
            failed.append((repo_name, msg))

    return installed, failed


def _run_ssh(ssh_args, command):
    """Run a command on remote via SSH, return stdout."""
    result = subprocess.run(
        ["ssh"] + ssh_args + [command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"SSH command failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()}"
        )
    return result.stdout


def fetch_remote_object_info(ssh_args):
    """SSH to worker, curl local object_info, return set of installed node types."""
    try:
        raw = _run_ssh(ssh_args, "curl -s http://127.0.0.1:8188/object_info")
    except RuntimeError as e:
        raise ConnectionError(f"Failed to get remote object_info: {e}") from e
    data = json.loads(raw)
    return set(data.keys())


def _find_remote_comfyui(ssh_args):
    """Find ComfyUI dir on remote, return (comfyui_dir, pip_cmd, venv_activate)."""
    comfyui_dir = _run_ssh(
        ssh_args,
        "find /workspace -maxdepth 4 -name main.py -path '*/ComfyUI/*' "
        "2>/dev/null | head -1 | xargs -r dirname"
    ).strip()
    if not comfyui_dir:
        raise RuntimeError("ComfyUI not found under /workspace on remote")

    venv_activate = _run_ssh(
        ssh_args,
        f"find '{comfyui_dir}' -maxdepth 3 -path '*/bin/activate' "
        f"-name activate 2>/dev/null | head -1"
    ).strip()

    if venv_activate:
        pip_cmd = f"source '{venv_activate}' && pip"
    else:
        pip_cmd = "pip3"

    return comfyui_dir, pip_cmd, venv_activate


def install_remote(repos, ssh_args, dry_run=False):
    """SSH install repos on remote worker.

    Returns (installed: [repo_name], failed: [(repo_name, error)],
             remote_env: (comfyui_dir, venv_activate) for restart)
    """
    comfyui_dir, pip_cmd, venv_activate = _find_remote_comfyui(ssh_args)
    custom_nodes_dir = f"{comfyui_dir}/custom_nodes"
    print(f"  custom_nodes: {custom_nodes_dir}")
    print(f"  pip: {pip_cmd}")

    installed = []
    newly_installed = []
    failed = []

    for i, (git_url, node_types) in enumerate(repos.items(), 1):
        repo_name = git_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        dest = f"{custom_nodes_dir}/{repo_name}"

        status = f"  [{i}/{len(repos)}] {repo_name}"

        already_cloned = _run_ssh(
            ssh_args,
            f"[ -d '{dest}' ] && echo yes || echo no"
        ).strip() == "yes"

        if already_cloned and dry_run:
            print(f"{status} ... Already cloned, would install deps")
            continue

        if dry_run:
            print(f"{status} ... Would install")
            continue

        print(f"{status} ... ", end="", flush=True)
        try:
            if not already_cloned:
                _run_ssh(
                    ssh_args,
                    f"cd '{custom_nodes_dir}' && git clone --depth=1 '{git_url}'"
                )
            has_req = _run_ssh(
                ssh_args,
                f"[ -f '{dest}/requirements.txt' ] && echo yes || echo no"
            ).strip()
            if has_req == "yes":
                _run_ssh(
                    ssh_args,
                    f"{pip_cmd} install -r '{dest}/requirements.txt'"
                )
                verb = "pip OK" if already_cloned else "Cloned + pip OK"
                print(verb)
            else:
                if already_cloned:
                    print("Already installed (no requirements.txt)")
                else:
                    print("Cloned (no requirements.txt)")
            installed.append(repo_name)
            newly_installed.append(repo_name)
        except RuntimeError as e:
            msg = str(e).strip().split("\n")[-1]
            print(f"FAILED: {msg}")
            failed.append((repo_name, msg))

    return installed, failed, newly_installed, (comfyui_dir, venv_activate)


def _restart_remote_comfyui(ssh_args, comfyui_dir, venv_activate):
    """Kill and restart ComfyUI on the remote worker."""
    print("  Restarting remote ComfyUI...", end=" ", flush=True)
    try:
        _run_ssh(ssh_args,
            "PID=$(ps aux | grep '[m]ain.py.*--listen' | awk '{print $2}');"
            " [ -n \"$PID\" ] && kill $PID && echo \"Killed PID: $PID\""
            " || echo 'No ComfyUI process found'")
        time.sleep(2)

        # Build start command. Use ssh -f to background the SSH connection
        # so it doesn't hang waiting for the child process.
        start_cmd = f"cd '{comfyui_dir}'"
        if venv_activate:
            start_cmd += f" && source '{venv_activate}'"
        start_cmd += (
            " && nohup python main.py --listen 0.0.0.0 --enable-cors-header"
            " > /tmp/comfyui-restart.log 2>&1 &"
        )
        subprocess.run(
            ["ssh", "-f"] + ssh_args + [start_cmd],
            capture_output=True, text=True, timeout=15,
        )
        time.sleep(8)

        alive = _run_ssh(
            ssh_args,
            "ps aux | grep '[m]ain.py.*--listen' | grep -q . && echo yes || echo no"
        ).strip()
        if alive == "yes":
            print("OK")
        else:
            print("FAILED (process not found)")
            try:
                log = _run_ssh(ssh_args, "tail -10 /tmp/comfyui-restart.log 2>/dev/null")
                if log.strip():
                    print(f"  Last log:\n{log}")
            except RuntimeError:
                pass
    except RuntimeError as e:
        print(f"FAILED: {e}")


def print_missing_report(missing, node_map, conflicts):
    """Print table of missing node types with their repos.

    Warns when a node is claimed by multiple repos (mapping may be wrong).
    """
    for nt in sorted(missing):
        url = node_map.get(nt)
        if url:
            alts = conflicts.get(nt, [])
            if len(alts) > 1:
                print(f"  {nt:<30s} -> {url}")
                other = [u for u in alts if u != url]
                print(f"  {'':30s}    WARNING: also claimed by {', '.join(other)}")
            else:
                print(f"  {nt:<30s} -> {url}")
        else:
            search_url = f"https://www.google.com/search?q=comfyui+custom+node+%22{nt}%22"
            print(f"  {nt:<30s} -> [UNKNOWN] search: {search_url}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync custom nodes for a ComfyUI workflow"
    )
    parser.add_argument("workflow", help="Path to workflow JSON file")
    parser.add_argument(
        "--ssh", type=str, metavar="ARGS",
        help="SSH connection args as a single quoted string (e.g. 'root@host -p 22 -i key')"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be installed")
    parser.add_argument("--local-port", type=int, default=8188, help="Local ComfyUI port")
    parser.add_argument("--no-local", action="store_true", help="Skip local check")
    parser.add_argument(
        "--local-custom-nodes",
        help="Path to local custom_nodes directory (auto-detected from ComfyUI if not set)"
    )
    parser.add_argument("--no-cache", action="store_true", help="Force re-download node map")

    args = parser.parse_args()

    workflow_name = os.path.basename(args.workflow)
    print(f"=== Sync Nodes: {workflow_name} ===")

    # Step 1: Parse workflow
    node_types = extract_node_types(args.workflow)
    print(f"Workflow requires {len(node_types)} node types")

    # Step 2: Fetch node map
    cache_dir = os.path.join(os.path.dirname(__file__), ".cache")
    node_map, conflicts = fetch_node_map(cache_dir, no_cache=args.no_cache)

    local_summary = None
    remote_summary = None

    # Step 3-4: Local
    if not args.no_local:
        print(f"\n--- Local (http://127.0.0.1:{args.local_port}) ---")
        try:
            local_installed = fetch_object_info(f"http://127.0.0.1:{args.local_port}")
            local_missing = node_types - local_installed
            print(f"Missing: {len(local_missing)} node types")

            if local_missing:
                print_missing_report(local_missing, node_map, conflicts)
                repos, unknown, conflicting = resolve_missing_repos(
                    local_missing, node_map, conflicts, args.dry_run,
                )
                print()

                if repos:
                    if args.local_custom_nodes:
                        cn_dir = args.local_custom_nodes
                        pip_args = [sys.executable, "-m", "pip"]
                        pip_env = None
                    else:
                        cn_dir, pip_args, pip_env = _detect_local_env(args.local_port)
                    print(f"  custom_nodes: {cn_dir}")
                    print(f"  pip: {' '.join(pip_args)}")
                    ok, fail = install_local(
                        repos, cn_dir, pip_args, pip_env, dry_run=args.dry_run,
                    )
                    local_summary = {
                        "installed": len(ok), "failed": len(fail),
                        "unknown": len(unknown), "conflict": len(conflicting),
                    }
                else:
                    local_summary = {
                        "installed": 0, "failed": 0,
                        "unknown": len(unknown), "conflict": len(conflicting),
                    }
            else:
                print("  All node types are already installed.")
                local_summary = {"installed": 0, "failed": 0, "unknown": 0, "conflict": 0}
        except ConnectionError as e:
            print(f"  ERROR: {e}")

    # Step 5-6: Remote
    if args.ssh:
        ssh_args = shlex.split(args.ssh)
        if ssh_args and ssh_args[0] == "ssh":
            ssh_args = ssh_args[1:]
        print(f"\n--- Remote via SSH ({args.ssh}) ---")
        try:
            remote_installed = fetch_remote_object_info(ssh_args)
            remote_missing = node_types - remote_installed
            print(f"Missing: {len(remote_missing)} node types")

            if remote_missing:
                print_missing_report(remote_missing, node_map, conflicts)
                repos, unknown, conflicting = resolve_missing_repos(
                    remote_missing, node_map, conflicts, args.dry_run,
                )
                print()

                if repos:
                    ok, fail, newly, remote_env = install_remote(
                        repos, ssh_args, dry_run=args.dry_run,
                    )
                    if newly and not args.dry_run:
                        _restart_remote_comfyui(ssh_args, *remote_env)
                    remote_summary = {
                        "installed": len(ok), "failed": len(fail),
                        "unknown": len(unknown), "conflict": len(conflicting),
                    }
                else:
                    remote_summary = {
                        "installed": 0, "failed": 0,
                        "unknown": len(unknown), "conflict": len(conflicting),
                    }
            else:
                print("  All node types are already installed.")
                remote_summary = {"installed": 0, "failed": 0, "unknown": 0, "conflict": 0}
        except (ConnectionError, RuntimeError) as e:
            print(f"  ERROR: {e}")

    # Step 7: Summary
    print(f"\n=== Summary ===")
    for label, summary in [("Local", local_summary), ("Remote", remote_summary)]:
        if not summary:
            continue
        parts = []
        if summary["installed"]:
            parts.append(f"{summary['installed']} installed")
        if summary["failed"]:
            parts.append(f"{summary['failed']} failed")
        if summary["unknown"]:
            parts.append(f"{summary['unknown']} unknown")
        if summary["conflict"]:
            parts.append(f"{summary['conflict']} conflict (run without --dry-run to choose)")
        print(f"{label + ':':8s}{', '.join(parts) if parts else 'nothing to do'}")

    local_installed = local_summary and local_summary["installed"]
    if local_installed and not args.dry_run:
        print("Restart local ComfyUI for new nodes to take effect.")


def _detect_local_env(port):
    """Detect custom_nodes dir and pip config from the running ComfyUI process.

    Returns (custom_nodes_dir, pip_args, pip_env) where pip_env is a dict of
    extra environment variables (e.g. VIRTUAL_ENV) to pass to subprocess.
    """
    custom_nodes_dir = None
    pip_args = [sys.executable, "-m", "pip"]
    pip_env = None

    try:
        result = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True,
        )
        for line in result.stdout.strip().split("\n"):
            if "python" not in line or "main.py" not in line:
                continue
            parts = line.split()

            # Extract --base-directory (ComfyUI Desktop uses this)
            for i, p in enumerate(parts):
                if p == "--base-directory" and i + 1 < len(parts):
                    cn = os.path.join(parts[i + 1], "custom_nodes")
                    if os.path.isdir(cn):
                        custom_nodes_dir = cn
                    break

            # Extract python executable path for venv pip detection
            for p in parts:
                if "python" not in p or not os.path.isfile(p):
                    continue
                bin_dir = os.path.dirname(p)
                venv_uv = os.path.join(bin_dir, "uv")
                venv_pip = os.path.join(bin_dir, "pip")
                # Detect venv root (e.g. /path/to/.venv from /path/to/.venv/bin/python)
                venv_dir = os.path.dirname(bin_dir)
                if os.path.isfile(venv_uv):
                    pip_args = [venv_uv, "pip"]
                    pip_env = {**os.environ, "VIRTUAL_ENV": venv_dir}
                elif os.path.isfile(venv_pip):
                    pip_args = [venv_pip]
                else:
                    pip_args = [p, "-m", "pip"]
                break

            if custom_nodes_dir:
                break

            # Fallback: infer from main.py location
            for i, p in enumerate(parts):
                if p.endswith("main.py") and os.path.isabs(p):
                    cn = os.path.join(os.path.dirname(p), "custom_nodes")
                    if os.path.isdir(cn):
                        custom_nodes_dir = cn
                        break
    except FileNotFoundError:
        pass

    if not custom_nodes_dir:
        raise RuntimeError(
            "Cannot detect custom_nodes directory from running ComfyUI process. "
            "Use --local-custom-nodes to specify it explicitly."
        )

    return custom_nodes_dir, pip_args, pip_env


if __name__ == "__main__":
    main()
