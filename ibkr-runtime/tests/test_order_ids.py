from __future__ import annotations

import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ibkr_runtime.order_ids import IbkrOrderIdAllocator, OrderIdNotReady


class IbkrOrderIdAllocatorTests(unittest.TestCase):
    def test_reconciliation_confirmation_requires_a_fresh_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            allocator = IbkrOrderIdAllocator(Path(directory) / "orders.sqlite")
            with self.assertRaises(OrderIdNotReady):
                allocator.confirm_reconciled()
            allocator.observe_next_valid_id(11)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            allocator.confirm_reconciled()
            self.assertEqual(allocator.allocate(), 11)
            allocator.close()

    def test_requires_fresh_next_valid_id_and_allocates_monotonically(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            allocator = IbkrOrderIdAllocator(path)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()

            allocator.observe_next_valid_id(100)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            allocator.confirm_reconciled()
            self.assertEqual(allocator.allocate(), 100)
            self.assertEqual(allocator.allocate(), 101)
            allocator.close()

            restarted = IbkrOrderIdAllocator(path)
            with self.assertRaises(OrderIdNotReady):
                restarted.allocate()
            restarted.observe_next_valid_id(102)
            restarted.confirm_reconciled()
            self.assertEqual(restarted.allocate(), 102)
            restarted.close()

    def test_rejects_stale_invalid_and_future_reconciliation_values(self) -> None:
        with TemporaryDirectory() as directory:
            allocator = IbkrOrderIdAllocator(Path(directory) / "orders.sqlite")
            allocator.observe_next_valid_id(20)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            allocator.confirm_reconciled()
            allocator.allocate()
            with self.assertRaises(OrderIdNotReady):
                allocator.observe_next_valid_id(20)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            for invalid in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    allocator.observe_next_valid_id(invalid)
            allocator.close()

    def test_stale_callback_invalidates_an_unconsumed_candidate(self) -> None:
        with TemporaryDirectory() as directory:
            allocator = IbkrOrderIdAllocator(Path(directory) / "orders.sqlite")
            allocator.observe_next_valid_id(21)
            with self.assertRaises(OrderIdNotReady):
                allocator.observe_next_valid_id(20)
            with self.assertRaises(OrderIdNotReady):
                allocator.confirm_reconciled()
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            allocator.observe_next_valid_id(21)
            allocator.confirm_reconciled()
            self.assertEqual(allocator.allocate(), 21)
            allocator.close()

    def test_only_one_local_process_can_own_the_same_database_path(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            first = IbkrOrderIdAllocator(path)
            with self.assertRaisesRegex(RuntimeError, "another local execution process"):
                IbkrOrderIdAllocator(path)
            first.close()

            second = IbkrOrderIdAllocator(path)
            second.close()

    def test_owner_lock_excludes_a_second_os_process(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            child_code = (
                "import sys,time; from ibkr_runtime.order_ids import IbkrOrderIdAllocator; "
                "owner=IbkrOrderIdAllocator(sys.argv[1]); print('OWNER_READY', flush=True); "
                "time.sleep(20)"
            )
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline().strip(), "OWNER_READY")
                with self.assertRaisesRegex(RuntimeError, "another local execution process"):
                    IbkrOrderIdAllocator(path)
            finally:
                child.terminate()
                child.wait(timeout=5)
                if child.stdout is not None:
                    child.stdout.close()
                if child.stderr is not None:
                    child.stderr.close()

    def test_allocated_cursor_is_committed_before_id_is_returned(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "orders.sqlite"
            allocator = IbkrOrderIdAllocator(path)
            allocator.observe_next_valid_id(8)
            with self.assertRaises(OrderIdNotReady):
                allocator.allocate()
            allocator.confirm_reconciled()
            self.assertEqual(allocator.allocate(), 8)
            with sqlite3.connect(path) as db:
                self.assertEqual(
                    db.execute("SELECT next_order_id,ready FROM ibkr_order_id_state").fetchone(),
                    (9, 1),
                )
            allocator.close()


if __name__ == "__main__":
    unittest.main()
