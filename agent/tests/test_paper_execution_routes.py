from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import api_server
from src.api import state as api_state
from src.api import security as api_security
from src.api.paper_execution_routes import register_paper_execution_routes
from src.api.security import require_auth, require_event_stream_auth
from src.execution.paper_execution_manager import (
    PaperExecutionManager,
    PaperRuntimeAuthorizationError,
    PaperRuntimeConflict,
    PaperRuntimeNotReady,
)
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.execution_service import PaperExecutionService
from src.execution.order_ledger import ExecutionControlState, SQLiteOrderLedger
from src.execution.paper_execution_manager import PaperRuntimeScope, PaperRuntimeUnavailable
from src.execution.contracts import Environment
from src.session.models import AuthMethod, Principal


def _app(*, manager=None, readiness=None):
    app = FastAPI()
    if manager is not None:
        app.state.paper_execution_manager = manager
    if readiness is not None:
        app.state.paper_execution_readiness = readiness
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_event_stream_auth] = lambda: None
    register_paper_execution_routes(app)
    return app


def test_status_reports_inert_default_manager_without_account_details(monkeypatch):
    manager = PaperExecutionManager(runtime_factory=None)
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: manager)

    response = TestClient(_app(manager=manager)).get("/api/execution/paper/status")

    assert response.status_code == 200
    assert response.json() == {
        "configured": False,
        "runtime_factory_registered": False,
        "composition_ready": False,
        "missing_requirements": [
            "ibkr_runtime_factory",
            "strategy_session_builder",
            "attributable_control_authorizer",
            "account_reconciliation_facts",
            "market_data_verified_contracts",
            "risk_cost_facts",
        ],
        "phase": "NOT_CONFIGURED",
        "control_state": None,
        "dispatch_gate_open": False,
        "owner_lease_held": False,
        "reason": "runtime_composition_not_configured",
        "last_error_code": None,
        "strategy_version_id": None,
    }


def test_activity_route_is_authenticated_bounded_and_uses_manager_scope_only():
    seen_limits = []
    manager = SimpleNamespace(activity_snapshot=lambda *, limit: seen_limits.append(limit) or {
        "broker": "IBKR", "environment": "paper", "control_state": "PAUSED",
        "control_reason": None, "orders": [], "history_limit": limit,
        "latest_activity_seq": 0,
        "recovery": {
            "ready_to_reserve": 0, "ready_to_send": 0,
            "unresolved_sends": 0, "active_orders": 0,
        },
        "risk": {
            "source": "ledger_reservations_only", "reserved_cash": "0",
            "reserved_sell_shares": 0, "risk_limits": "unavailable",
        },
        "positions": {
            "source": "unknown", "as_of": None, "positions": [],
            "reason": "no fresh authoritative broker snapshot is available",
        },
    })
    app = _app(manager=manager)
    client = TestClient(app)

    response = client.get(
        "/api/execution/paper/activity?limit=7&account_ref=attacker&environment=live"
    )

    assert response.status_code == 200
    assert response.json()["history_limit"] == 7
    assert response.json()["positions"]["source"] == "unknown"
    assert "attacker" not in response.text
    assert seen_limits == [7]
    assert client.get("/api/execution/paper/activity?limit=101").status_code == 422


def test_activity_route_rejects_missing_bearer_auth(monkeypatch):
    monkeypatch.setattr(api_security, "_configured_api_key", lambda: "fixture-secret")
    app = _app(manager=SimpleNamespace(activity_snapshot=lambda **_: {}))
    app.dependency_overrides.pop(require_auth)

    response = TestClient(app).get("/api/execution/paper/activity")

    assert response.status_code == 401


def test_activity_sse_uses_last_event_id_and_emits_durable_frames():
    seen_cursors = []

    def read_events(*, after_seq, limit):
        seen_cursors.append((after_seq, limit))
        if len(seen_cursors) == 1:
            return [{
                "seq": 4, "event_type": "broker.report",
                "payload": {"intent_id": "fixture-order", "state": "FILLED"},
                "occurred_at": "2026-10-20T14:00:00+00:00",
            }]
        raise RuntimeError("close after fixture event")

    manager = SimpleNamespace(latest_activity_seq=lambda: 5, activity_events_since=read_events)
    response = TestClient(_app(manager=manager)).get(
        "/api/execution/paper/activity/events?after_seq=2",
        headers={"Last-Event-ID": "3"},
    )

    assert response.status_code == 200
    assert "id: 4\nevent: broker.report\ndata: {\"intent_id\":\"fixture-order\",\"state\":\"FILLED\"}" in response.text
    assert seen_cursors == [(3, 100), (4, 100)]


def test_activity_sse_accepts_existing_use_sse_query_cursor_on_reconnect():
    seen_cursors = []

    def read_events(*, after_seq, limit):
        seen_cursors.append(after_seq)
        if len(seen_cursors) == 1:
            return [{"seq": 5, "event_type": "order.state", "payload": {"state": "FILLED"}}]
        raise RuntimeError("close after fixture event")

    manager = SimpleNamespace(latest_activity_seq=lambda: 8, activity_events_since=read_events)
    response = TestClient(_app(manager=manager)).get(
        "/api/execution/paper/activity/events?Last-Event-ID=4&after_seq=2"
    )

    assert response.status_code == 200
    assert "id: 5\nevent: order.state" in response.text
    assert seen_cursors == [4, 5]


def test_activity_sse_rejects_future_cursor_before_streaming():
    from src.execution.order_ledger import InvalidActivityCursor

    manager = SimpleNamespace(
        latest_activity_seq=lambda: 2,
        activity_events_since=lambda *, after_seq, limit: (_ for _ in ()).throw(
            InvalidActivityCursor("activity cursor is ahead")
        ),
    )
    response = TestClient(_app(manager=manager)).get(
        "/api/execution/paper/activity/events", headers={"Last-Event-ID": "3"}
    )

    assert response.status_code == 422


def test_activity_sse_requires_event_stream_auth(monkeypatch):
    monkeypatch.setattr(api_security, "_configured_api_key", lambda: "fixture-secret")
    app = _app(manager=SimpleNamespace(latest_activity_seq=lambda: 0, activity_events_since=lambda **_: []))
    app.dependency_overrides.pop(require_event_stream_auth)

    response = TestClient(app).get("/api/execution/paper/activity/events")

    assert response.status_code == 401


def test_lazy_app_manager_has_no_factory_and_cannot_start(monkeypatch):
    monkeypatch.setattr(api_server, "_paper_execution_manager", None, raising=False)

    manager = api_server._get_paper_execution_manager()

    assert manager.runtime_factory is None
    assert manager.status() is None
    with pytest.raises(PaperRuntimeUnavailable, match="composition is not configured"):
        manager.start(PaperRuntimeScope("IBKR", "paper-fixture", Environment.PAPER))


def test_manager_and_readiness_are_isolated_per_fastapi_application():
    first = _app()
    second = _app()
    first_manager = PaperExecutionManager(runtime_factory=None)
    first_readiness = PaperExecutionReadiness(strategy_session_builder=True)
    second_manager = PaperExecutionManager(runtime_factory=None)
    second_readiness = PaperExecutionReadiness(attributable_control_authorizer=True)
    api_state._set_paper_execution_composition(first_manager, first_readiness, app=first)
    api_state._set_paper_execution_composition(second_manager, second_readiness, app=second)

    assert api_state._get_paper_execution_manager(first) is first_manager
    assert api_state._get_paper_execution_readiness(first) is first_readiness
    assert api_state._get_paper_execution_manager(second) is second_manager
    assert api_state._get_paper_execution_readiness(second) is second_readiness

    assert first_manager is not second_manager
    assert first_readiness is not second_readiness
    assert first_manager.startup_readiness is first_readiness
    assert second_manager.startup_readiness is second_readiness
    assert first_manager.runtime_factory is None
    assert second_manager.runtime_factory is None

    first_status = TestClient(first).get("/api/execution/paper/status").json()
    second_status = TestClient(second).get("/api/execution/paper/status").json()
    assert "strategy_session_builder" not in first_status["missing_requirements"]
    assert "attributable_control_authorizer" in first_status["missing_requirements"]
    assert "attributable_control_authorizer" not in second_status["missing_requirements"]
    assert "strategy_session_builder" in second_status["missing_requirements"]


def test_app_composition_rejects_replacing_active_manager_and_allows_idempotent_pair(monkeypatch):
    app = _app()
    manager = PaperExecutionManager(runtime_factory=None)
    readiness = PaperExecutionReadiness(strategy_session_builder=True)
    api_state._set_paper_execution_composition(manager, readiness, app=app)
    active_status = SimpleNamespace(phase="READY")
    monkeypatch.setattr(manager, "status", lambda: active_status)

    # Idempotent installation of the exact pair keeps the active slot intact.
    api_state._set_paper_execution_composition(manager, readiness, app=app)

    replacement = PaperExecutionManager(runtime_factory=None)
    replacement_readiness = PaperExecutionReadiness(attributable_control_authorizer=True)
    with pytest.raises(PaperRuntimeConflict, match="cannot replace PAPER composition"):
        api_state._set_paper_execution_composition(replacement, replacement_readiness, app=app)

    assert app.state.paper_execution_manager is manager
    assert app.state.paper_execution_readiness is readiness
    assert manager.status() is active_status
    assert replacement.status() is None


def test_app_composition_allows_replacement_when_current_manager_has_no_active_slot():
    app = _app()
    initial_manager = PaperExecutionManager(runtime_factory=None)
    initial_readiness = PaperExecutionReadiness(strategy_session_builder=True)
    api_state._set_paper_execution_composition(initial_manager, initial_readiness, app=app)
    replacement = PaperExecutionManager(runtime_factory=None)
    replacement_readiness = PaperExecutionReadiness(attributable_control_authorizer=True)

    api_state._set_paper_execution_composition(replacement, replacement_readiness, app=app)

    assert app.state.paper_execution_manager is replacement
    assert app.state.paper_execution_readiness is replacement_readiness
    assert replacement.startup_readiness is replacement_readiness


def test_status_exposes_state_flags_but_never_account_scope(monkeypatch):
    manager = SimpleNamespace(
        runtime_factory=lambda _scope: None,
        status=lambda: SimpleNamespace(
            scope=SimpleNamespace(account_ref="private-account"),
            phase="READY",
            control_state=ExecutionControlState.RUNNING,
            reason="explicit_activation",
            owner_lease_held=True,
            last_error_code=None,
        ),
    )
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: manager)

    response = TestClient(_app(manager=manager)).get("/api/execution/paper/status")

    assert response.status_code == 200
    assert response.json()["dispatch_gate_open"] is True
    assert response.json()["control_state"] == "RUNNING"
    assert "account_ref" not in response.text
    assert "private-account" not in response.text


def test_bare_runtime_factory_is_not_a_ready_strategy_composition(monkeypatch):
    manager = PaperExecutionManager(runtime_factory=lambda _scope: None)
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: manager)
    monkeypatch.setattr(
        api_server,
        "_get_paper_execution_readiness",
        lambda: PaperExecutionReadiness(),
        raising=False,
    )

    response = TestClient(_app(manager=manager, readiness=PaperExecutionReadiness())).get(
        "/api/execution/paper/status"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["runtime_factory_registered"] is True
    assert body["composition_ready"] is False
    assert "strategy_session_builder" in body["missing_requirements"]
    assert "account_reconciliation_facts" in body["missing_requirements"]
    assert body["dispatch_gate_open"] is False


def test_status_fails_closed_when_app_readiness_drifts_from_manager_start_gate():
    factory_calls = []
    manager_readiness = PaperExecutionReadiness()
    app_readiness = PaperExecutionReadiness(
        strategy_session_builder=True,
        attributable_control_authorizer=True,
        account_and_reconciliation_facts=True,
        market_data_and_verified_contracts=True,
        risk_and_cost_facts=True,
    )
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: factory_calls.append(scope),
        startup_readiness=manager_readiness,
    )
    app = _app(manager=manager, readiness=app_readiness)

    response = TestClient(app).get("/api/execution/paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["composition_ready"] is False
    assert body["missing_requirements"] == [
        "strategy_session_builder",
        "attributable_control_authorizer",
        "account_reconciliation_facts",
        "market_data_verified_contracts",
        "risk_cost_facts",
        "readiness_binding_mismatch",
    ]
    assert manager.startup_readiness is manager_readiness
    with pytest.raises(PaperRuntimeNotReady):
        manager.start(PaperRuntimeScope("IBKR", "paper-fixture", Environment.PAPER))
    assert factory_calls == []
    assert manager.status() is None


def test_complete_app_binding_reports_the_same_ready_gate_without_starting_runtime():
    factory_calls = []
    readiness = PaperExecutionReadiness(
        strategy_session_builder=True,
        attributable_control_authorizer=True,
        account_and_reconciliation_facts=True,
        market_data_and_verified_contracts=True,
        risk_and_cost_facts=True,
    )
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: factory_calls.append(scope),
    )
    app = _app()

    api_state._set_paper_execution_composition(manager, readiness, app=app)
    response = TestClient(app).get("/api/execution/paper/status")

    assert response.status_code == 200
    body = response.json()
    assert body["runtime_factory_registered"] is True
    assert body["composition_ready"] is True
    assert body["missing_requirements"] == []
    assert body["phase"] == "STOPPED"
    assert body["dispatch_gate_open"] is False
    assert manager.startup_readiness is readiness
    assert api_state._get_paper_execution_readiness(app) is readiness
    assert factory_calls == []
    assert manager.status() is None


def test_composition_readiness_is_explicit_and_complete_only_when_all_roles_are_registered():
    complete = PaperExecutionReadiness(
        strategy_session_builder=True,
        attributable_control_authorizer=True,
        account_and_reconciliation_facts=True,
        market_data_and_verified_contracts=True,
        risk_and_cost_facts=True,
    )
    assert complete.composition_ready is True
    assert complete.missing_requirements == ()


def test_app_composition_binds_status_readiness_to_manager_start_gate(monkeypatch, tmp_path):
    calls = []
    manager = PaperExecutionManager(runtime_factory=lambda scope: calls.append(scope))
    readiness = PaperExecutionReadiness()
    monkeypatch.setattr(api_state, "_paper_execution_manager", None)
    monkeypatch.setattr(api_state, "_paper_execution_readiness", None)
    monkeypatch.setattr(api_server, "_paper_execution_manager", None, raising=False)
    monkeypatch.setattr(api_server, "_paper_execution_readiness", None, raising=False)

    api_state._set_paper_execution_composition(manager, readiness)

    assert api_state._get_paper_execution_readiness() is readiness
    assert manager.startup_readiness is readiness
    with pytest.raises(PaperRuntimeNotReady):
        manager.start(PaperRuntimeScope("IBKR", "paper-fixture", Environment.PAPER))
    assert calls == []


def test_emergency_pause_uses_trusted_active_scope_and_ignores_request_fields(monkeypatch):
    scope = PaperRuntimeScope("IBKR", "private-paper-account", Environment.PAPER)
    state = {"control_state": ExecutionControlState.RUNNING, "phase": "READY"}
    calls = []
    status = lambda: SimpleNamespace(
        scope=scope,
        phase=state["phase"],
        control_state=state["control_state"],
        reason="operator_pause" if state["control_state"] is ExecutionControlState.PAUSED else None,
        owner_lease_held=True,
        last_error_code=None,
    )

    def pause(requested_scope, *, reason):
        calls.append((requested_scope, reason))
        state["control_state"] = ExecutionControlState.PAUSED

    manager = SimpleNamespace(runtime_factory=lambda _scope: None, status=status, pause=pause)
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: manager)
    monkeypatch.setattr(api_server, "_get_paper_execution_readiness", lambda: PaperExecutionReadiness())

    response = TestClient(_app(manager=manager, readiness=PaperExecutionReadiness())).post(
        "/api/execution/paper/pause",
        json={"account_ref": "attacker-account", "reason": "activate", "principal": "forged"},
    )

    assert response.status_code == 200
    assert calls == [(scope, "authenticated_operator_pause")]
    assert response.json()["control_state"] == "PAUSED"
    assert response.json()["dispatch_gate_open"] is False
    assert "private-paper-account" not in response.text
    assert "attacker-account" not in response.text


def test_emergency_pause_refuses_when_no_runtime_is_active(monkeypatch):
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: PaperExecutionManager(runtime_factory=None))

    response = TestClient(_app(manager=PaperExecutionManager(runtime_factory=None))).post(
        "/api/execution/paper/pause"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "no managed PAPER runtime is active"


def test_start_route_requires_trusted_scope_and_does_not_accept_scope_from_body():
    trusted_scope = PaperRuntimeScope("IBKR", "trusted-paper-scope", Environment.PAPER)
    authorizer_calls = []
    runtime_factory = lambda *_args: pytest.fail("runtime factory must not start")
    runtime_factory.strategy_version_id = "d" * 64
    manager = PaperExecutionManager(
        runtime_factory=runtime_factory,
        control_authorizer=lambda principal, scope, action: (
            authorizer_calls.append((principal, scope, action)) is None
            and principal.attributable
            and action == "start"
        ),
    )
    readiness = PaperExecutionReadiness()
    app = _app(manager=manager, readiness=readiness)
    api_state._set_paper_execution_composition(manager, readiness, app=app, scope=trusted_scope)
    app.dependency_overrides[require_auth] = lambda: Principal(
        "fixture-human", AuthMethod.FEDERATED_IDENTITY
    )

    response = TestClient(app).post(
        "/api/execution/paper/start",
        json={
            "broker": "ATTACKER",
            "account_ref": "attacker-account",
            "environment": "live",
            "principal": "forged-human",
            "grant": "start",
            "strategy_version_id": "d" * 64,
        },
    )

    # Authorization sees only the scope installed by trusted app composition.
    # Runtime startup remains blocked because the inert manager has no factory.
    assert response.status_code == 409
    assert authorizer_calls == [
        (Principal("fixture-human", AuthMethod.FEDERATED_IDENTITY), trusted_scope, "start")
    ]
    assert manager.status() is None
    assert "attacker-account" not in response.text


def test_start_route_only_accepts_the_strategy_version_pinned_by_trusted_composition():
    pinned_version = "a" * 64
    scope = PaperRuntimeScope("IBKR", "trusted-paper-scope", Environment.PAPER)
    principal = Principal("fixture-human", AuthMethod.FEDERATED_IDENTITY)
    calls = []
    manager = SimpleNamespace(
        runtime_factory=SimpleNamespace(strategy_version_id=pinned_version),
        startup_readiness=PaperExecutionReadiness(),
        status=lambda: None,
        start_for_principal=lambda requested_scope, actor: calls.append((requested_scope, actor)),
    )
    app = _app(manager=manager)
    app.state.paper_execution_scope = scope
    app.dependency_overrides[require_auth] = lambda: principal
    client = TestClient(app)

    rejected = client.post(
        "/api/execution/paper/start",
        json={"strategy_version_id": "b" * 64},
    )
    assert rejected.status_code == 409
    assert calls == []

    accepted = client.post(
        "/api/execution/paper/start",
        json={"strategy_version_id": pinned_version},
    )
    assert accepted.status_code == 200
    assert accepted.json()["strategy_version_id"] == pinned_version
    assert calls == [(scope, principal)]


@pytest.mark.parametrize(
    ("factory_pin", "body"),
    [
        (None, None),
        (None, {"strategy_version_id": "a" * 64}),
        ("malformed", None),
        ("malformed", {"strategy_version_id": "a" * 64}),
    ],
    ids=["missing-pin-no-version", "missing-pin-arbitrary-version", "malformed-pin-no-version", "malformed-pin-arbitrary-version"],
)
def test_start_route_rejects_missing_or_invalid_version_pin_before_any_side_effect(factory_pin, body):
    scope = PaperRuntimeScope("IBKR", "trusted-paper-scope", Environment.PAPER)
    principal = Principal("fixture-human", AuthMethod.FEDERATED_IDENTITY)
    effects = {"authorizer": 0, "factory": 0, "start": 0, "order": 0}

    def unpinned_runtime_factory(_scope):
        effects["factory"] += 1
        effects["order"] += 1

    if factory_pin is not None:
        unpinned_runtime_factory.strategy_version_id = factory_pin

    manager = PaperExecutionManager(
        runtime_factory=unpinned_runtime_factory,
        control_authorizer=lambda *_args: effects.__setitem__("authorizer", effects["authorizer"] + 1) or True,
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    real_start = manager.start_for_principal

    def count_start(*args):
        effects["start"] += 1
        return real_start(*args)

    manager.start_for_principal = count_start
    app = _app(manager=manager, readiness=manager.startup_readiness)
    app.state.paper_execution_scope = scope
    app.dependency_overrides[require_auth] = lambda: principal

    client = TestClient(app)
    response = (
        client.post("/api/execution/paper/start")
        if body is None
        else client.post("/api/execution/paper/start", json=body)
    )

    assert response.status_code == 409
    assert "pinned strategy version" in response.json()["detail"]
    assert effects == {"authorizer": 0, "factory": 0, "start": 0, "order": 0}


@pytest.mark.parametrize("strategy_version_id", [None, "malformed"])
def test_app_composition_rejects_missing_or_malformed_strategy_version_pin(strategy_version_id):
    from src.execution.paper_execution_composition import PaperExecutionComposition

    runtime_factory = lambda _scope: None
    if strategy_version_id is not None:
        runtime_factory.strategy_version_id = strategy_version_id
    manager = PaperExecutionManager(
        runtime_factory=runtime_factory,
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    with pytest.raises(ValueError, match="must pin a lowercase SHA-256 strategy version"):
        PaperExecutionComposition(
            manager=manager,
            readiness=manager.startup_readiness,
            scope=PaperRuntimeScope("IBKR", "trusted-paper-scope", Environment.PAPER),
        )


def test_start_route_without_trusted_scope_never_calls_authorizer_or_factory():
    authorizer_calls = []
    factory_calls = []
    manager = PaperExecutionManager(
        runtime_factory=lambda scope: factory_calls.append(scope),
        control_authorizer=lambda *args: authorizer_calls.append(args) is None,
    )
    app = _app(manager=manager)
    app.dependency_overrides[require_auth] = lambda: Principal(
        "fixture-human", AuthMethod.FEDERATED_IDENTITY
    )

    response = TestClient(app).post("/api/execution/paper/start")

    assert response.status_code == 409
    assert response.json()["detail"] == "trusted PAPER runtime scope is not configured"
    assert authorizer_calls == []
    assert factory_calls == []


def test_active_management_routes_pass_authenticated_principal_and_manager_scope():
    scope = PaperRuntimeScope("IBKR", "trusted-active-account", Environment.PAPER)
    principal = Principal("shared-key-holder", AuthMethod.SHARED_KEY)
    calls = []

    def deny(action):
        def operation(*args, **kwargs):
            calls.append((action, args, kwargs))
            raise PaperRuntimeAuthorizationError("an attributable human principal is required")
        return operation

    manager = SimpleNamespace(
        runtime_factory=object(),
        startup_readiness=PaperExecutionReadiness(),
        status=lambda: SimpleNamespace(scope=scope),
        recover_failed_session=deny("recover"),
        activate=deny("activate"),
        cancel_owned_order=deny("cancel"),
        stop=deny("stop"),
    )
    app = _app(manager=manager)
    app.dependency_overrides[require_auth] = lambda: principal
    client = TestClient(app)

    responses = [
        client.post("/api/execution/paper/recover", json={"account_ref": "attacker"}),
        client.post("/api/execution/paper/activate", json={"account_ref": "attacker"}),
        client.post(
            "/api/execution/paper/cancel-owned-order",
            json={"intent_id": "intent-1"},
        ),
        client.post("/api/execution/paper/stop", json={"account_ref": "attacker"}),
    ]

    assert [response.status_code for response in responses] == [403, 403, 403, 403]
    assert [call[0] for call in calls] == ["recover", "activate", "cancel", "stop"]
    assert all(call[1][0] == scope for call in calls)
    assert all(call[1][-1] == principal for call in calls)


def test_recover_route_uses_active_scope_and_returns_paused_without_request_body():
    scope = PaperRuntimeScope("IBKR", "trusted-active-account", Environment.PAPER)
    principal = Principal("recovery-human", AuthMethod.FEDERATED_IDENTITY)
    calls = []
    status = SimpleNamespace(
        scope=scope, phase="READY", control_state=ExecutionControlState.PAUSED,
        owner_lease_held=True, reason="recovered_requires_explicit_activation",
        last_error_code=None,
    )
    manager = SimpleNamespace(
        runtime_factory=object(),
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True, attributable_control_authorizer=True,
            account_and_reconciliation_facts=True, market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
        status=lambda: status,
        recover_failed_session=lambda requested_scope, actor: calls.append((requested_scope, actor)),
    )
    app = _app(manager=manager)
    app.dependency_overrides[require_auth] = lambda: principal

    response = TestClient(app).post("/api/execution/paper/recover")

    assert response.status_code == 200
    assert response.json()["control_state"] == "PAUSED"
    assert response.json()["dispatch_gate_open"] is False
    assert response.json()["owner_lease_held"] is True
    assert calls == [(scope, principal)]


def test_start_route_rejects_shared_key_principal_before_factory_or_authorizer():
    scope = PaperRuntimeScope("IBKR", "trusted-paper-scope", Environment.PAPER)
    factory_calls = []
    authorizer_calls = []
    runtime_factory = lambda requested_scope: factory_calls.append(requested_scope)
    runtime_factory.strategy_version_id = "e" * 64
    manager = PaperExecutionManager(
        runtime_factory=runtime_factory,
        control_authorizer=lambda *args: authorizer_calls.append(args) is None,
    )
    readiness = PaperExecutionReadiness()
    app = _app(manager=manager, readiness=readiness)
    api_state._set_paper_execution_composition(manager, readiness, app=app, scope=scope)
    app.dependency_overrides[require_auth] = lambda: Principal(
        "shared-key-holder", AuthMethod.SHARED_KEY
    )

    response = TestClient(app).post(
        "/api/execution/paper/start", json={"strategy_version_id": "e" * 64}
    )

    assert response.status_code == 403
    assert factory_calls == []
    assert authorizer_calls == []
    assert manager.status() is None


def test_cancel_route_rejects_scope_and_identity_fields_in_request_body():
    app = _app()
    response = TestClient(app).post(
        "/api/execution/paper/cancel-owned-order",
        json={
            "intent_id": "intent-1",
            "account_ref": "attacker-account",
            "principal": "forged-human",
            "grant": "cancel",
            "broker_order_id": "123",
        },
    )
    assert response.status_code == 422


def test_emergency_pause_closes_the_durable_gate_and_keeps_the_owner_lease(tmp_path, monkeypatch):
    scope = PaperRuntimeScope("IBKR", "paper-integration-fixture", Environment.PAPER)
    service = PaperExecutionService(
        ledger=SQLiteOrderLedger(tmp_path / "paper.sqlite"),
        lock_directory=tmp_path / "locks",
        broker=scope.broker,
        account_ref=scope.account_ref,
        environment=scope.environment,
        owner_id="pause-route-test",
        recovery_check=lambda: True,
        activation_check=lambda: True,
        quiesce_check=lambda: False,
    )

    class Runtime:
        durable_database_paths = (service.ledger.path,)

        def __init__(self):
            self.service = service

        def start_paused(self):
            self.service.start()

        def activate(self):
            self.service.activate()

        def pause(self, *, reason):
            self.service.pause(reason=reason)

        def cancel_owned_order(self, _intent_id):
            raise AssertionError("pause route must not cancel orders")

        def stop(self):
            return self.service.stop()

    manager = PaperExecutionManager(
        runtime_factory=lambda _scope: Runtime(),
        control_authorizer=lambda principal, _scope, action: principal.attributable and action == "activate",
        startup_readiness=PaperExecutionReadiness(
            strategy_session_builder=True,
            attributable_control_authorizer=True,
            account_and_reconciliation_facts=True,
            market_data_and_verified_contracts=True,
            risk_and_cost_facts=True,
        ),
    )
    manager.start(scope)
    manager.activate(scope, Principal("test-human", AuthMethod.FEDERATED_IDENTITY))
    monkeypatch.setattr(api_server, "_get_paper_execution_manager", lambda: manager)

    response = TestClient(_app(manager=manager)).post("/api/execution/paper/pause")

    assert response.status_code == 200
    assert response.json()["control_state"] == "PAUSED"
    assert response.json()["dispatch_gate_open"] is False
    status = manager.status()
    assert status is not None
    assert status.owner_lease_held is True
    assert service.state() == (ExecutionControlState.PAUSED, "authenticated_operator_pause")
