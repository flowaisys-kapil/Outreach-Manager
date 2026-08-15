# openoutreach/core/llm/runner.py
"""Dedicated thread running a persistent asyncio event loop for LLM calls."""
from __future__ import annotations

import asyncio
from typing import Awaitable, TypeVar
import threading

_T = TypeVar("_T")


class _AgentRunner:
    """Owns one persistent asyncio loop on a dedicated daemon thread."""

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()
        threading.Thread(
            target=self._serve, args=(ready,), daemon=True, name="llm-runner",
        ).start()
        ready.wait()

    def _serve(self, ready: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        ready.set()
        self._loop.run_forever()

    def run(self, coro: Awaitable[_T] | _T) -> _T:
        """Submit *coro* to the runner loop; block until it completes."""
        if not asyncio.iscoroutine(coro) and not isinstance(coro, asyncio.Future):
            return coro  # type: ignore[return-value]
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()


_runner: _AgentRunner | None = None
_runner_lock = threading.Lock()


def _get_runner() -> _AgentRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = _AgentRunner()
    return _runner
