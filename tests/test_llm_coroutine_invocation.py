# tests/test_llm_coroutine_invocation.py
"""Regression tests for Ticket 4 — Fix LLM Coroutine Invocation Bug."""
import pytest
import warnings
import asyncio
from unittest.mock import patch, MagicMock

from outreach_manager.core.llm import run_agent_sync, LLMFailure


class TestLLMCoroutineInvocation:

    def test_sync_callable_wrapper(self):
        """Verify synchronous callables execute cleanly via run_agent_sync."""
        res = run_agent_sync(lambda: "hello_sync")
        assert res == "hello_sync"

    def test_async_callable_execution(self):
        """Verify async coroutine factories execute successfully."""
        async def sample_coro():
            await asyncio.sleep(0.01)
            return "hello_async"

        res = run_agent_sync(lambda: sample_coro())
        assert res == "hello_async"

    def test_unsupported_coroutine_object_raises_clear_type_error(self):
        """Passing a coroutine object directly (instead of a callable) raises a clear TypeError and closes coroutine."""
        async def dummy_coro():
            return "dummy"

        coro_obj = dummy_coro()

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            with pytest.raises(TypeError) as exc_info:
                run_agent_sync(coro_obj)

            assert "run_agent_sync expected a zero-argument callable" in str(exc_info.value)
            assert "coroutine" in str(exc_info.value)

            # Ensure no 'coroutine was never awaited' warning was emitted
            coro_warnings = [
                w for w in recorded_warnings
                if issubclass(w.category, RuntimeWarning) and "coroutine" in str(w.message)
            ]
            assert len(coro_warnings) == 0

    def test_unsupported_non_callable_type_raises_clear_type_error(self):
        """Passing a non-callable integer/string raises TypeError immediately."""
        with pytest.raises(TypeError) as exc_info:
            run_agent_sync(12345)
        assert "run_agent_sync expected a zero-argument callable" in str(exc_info.value)
        assert "int" in str(exc_info.value)

    def test_retry_instantiates_fresh_coroutine_per_attempt(self):
        """Verify retries invoke the callable factory multiple times to get fresh coroutines."""
        attempts = 0

        async def failing_then_succeeding():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise IOError("Transient network glitch")
            return "retry_success"

        with patch("outreach_manager.core.llm._pace"):
            res = run_agent_sync(lambda: failing_then_succeeding())

        assert res == "retry_success"
        assert attempts == 2

    def test_structured_output_agent_invocation(self):
        """Verify structured output agent runs and returns output object."""
        mock_result = MagicMock(output=MagicMock(message="structured_result"))

        async def mock_agent_run():
            return mock_result

        res = run_agent_sync(lambda: mock_agent_run(), structured_output=True)
        assert res.output.message == "structured_result"
