import asyncio
import aiohttp
import server

from .utils.logging import debug_log, log
from .utils.network import get_server_loop

FORWARDED_EVENTS = {"progress_state", "execution_error"}

_original_send_sync = None
_master_url = None
_session = None
_forward_count = 0


def activate(master_url):
    global _original_send_sync, _master_url, _session
    if _original_send_sync is not None:
        debug_log("Progress reporter already active, skipping activation")
        return

    prompt_server = server.PromptServer.instance
    _original_send_sync = prompt_server.send_sync
    _master_url = master_url.rstrip("/")

    def patched_send_sync(event, data, sid=None):
        _original_send_sync(event, data, sid)
        if event in FORWARDED_EVENTS:
            _fire_and_forget(event, data)

    prompt_server.send_sync = patched_send_sync
    log(f"Progress reporter activated, forwarding to {_master_url}")


def _fire_and_forget(event, data):
    try:
        loop = get_server_loop()
    except Exception:
        return
    if loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_post_event(event, data), loop)


async def _post_event(event, data):
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    url = f"{_master_url}/distributed/worker_progress"
    payload = {"event": event, "data": data}
    try:
        async with _session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            global _forward_count
            _forward_count += 1
            if resp.status != 200:
                log(f"Progress forward failed: {resp.status}")
            elif _forward_count == 1:
                log(f"Progress forward OK (first event: {event})")
    except Exception as e:
        log(f"Progress forward error: {e}")


async def _close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def deactivate():
    global _original_send_sync, _master_url, _forward_count
    if _original_send_sync is None:
        return

    prompt_server = server.PromptServer.instance
    prompt_server.send_sync = _original_send_sync
    _original_send_sync = None
    _master_url = None

    try:
        loop = get_server_loop()
        if not loop.is_closed():
            asyncio.run_coroutine_threadsafe(_close_session(), loop)
    except Exception:
        pass

    log(f"Progress reporter deactivated, forwarded {_forward_count} events")
    _forward_count = 0
