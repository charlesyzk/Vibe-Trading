from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.execution.ibkr_order_ids import IbkrOrderIdAllocator, OrderIdNotReady


def test_allocation_requires_fresh_broker_value_and_reconciliation(tmp_path):
    path = tmp_path / "execution.sqlite"
    allocator = IbkrOrderIdAllocator(path)
    with pytest.raises(OrderIdNotReady):
        allocator.confirm_reconciled()
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()

    allocator.observe_next_valid_id(41)
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.confirm_reconciled()
    assert allocator.allocate() == 41
    assert allocator.allocate() == 42
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT next_order_id,ready FROM ibkr_order_id_state WHERE singleton=1"
        ).fetchone() == (43, 1)
    allocator.close()


def test_new_runtime_epoch_needs_a_new_candidate_and_stale_value_fails_closed(tmp_path):
    path = tmp_path / "execution.sqlite"
    allocator = IbkrOrderIdAllocator(path)
    allocator.observe_next_valid_id(90)
    allocator.confirm_reconciled()
    assert allocator.allocate() == 90
    allocator.close()

    restarted = IbkrOrderIdAllocator(path)
    with pytest.raises(OrderIdNotReady):
        restarted.allocate()
    with pytest.raises(OrderIdNotReady, match="below the durable local cursor"):
        restarted.observe_next_valid_id(90)
    with pytest.raises(OrderIdNotReady):
        restarted.allocate()
    restarted.close()


def test_stale_callback_clears_any_prior_unconsumed_candidate(tmp_path):
    allocator = IbkrOrderIdAllocator(tmp_path / "execution.sqlite")
    allocator.observe_next_valid_id(17)
    # A stale callback after a valid candidate invalidates that candidate.
    with pytest.raises(OrderIdNotReady, match="below the durable local cursor"):
        allocator.observe_next_valid_id(16)
    with pytest.raises(OrderIdNotReady):
        allocator.confirm_reconciled()
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    allocator.observe_next_valid_id(17)
    allocator.confirm_reconciled()
    assert allocator.allocate() == 17
    allocator.close()


def test_refreshing_candidate_closes_allocator_until_new_next_valid_id(tmp_path):
    allocator = IbkrOrderIdAllocator(tmp_path / "execution.sqlite")
    allocator.observe_next_valid_id(17)
    allocator.confirm_reconciled()
    assert allocator.allocate() == 17
    allocator.require_fresh_candidate()
    with pytest.raises(OrderIdNotReady):
        allocator.allocate()
    with pytest.raises(OrderIdNotReady):
        allocator.candidate_for_reconciliation()
    allocator.observe_next_valid_id(18)
    assert allocator.candidate_for_reconciliation() == 18
    allocator.close()


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_next_valid_id_must_be_a_positive_integer(tmp_path, value):
    allocator = IbkrOrderIdAllocator(tmp_path / "execution.sqlite")
    with pytest.raises(ValueError, match="positive integer"):
        allocator.observe_next_valid_id(value)
    allocator.close()


def test_one_local_process_owns_the_allocator_path(tmp_path):
    path = tmp_path / "execution.sqlite"
    first = IbkrOrderIdAllocator(path)
    with pytest.raises(RuntimeError, match="another local execution process"):
        IbkrOrderIdAllocator(path)
    first.close()
    second = IbkrOrderIdAllocator(path)
    second.close()


def test_allocator_lock_excludes_another_process_and_releases_on_exit(tmp_path):
    path = tmp_path / "execution.sqlite"
    code = (
        "import sys,time; from src.execution.ibkr_order_ids import IbkrOrderIdAllocator; "
        "owner=IbkrOrderIdAllocator(sys.argv[1]); print('OWNER_READY', flush=True); time.sleep(20)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(path)], cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "OWNER_READY"
        with pytest.raises(RuntimeError, match="another local execution process"):
            IbkrOrderIdAllocator(path)
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()
    reopened = IbkrOrderIdAllocator(path)
    reopened.close()
