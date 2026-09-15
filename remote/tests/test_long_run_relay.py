"""Opt-in long-run test of the relay path (``pytest -m slow -q``).

Fake vLLM, the real relay and the real agent run in one event loop (see
``test_end_to_end.Stack``); four threads push 200 streaming requests through
``core.remote_service.RemoteService`` while a controller thread drops the
agent's relay socket twice, mid-run and without a close frame, as a network
outage would. Every request must either succeed or fail with
``RemoteUnavailable`` within the client timeout (never hang), every request
must succeed after retries once the agent has reconnected, and the relay's
``/status`` must show nothing in flight at the end.

The file is not called ``test_long_run.py`` because ``tests/test_long_run.py``
already uses that module name (the test directories are not packages).
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.remote_service import RemoteUnavailable
from test_end_to_end import Stack

REQUESTS = 200
THREADS = 4
DROPS_AFTER = (40, 120)  # completed requests after which the agent socket is dropped
CLIENT_TIMEOUT = 10  # RemoteService socket timeout; no attempt may take longer than this plus a margin
MAX_ATTEMPTS = 30
RETRY_PAUSE = 0.5


async def drop_agent_socket(agent):
    """Abort the agent's relay socket like a pulled cable (no WebSocket close frame)."""
    ws = agent.ws
    if ws is None or ws.closed:
        return False
    connection = getattr(ws, "_conn", None)
    transport = getattr(connection, "transport", None) if connection is not None else None
    if transport is not None:
        transport.abort()
    else:  # older aiohttp without the attribute: a hard close is the next best thing
        await ws.close(code=1006, message=b"dropped")
    return True


def _wait_until(predicate, timeout, what):
    deadline = time.time() + timeout
    while not predicate():
        assert time.time() < deadline, "timed out waiting for " + what
        time.sleep(0.02)


def controller(loop, stack, progress):
    """Drop the agent's socket after ``DROPS_AFTER`` completed requests and wait for the reconnect."""
    for target in DROPS_AFTER:
        _wait_until(lambda: progress["done"] >= target, 120, "{0} completed requests".format(target))
        dropped = asyncio.run_coroutine_threadsafe(drop_agent_socket(stack.agent), loop).result(timeout=5)
        assert dropped, "the agent had no socket to drop"
        progress["drops"] += 1
        _wait_until(lambda: not stack.agent.relay_connected, 10, "the agent to notice the drop")
        _wait_until(lambda: stack.agent.relay_connected, 30, "the agent to reconnect")
        progress["reconnects"] += 1


@pytest.mark.slow
def test_relay_path_recovers_from_agent_socket_drops():
    async def scenario():
        async with Stack() as stack:
            stack.vllm.chunk_delay = 0.03  # keep a few requests in flight at any time
            loop = asyncio.get_running_loop()
            service = stack.client(timeout=CLIENT_TIMEOUT)
            lock = threading.Lock()
            progress = {"done": 0, "drops": 0, "reconnects": 0}
            attempts_log = []  # (request, attempt, seconds, error message or None)

            def one_request(index):
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    started = time.time()
                    try:
                        text = service.stream_edit(
                            "qwen-27b", "Fix grammar.", "Orignal text {0}.".format(index), threading.Event()
                        )
                        error = None
                    except RemoteUnavailable as exc:
                        error = str(exc) or exc.__class__.__name__
                    with lock:
                        attempts_log.append((index, attempt, time.time() - started, error))
                    if error is None:
                        assert text == "Edited text."
                        with lock:
                            progress["done"] += 1
                        return attempt
                    time.sleep(RETRY_PAUSE)
                raise AssertionError("request {0} still failing after {1} attempts".format(index, MAX_ATTEMPTS))

            def run_all():
                with ThreadPoolExecutor(THREADS) as pool:
                    return list(pool.map(one_request, range(REQUESTS)))

            watcher = threading.Thread(target=controller, args=(loop, stack, progress), name="teai-drop", daemon=True)
            watcher.start()
            attempts = await loop.run_in_executor(None, run_all)
            await loop.run_in_executor(None, watcher.join, 30)
            assert not watcher.is_alive(), "the controller thread did not finish"

            failures = [entry for entry in attempts_log if entry[3] is not None]
            slowest = max(entry[2] for entry in attempts_log)
            assert len(attempts) == REQUESTS and progress["done"] == REQUESTS
            assert progress["drops"] == len(DROPS_AFTER) and progress["reconnects"] == len(DROPS_AFTER)
            assert failures, "no request noticed the socket drops"
            assert slowest < CLIENT_TIMEOUT + 5, "an attempt hung for {0:.1f}s".format(slowest)
            assert sum(attempts) == len(attempts_log)

            for _ in range(100):  # the last CANCELLED/ERROR frames settle
                status = await stack.in_thread(service.fetch_status)
                if status["agents"] and all(a["in_flight"] == 0 and a["queued"] == 0 for a in status["agents"]):
                    break
                await asyncio.sleep(0.05)
            assert len(status["agents"]) == 1
            assert status["agents"][0]["state"] == "ready"
            assert status["agents"][0]["in_flight"] == 0 and status["agents"][0]["queued"] == 0
            assert status["relay"]["requests_total"] == len(attempts_log)
            assert status["relay"]["requests_failed"] == len(failures)
            assert stack.agent.in_flight == {}
            print("\n{0} requests in {1} attempts, {2} failed during {3} drops, slowest attempt {4:.2f}s, "
                  "agent reconnects {5}".format(REQUESTS, len(attempts_log), len(failures), progress["drops"],
                                                slowest, progress["reconnects"]))

    asyncio.run(scenario())
