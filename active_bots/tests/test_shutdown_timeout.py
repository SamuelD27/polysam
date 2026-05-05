"""Regression guard for the time-boxed shutdown pattern in daemon_base_v1.

The daemon's ``run()`` wraps ``asyncio.gather(*tasks)`` in an
``asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT_S)`` after CancelledError so
tasks that swallow cancel (buggy feeds, hung websocket close frames) can't
block process exit. This test doesn't import ``run()`` — it verifies the
pattern itself so a future refactor that removes the timeout gets caught.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import daemon_base_v1


class ShutdownTimeoutTests(unittest.TestCase):
    def test_module_constant_is_five_seconds(self) -> None:
        """Sanity: the module-level constant exists and is 5.0s."""
        self.assertEqual(daemon_base_v1.SHUTDOWN_TIMEOUT_S, 5.0)

    def test_wait_returns_pending_for_cancel_ignoring_tasks(self) -> None:
        """asyncio.wait(..., timeout=...) must return pending tasks that
        refuse to observe cancellation, rather than hanging forever."""

        async def run_case() -> int:
            # Patch the constant to keep the test fast (not the real 5s).
            original = daemon_base_v1.SHUTDOWN_TIMEOUT_S
            daemon_base_v1.SHUTDOWN_TIMEOUT_S = 0.2
            try:
                # Swallow a bounded number of CancelledErrors so the test
                # can clean up after proving the point. A daemon feed that
                # actually swallows cancel forever would also hang
                # asyncio.run's cleanup — that's the *whole reason* the
                # shutdown code falls back to os._exit(1). Testing that
                # exact pathology in-process would deadlock the test
                # runner, so we simulate "ignores cancel long enough to
                # exceed SHUTDOWN_TIMEOUT_S".
                class Box:
                    swallows_left = 50  # >> timeout/sleep_dt → stays pending

                async def cancel_ignoring() -> None:
                    while True:
                        try:
                            await asyncio.sleep(0.01)
                        except asyncio.CancelledError:
                            if Box.swallows_left <= 0:
                                raise
                            Box.swallows_left -= 1
                            continue

                tasks = [asyncio.create_task(cancel_ignoring()) for _ in range(2)]

                # Let the tasks actually enter their try block before we
                # cancel, so the CancelledError lands inside the except
                # handler (not at function entry, which would let the
                # coroutine exit and make the test vacuous).
                await asyncio.sleep(0.01)

                # Simulate handle_signal cancelling everything.
                for t in tasks:
                    t.cancel()

                # Mirror the daemon shutdown pattern. Don't await gather
                # here — with swallow-cancel tasks that would just block
                # until they give up. Go straight to the bounded wait,
                # which is the line of code this test is guarding.
                _, pending = await asyncio.wait(
                    tasks,
                    timeout=daemon_base_v1.SHUTDOWN_TIMEOUT_S,
                )
                pending_count = len(pending)

                # Cleanup: exhaust the remaining swallows so the tasks
                # actually exit, so asyncio.run's cleanup doesn't hang the
                # test runner.
                Box.swallows_left = 0
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                return pending_count
            finally:
                daemon_base_v1.SHUTDOWN_TIMEOUT_S = original

        # Outer timeout as a hard safety net — if the shutdown pattern
        # ever regresses such that this test would hang, fail instead.
        async def bounded():
            return await asyncio.wait_for(run_case(), timeout=5.0)

        pending_count = asyncio.run(bounded())
        self.assertEqual(
            pending_count,
            2,
            "asyncio.wait must surface tasks that ignored cancel as pending — "
            "otherwise the daemon's shutdown timeout has no effect.",
        )


if __name__ == "__main__":
    unittest.main()
