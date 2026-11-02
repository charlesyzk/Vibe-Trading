from datetime import datetime, timezone

from src.execution.contracts import Environment
from src.execution.paper_draft_control_authorizer import PaperDraftControlAuthorizer
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.paper_order_preparation import PaperOrderSetup
from src.session.models import AuthMethod, Principal


def test_deploy_action_authorizes_entry_only_in_window_and_cancel_afterward():
    scope = PaperRuntimeScope("IBKR", "DU123", Environment.PAPER)
    operator = Principal("partner", AuthMethod.LOCAL_OPERATOR_TOKEN)
    setup = PaperOrderSetup.model_validate({
        "valid_from": "2026-11-02T14:30:00Z", "valid_until": "2026-11-02T21:00:00Z",
        "max_order_notional": "500", "max_gross_exposure": "1000", "strategy_budget": "900",
        "max_daily_loss": "100", "max_orders_per_session": 2})
    issued = datetime(2026, 11, 2, 13, tzinfo=timezone.utc)
    clock = [datetime(2026, 11, 2, 15, tzinfo=timezone.utc)]
    authorize = PaperDraftControlAuthorizer(scope=scope, principal=operator, setup=setup, granted_at=issued,
                                             now=lambda: clock[0], fallback=lambda *_: False)
    assert authorize(operator, scope, "start") and authorize(operator, scope, "activate")
    clock[0] = setup.valid_until
    assert not authorize(operator, scope, "start") and not authorize(operator, scope, "activate")
    assert authorize(operator, scope, "cancel") and authorize(operator, scope, "stop")
    assert not authorize(operator, scope, "unknown")
    assert not authorize(Principal("partner", AuthMethod.SHARED_KEY), scope, "cancel")
    clock[0] = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    assert not authorize(operator, scope, "cancel")
