"""App composition for the one-shot, fixed AAPL IBKR PAPER integration test."""

from __future__ import annotations

import os
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.config.paths import get_runtime_root
from src.execution.contracts import Environment
from src.execution.ibkr_callback_pump import ContractIdentity
from src.execution.ibkr_completed_bar_source import IbkrPaperCompletedBarSource
from src.execution.ibkr_paper_runtime_factory import (
    IbkrPaperEndpointProfile,
    IbkrPaperRuntimeConfig,
    IbkrPaperRuntimeDependencies,
)
from src.execution.ibkr_paper_risk_facts import (
    IbkrManagedRiskSnapshotSource,
    VerifiedIbkrContract,
)
from src.execution.order_ledger import SQLiteOrderLedger
from src.execution.paper_control_grants import PaperControlGrantStore
from src.execution.paper_execution_manager import PaperExecutionManager, PaperRuntimeScope
from src.execution.paper_integration_test_config import read_paper_integration_test_config
from src.execution.paper_backtest_attestation import verify_paper_backtest_attestation
from src.execution.paper_integration_test_risk import PaperIntegrationTestRiskFactsProvider
from src.execution.paper_strategy_candidates import paper_fixed_order_compatibility_reasons
from src.execution.paper_execution_composition import PaperExecutionComposition
from src.execution.paper_execution_readiness import PaperExecutionReadiness
from src.execution.python_sandbox import PythonSandbox
from src.execution.python_upload_paper_composition_factory import build_frozen_python_upload_paper_composition
from src.execution.versions import StrategyVersionStore
from src.session.models import Principal
from src.trading.connectors.ibkr.local import load_config


_CLIENT_ID = 6138


class PaperSignalSessionMismatch(RuntimeError):
    """The frozen backtest signal is no longer the latest completed session."""


def _require_fixed_order_backtest(root: Path, version):
    evidence = verify_paper_backtest_attestation(
        root / "paper-integration-test" / "backtests",
        version=version,
    )
    report = json.loads(evidence.report_path.read_text(encoding="utf-8"))
    # The dedicated report was fetched and attested by this trusted local
    # PAPER runner before it was copied into the deployment attestation store.
    reasons = paper_fixed_order_compatibility_reasons(
        version, report, trusted_dedicated_backtest=True,
    )
    if reasons:
        raise RuntimeError("strategy backtest is outside the fixed PAPER order envelope")
    return evidence


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = get_runtime_root() / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#") and "=" in clean:
                key, raw = clean.split("=", 1)
                if key.strip() == name:
                    return raw.strip().strip("\"'")
    except OSError:
        pass
    return ""


class PaperIntegrationTestRunner:
    """Submit the single configured signal bar through the managed session."""

    def __init__(self, *, manager: PaperExecutionManager, scope: PaperRuntimeScope,
                 config_path: Path, source: IbkrPaperCompletedBarSource,
                 signal_session_date: date, version_id: str) -> None:
        self.manager = manager
        self.scope = scope
        self.config_path = config_path
        self.source = source
        self.signal_session_date = signal_session_date
        self.version_id = version_id
        self._lock = threading.Lock()

    def status_snapshot(self) -> dict[str, Any]:
        """Expose the fixed order and UTC window without account identifiers."""
        config = read_paper_integration_test_config(self.config_path)
        now = datetime.now(timezone.utc)
        try:
            version_store = StrategyVersionStore(get_runtime_root() / "strategy-versions")
            version = version_store.get_version(self.version_id)
            evidence = _require_fixed_order_backtest(get_runtime_root(), version)
            from src.bt_runtime.calendars import build_xnys_schedule
            evaluation_date = date.fromisoformat(evidence.evaluation_end)
            sessions = build_xnys_schedule(evaluation_date - timedelta(days=14), evaluation_date).sessions
            previous_sessions = [session.session_date for session in sessions if session.session_date < evaluation_date]
            if not previous_sessions:
                raise ValueError("no previous XNYS session available")
            backtest_attestation = {
                "status": "verified",
                "report_sha256": evidence.report_sha256,
                "data_manifest_hash": evidence.data_manifest_hash,
                "evaluation_start": evidence.evaluation_start,
                "evaluation_end": evidence.evaluation_end,
                "suggested_warmup_date": previous_sessions[-1].isoformat(),
            }
        except Exception:
            backtest_attestation = {"status": "blocked"}
        return {
            "configured": True,
            "enabled": config.enabled,
            "window_open": config.is_submission_window_open(now),
            "valid_window_utc": {
                "start": config.valid_window_utc.start.isoformat().replace("+00:00", "Z"),
                "end": config.valid_window_utc.end.isoformat().replace("+00:00", "Z"),
            },
            "symbol": config.instrument.symbol,
            "side": config.order.side,
            "quantity": config.order.quantity,
            "order_type": config.order.order_type,
            "limit_price": str(config.order.limit_price),
            "time_in_force": config.order.time_in_force,
            "regular_trading_hours_only": not config.order.outside_rth,
            "max_notional_usd": str(config.risk.max_order_notional_usd),
            "strategy_version_id": config.strategy_version_id,
            "backtest_attestation": backtest_attestation,
        }

    def __call__(self, principal: Principal) -> dict[str, Any]:
        if not isinstance(principal, Principal) or not principal.attributable:
            raise PermissionError("an attributable operator token is required")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("one-shot integration action is already in progress")
        try:
            config = read_paper_integration_test_config(self.config_path)
            if config.strategy_version_id != self.version_id:
                raise RuntimeError("integration-test configuration no longer matches the pinned strategy version")
            version_store = StrategyVersionStore(get_runtime_root() / "strategy-versions")
            version = version_store.get_version(self.version_id)
            _require_fixed_order_backtest(get_runtime_root(), version)
            now = datetime.now(timezone.utc)
            if not config.is_submission_window_open(now):
                raise RuntimeError("integration-test order is disabled or outside its configured UTC window")

            # Read and validate the exact completed source bar before opening
            # the managed account session or dispatch gate.
            bars = self.source()
            if not bars or bars[-1].session_date != self.signal_session_date:
                raise PaperSignalSessionMismatch("latest completed IBKR bar differs from the frozen signal session")
            matches = [bar for bar in bars if bar.session_date == self.signal_session_date]
            if len(matches) != 1:
                raise RuntimeError("frozen signal session is absent or duplicated in IBKR history")

            self.manager.start_activate_and_push_completed_bar(
                self.scope,
                principal,
                expected_strategy_version_id=self.version_id,
                bar=matches[0],
            )

            wait_started = time.monotonic()
            deadline = wait_started + 15.0
            activity: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                activity = self.manager.activity_snapshot(limit=10)
                if activity.get("orders"):
                    break
                current = self.manager.status()
                if current is None or current.phase != "READY":
                    break
                time.sleep(0.2)
            # The integration action is deliberately one event. Close the
            # gate and reconcile a fresh broker snapshot after dispatch so
            # partners see current broker positions, not inferred fills.
            reconciliation_status = "verified"
            try:
                activity = self.manager.pause_and_refresh_account_snapshot(self.scope)
            except Exception:
                reconciliation_status = "failed"
                try:
                    activity = self.manager.activity_snapshot(limit=10)
                except Exception:
                    activity = None
            status = self.manager.status()
            return {
                "action": "one_shot_paper_integration_test",
                "strategy_version_id": self.version_id,
                "bar_session_date": self.signal_session_date.isoformat(),
                "bar_source": "IBKR PAPER Gateway historical TRADES, complete XNYS daily bar",
                "dispatch_gate_open": bool(status and status.control_state.value == "RUNNING"),
                "phase": status.phase if status else "STOPPED",
                "control_state": status.control_state.value if status else None,
                "post_order_reconciliation": reconciliation_status,
                "activity": activity,
                "order_observed_within_seconds": round(time.monotonic() - wait_started, 2)
                if activity and activity.get("orders") else None,
            }
        finally:
            self._lock.release()


def build_paper_integration_test_composition() -> PaperExecutionComposition:
    """Construct the trusted fixed-version PAPER app composition without connecting."""
    root = get_runtime_root()
    config_path = root / "paper-integration-test.json"
    config = read_paper_integration_test_config(config_path)
    if not config.strategy_version_id:
        raise RuntimeError("the one-shot test strategy version has not been frozen")

    local_config = load_config()
    if (
        local_config.profile != "paper"
        or local_config.readonly is not True
        or local_config.account != config.account_ref
        or local_config.port != config.endpoint.port
        or local_config.host.strip().lower() not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise RuntimeError("saved local IBKR settings differ from the exact PAPER integration-test scope")

    runtime_home = root / "paper-integration-test"
    runtime_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_home.chmod(0o700)
    database_path = runtime_home / "execution.sqlite"
    lock_directory = runtime_home / "owner-lock"
    runtime_project = Path(__file__).resolve().parents[3] / "ibkr-runtime"
    runtime_python = runtime_project / ".venv" / "bin" / "python"
    runtime_config = IbkrPaperRuntimeConfig(
        python=runtime_python,
        runtime_cwd=runtime_project,
        gateway_host=config.endpoint.host,
        gateway_port=config.endpoint.port,
        paper_endpoint_profile=IbkrPaperEndpointProfile.IB_GATEWAY_PAPER,
        client_id=_CLIENT_ID,
        account_ref=config.account_ref,
        database_path=database_path,
        lock_directory=lock_directory,
    )

    version_store = StrategyVersionStore(root / "strategy-versions")
    version = version_store.get_version(config.strategy_version_id)
    backtest_attestation = _require_fixed_order_backtest(root, version)
    if backtest_attestation.evaluation_end is None:
        raise RuntimeError("verified PAPER backtest has no evaluation session")
    signal_session_date = date.fromisoformat(backtest_attestation.evaluation_end)
    deployment_id = f"paper-integration-test:{version.version_id[:16]}"
    run_id = f"paper-integration-test-{version.version_id[:16]}"
    contract = VerifiedIbkrContract(
        instrument_id=config.instrument.symbol,
        symbol=config.instrument.symbol,
        con_id=config.instrument.verified_con_id,
        primary_exchange="NASDAQ",
    )
    now = lambda: datetime.now(timezone.utc)
    def build_feeds():
        # Backtrader calls stop() on data feeds when a managed session exits.
        # The feed then latches closed, so each manager start must receive a
        # fresh instance even though the frozen contract identity is stable.
        feed = PaperCompletedBarFeed(
            instrument_id=config.instrument.symbol,
            timezone="America/New_York",
            currency="USD",
            calendar_id="us_equity_rth",
            now=now,
            idle_poll_seconds=0.05,
        )
        return {config.instrument.symbol: feed}, {contract.con_id: feed}

    feeds, data_by_con_id = build_feeds()
    resolve_contract_identity = lambda instrument_id: ContractIdentity(
        contract_id=contract.con_id,
        symbol=contract.symbol,
        security_type=contract.security_type,
        currency=contract.currency,
    ) if instrument_id == contract.instrument_id else None

    snapshot_reader = IbkrManagedRiskSnapshotSource(account_ref=config.account_ref)
    risk_provider = PaperIntegrationTestRiskFactsProvider(
        config=config,
        strategy_version_hash=version.version_id,
        deployment_id=deployment_id,
        snapshot_reader=snapshot_reader,
        now=now,
    )
    ledger = SQLiteOrderLedger(database_path)

    class RuntimeReference:
        runtime: Any = None

    runtime_ref = RuntimeReference()

    def verified_runtime_snapshot() -> bool:
        runtime = runtime_ref.runtime
        if runtime is None:
            return False
        try:
            snapshot_id = runtime._latest_snapshot_id
            if snapshot_id is None:
                return False
            # The external recovery predicate runs inside complete_recovery,
            # just before the runtime exposes the hydration snapshot to the
            # strategy session. Read the same durable proof directly here;
            # latest_reconciled_snapshot() is intentionally unavailable until
            # that transition finishes.
            snapshot = runtime.snapshot_store.complete_snapshot(snapshot_id)
            return bool(
                snapshot.get("capture_complete") is True
                and snapshot.get("reconciled") is True
                and snapshot.get("account_ref") == config.account_ref
                and snapshot.get("environment") == Environment.PAPER.value
                and snapshot.get("facts_sha256")
                and isinstance(snapshot.get("reconciliation_result"), dict)
            )
        except Exception:
            return False

    def recovery_check() -> bool:
        return verified_runtime_snapshot() is True

    def activation_check() -> bool:
        if not verified_runtime_snapshot():
            return False
        try:
            snapshot = runtime_ref.runtime.latest_reconciled_snapshot()
            current_config = read_paper_integration_test_config(config_path)
            current = datetime.now(timezone.utc)
            if not current_config.is_submission_window_open(current):
                return False
            return not snapshot.get("positions") and not snapshot.get("open_orders")
        except Exception:
            return False

    def quiesce_check() -> bool:
        runtime = runtime_ref.runtime
        return bool(
            runtime is not None
            and runtime.callback_worker.failure is None
            and runtime.callback_pump.events.empty()
            and not runtime.order_index.pending_callbacks()
            and not runtime.order_index.pending_inbox()
        )

    dependencies = IbkrPaperRuntimeDependencies(
        callback_sink=lambda _callback: (_ for _ in ()).throw(
            RuntimeError("callback sink must be installed by the managed session builder")
        ),
        recovery_check=recovery_check,
        activation_check=activation_check,
        quiesce_check=quiesce_check,
        intent_loader=ledger.get_intent,
        contract_loader=lambda instrument_id: contract if instrument_id == contract.instrument_id else None,
    )

    from src.execution.ibkr_paper_runtime_factory import IbkrPaperRuntimeFactory

    base_runtime_factory = IbkrPaperRuntimeFactory(config=runtime_config, dependencies=dependencies)

    def runtime_factory(scope: PaperRuntimeScope):
        runtime = base_runtime_factory(scope)
        runtime_ref.runtime = runtime
        return runtime

    sandbox_image = _setting("VIBE_PYTHON_WORKER_IMAGE")
    if not sandbox_image:
        raise RuntimeError("isolated Python worker image is not configured")
    sandbox = PythonSandbox(sandbox_image, context=_setting("DOCKER_CONTEXT") or None)
    grants = PaperControlGrantStore(root / "paper-control-grants.sqlite")
    scope = PaperRuntimeScope("IBKR", config.account_ref, Environment.PAPER)
    composition = build_frozen_python_upload_paper_composition(
        scope=scope,
        runtime_config=runtime_config,
        runtime_dependencies=dependencies,
        version_store=version_store,
        version_id=version.version_id,
        run_id=run_id,
        deployment_id=deployment_id,
        sandbox=sandbox,
        proposal_risk_inputs=risk_provider.proposal_inputs,
        dispatch_risk_facts=risk_provider,
        feeds=feeds,
        data_by_con_id=data_by_con_id,
        feed_factory=build_feeds,
        resolve_contract_identity=resolve_contract_identity,
        control_authorizer=grants,
        now=now,
        allow_unreported_usd_cash=True,
    )
    # The generic factory validates strategy, session, and control wiring;
    # replace only the low-level runtime constructor with the late-bound
    # lifecycle checks above. Construction remains offline.
    composition.manager.runtime_factory.runtime_factory = runtime_factory
    composition.manager.bind_stopped_activity(scope, ledger)
    source = IbkrPaperCompletedBarSource(
        config=local_config,
        account_ref=config.account_ref,
        contract=contract,
        duration="1 M",
    )
    runner = PaperIntegrationTestRunner(
        manager=composition.manager,
        scope=scope,
        config_path=config_path,
        source=source,
        signal_session_date=signal_session_date,
        version_id=version.version_id,
    )
    return PaperExecutionComposition(
        manager=composition.manager,
        readiness=composition.readiness,
        scope=scope,
        integration_test_runner=runner,
    )


__all__ = ["PaperIntegrationTestRunner", "build_paper_integration_test_composition"]
