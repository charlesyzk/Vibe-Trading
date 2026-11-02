from datetime import datetime, timezone
import sqlite3

import pytest

from src.execution.contracts import Environment
from src.execution.paper_deployment_receipt import deployment_granted_at
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.session.models import AuthMethod, Principal


def test_receipt_restores_original_time_without_extending_entry_authorization(tmp_path):
    scope = PaperRuntimeScope("IBKR", "DU123", Environment.PAPER)
    principal = Principal(subject="partner", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN)
    draft = {"strategy_version_id": "a" * 64, "setup": {
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2}}
    db = tmp_path / "receipts.sqlite"
    issued = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    after = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
    def receipt(**changes):
        return deployment_granted_at(**({"database": db, "scope": scope, "principal": principal,
                                         "draft": draft, "now": after} | changes))
    assert receipt(now=issued) == issued
    assert receipt() == issued
    with pytest.raises(ValueError, match="expired draft"):
        receipt(draft={**draft, "strategy_version_id": "b" * 64})
    with pytest.raises(ValueError, match="expired draft"):
        receipt(scope=PaperRuntimeScope("IBKR", "DU456", Environment.PAPER))
    with pytest.raises(ValueError, match="expired draft"):
        receipt(principal=Principal(subject="someone-else", auth_method=AuthMethod.LOCAL_OPERATOR_TOKEN))
    with pytest.raises(ValueError, match="receipt is invalid"):
        receipt(now=datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT subject, account_ref FROM paper_deployment_audit").fetchone() == ("partner", "DU123")
        connection.execute("UPDATE paper_deployment_audit SET subject='other'")
    with pytest.raises(ValueError, match="audit identity"):
        receipt()
