import os
import sys

# Add the directory to Python path
sys.path.append(os.path.dirname(__file__))

# Patch ComfyUI execution validation and progress forwarding
try:
    import execution
    import server
    from .distributed import ImageBatchDivider
    from . import worker_progress_reporter

    if hasattr(execution, 'validate_outputs'):
        original_validate_outputs = execution.validate_outputs

        def patched_validate_outputs(executor, node_id, result, node_class):
            if node_class == ImageBatchDivider:
                return
            return original_validate_outputs(executor, node_id, result, node_class)

        execution.validate_outputs = patched_validate_outputs

    if hasattr(execution.PromptExecutor, 'execute'):
        original_execute = execution.PromptExecutor.execute

        def patched_execute(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
            ps = server.PromptServer.instance
            ps._distributed_active_prompt_id = prompt_id

            master_url = None
            for node_data in prompt.values():
                inputs = node_data.get("inputs", {})
                if inputs.get("enable_progress_forwarding"):
                    master_url = inputs.get("master_url")
                    break

            if master_url:
                worker_progress_reporter.activate(master_url)

            try:
                return original_execute(self, prompt, prompt_id, extra_data, execute_outputs)
            finally:
                ps._distributed_active_prompt_id = None
                if master_url:
                    worker_progress_reporter.deactivate()

        execution.PromptExecutor.execute = patched_execute

except ImportError:
    pass

# Import everything needed from the main module
from .distributed import (
    NODE_CLASS_MAPPINGS as DISTRIBUTED_CLASS_MAPPINGS, 
    NODE_DISPLAY_NAME_MAPPINGS as DISTRIBUTED_DISPLAY_NAME_MAPPINGS
)

# Import utilities
from .utils.config import ensure_config_exists, CONFIG_FILE
from .utils.logging import debug_log

# Import distributed upscale nodes
from .distributed_upscale import (
    NODE_CLASS_MAPPINGS as UPSCALE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as UPSCALE_DISPLAY_NAME_MAPPINGS
)

WEB_DIRECTORY = "./web"

ensure_config_exists()

# Merge node mappings
NODE_CLASS_MAPPINGS = {**DISTRIBUTED_CLASS_MAPPINGS, **UPSCALE_CLASS_MAPPINGS}
NODE_DISPLAY_NAME_MAPPINGS = {**DISTRIBUTED_DISPLAY_NAME_MAPPINGS, **UPSCALE_DISPLAY_NAME_MAPPINGS}

__all__ = ['NODE_CLASS_MAPPINGS', 'NODE_DISPLAY_NAME_MAPPINGS']

debug_log("Loaded Distributed nodes.")
debug_log(f"Config file: {CONFIG_FILE}")
debug_log(f"Available nodes: {list(NODE_CLASS_MAPPINGS.keys())}")
