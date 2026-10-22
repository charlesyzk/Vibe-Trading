from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.execution.contracts import Environment
from src.execution.owner_lease import ExecutionOwnerLease, ExecutionOwnerUnavailable


def test_one_owner_per_local_broker_account_environment(tmp_path):
    first = ExecutionOwnerLease(
        lock_directory=tmp_path, broker="IBKR", account_ref="paper-1",
        environment=Environment.PAPER, owner_id="owner-1",
    )
    try:
        with pytest.raises(ExecutionOwnerUnavailable, match="another local process"):
            ExecutionOwnerLease(
                lock_directory=tmp_path, broker="IBKR", account_ref="paper-1",
                environment=Environment.PAPER, owner_id="owner-2",
            )
        # Account/environment are both part of the lock scope.
        other_account = ExecutionOwnerLease(
            lock_directory=tmp_path, broker="IBKR", account_ref="paper-2",
            environment=Environment.PAPER, owner_id="owner-3",
        )
        other_environment = ExecutionOwnerLease(
            lock_directory=tmp_path, broker="IBKR", account_ref="paper-1",
            environment=Environment.LIVE, owner_id="owner-4",
        )
        other_account.close()
        other_environment.close()
        first.assert_scope(broker="IBKR", account_ref="paper-1", environment=Environment.PAPER)
        with pytest.raises(ExecutionOwnerUnavailable, match="scope"):
            first.assert_scope(broker="IBKR", account_ref="paper-2", environment=Environment.PAPER)
    finally:
        first.close()


def test_owner_lock_excludes_a_second_os_process(tmp_path):
    agent_root = Path(__file__).resolve().parents[2]
    child_code = (
        "import sys,time; from src.execution.contracts import Environment; "
        "from src.execution.owner_lease import ExecutionOwnerLease; "
        "lease=ExecutionOwnerLease(lock_directory=sys.argv[1], broker='IBKR', "
        "account_ref='paper-1', environment=Environment.PAPER, owner_id='child'); "
        "print('OWNER_READY', flush=True); time.sleep(20)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code, str(tmp_path)],
        cwd=agent_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "OWNER_READY"
        with pytest.raises(ExecutionOwnerUnavailable, match="another local process"):
            ExecutionOwnerLease(
                lock_directory=tmp_path, broker="IBKR", account_ref="paper-1",
                environment=Environment.PAPER, owner_id="parent",
            )
    finally:
        child.terminate()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()


def test_owner_lease_rejects_a_different_account_scope(tmp_path):
    lease = ExecutionOwnerLease(
        lock_directory=tmp_path, broker="IBKR", account_ref="paper-1",
        environment=Environment.PAPER, owner_id="owner-1",
    )
    try:
        with pytest.raises(ExecutionOwnerUnavailable, match="scope"):
            lease.assert_scope(broker="IBKR", account_ref="other", environment=Environment.PAPER)
    finally:
        lease.close()


def test_owner_metadata_files_are_private_to_the_user(tmp_path):
    lease = ExecutionOwnerLease(
        lock_directory=tmp_path / "private-locks", broker="IBKR", account_ref="paper-1",
        environment=Environment.PAPER, owner_id="owner-1",
    )
    try:
        assert lease.path.stat().st_mode & 0o777 == 0o600
        assert lease.path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        lease.close()
