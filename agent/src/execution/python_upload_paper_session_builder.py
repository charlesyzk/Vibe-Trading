"""Trusted offline builder for one frozen Python-upload PAPER session."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import backtrader

from src.bt_runtime.calendars import EXPECTED_CALENDAR_VERSION
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Environment, PositionMode, RiskGrant, SourceKind, is_sha256
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.event_journal import JournalError, SQLiteEventJournal
from src.execution.event_worker import PYTHON_WORKER_COMPILER_VERSION, PythonEventCoordinator
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.outbox_dispatch import (
    ProposalRiskInputs,
    PythonOutboxIntentHandoff,
    RiskAwareProposalAuthorizer,
)
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.python_sandbox import PythonSandbox
from src.execution.risk import RiskFacts
from src.execution.strategy_source_policy import StrategySourcePolicyError, validate_strategy_source
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


class PythonUploadPaperSessionBuildError(RuntimeError):
    """A frozen Python-upload PAPER session failed trusted composition checks."""


ProposalRiskInputsProvider = Callable[[Any], ProposalRiskInputs]
DispatchRiskFactsProvider = Callable[..., DispatchRiskSnapshot]


class PythonUploadPaperSessionBuilder:
    """Build a manager-owned upload session with no runtime start or lease.

    The version, run identity, data feeds, contract mapping, sandbox and risk
    providers are supplied by the trusted app root. The uploaded source only
    runs later through ``PythonEventCoordinator`` and its isolated sandbox.
    """

    def __init__(
        self,
        *,
        version_store: StrategyVersionStore,
        version_id: str,
        run_id: str,
        deployment_id: str,
        proposal_risk_inputs: ProposalRiskInputsProvider,
        dispatch_risk_facts: DispatchRiskFactsProvider,
        sandbox: PythonSandbox,
        feeds: Mapping[str, PaperCompletedBarFeed],
        data_by_con_id: Mapping[int, Any],
        resolve_contract_identity: Callable[[str], ContractIdentity],
        now: Callable[[], datetime],
        queue_size: int = 256,
        idle_poll_seconds: float = 0.05,
    ) -> None:
        if not isinstance(version_store, StrategyVersionStore):
            raise TypeError("version_store must be a trusted StrategyVersionStore")
        if not isinstance(version_id, str) or not is_sha256(version_id):
            raise ValueError("version_id must be a lowercase immutable SHA-256 ID")
        for label, value in (("run_id", run_id), ("deployment_id", deployment_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} is required")
        if not callable(proposal_risk_inputs):
            raise TypeError("proposal_risk_inputs provider is required")
        if not callable(dispatch_risk_facts):
            raise TypeError("dispatch_risk_facts provider is required")
        if not isinstance(sandbox, PythonSandbox):
            raise TypeError("sandbox must be an explicitly configured PythonSandbox")
        if not isinstance(feeds, Mapping) or not feeds:
            raise TypeError("verified completed-bar feeds are required")
        if not isinstance(data_by_con_id, Mapping) or not data_by_con_id:
            raise TypeError("verified conId-to-feed mapping is required")
        if not callable(resolve_contract_identity):
            raise TypeError("verified contract identity resolver is required")
        if not callable(now):
            raise TypeError("a trusted clock provider is required")
        if type(queue_size) is not int or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if (
            isinstance(idle_poll_seconds, bool)
            or not isinstance(idle_poll_seconds, (int, float))
            or not 0.01 <= idle_poll_seconds <= 5
        ):
            raise ValueError("idle_poll_seconds must be between 10 ms and 5 seconds")
        self.version_store = version_store
        self.version_id = version_id
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.proposal_risk_inputs = proposal_risk_inputs
        self.dispatch_risk_facts = dispatch_risk_facts
        self.sandbox = sandbox
        self.feeds = dict(feeds)
        self.data_by_con_id = dict(data_by_con_id)
        self.resolve_contract_identity = resolve_contract_identity
        self.now = now
        self.queue_size = queue_size
        self.idle_poll_seconds = idle_poll_seconds

    def __call__(self, runtime, scope: PaperRuntimeScope) -> ManagedPythonPaperSession:
        self._validate_runtime(runtime, scope)
        record = self._load_frozen_upload()
        self._validate_feed_contracts(record)
        database_path = self._shared_runtime_database_path(runtime)

        journal = SQLiteEventJournal(database_path)
        identity = {
            "deployment_id": self.deployment_id,
            "account_ref": scope.account_ref,
            "environment": scope.environment.value,
            "strategy_version_hash": record.version_hash,
        }
        self._create_or_verify_run(journal, identity)

        def proposal_inputs(proposal: Any) -> ProposalRiskInputs:
            if proposal.run_id != self.run_id or proposal.identity != identity:
                raise PythonUploadPaperSessionBuildError(
                    "proposal identity differs from the pinned Python-upload PAPER session"
                )
            if proposal.bar.instrument_id not in record.spec.instrument_ids:
                raise PythonUploadPaperSessionBuildError(
                    "proposal instrument differs from the frozen Python-upload version"
                )
            result = self.proposal_risk_inputs(proposal)
            self._validate_proposal_inputs(result, scope, record, proposal.bar.instrument_id)
            return result

        def dispatch_facts(intent: Any, *, excluding_intent_id: str) -> DispatchRiskSnapshot:
            result = self.dispatch_risk_facts(intent, excluding_intent_id=excluding_intent_id)
            self._validate_dispatch_snapshot(result, scope, record, intent.instrument_id)
            if result.deployment_id != self.deployment_id:
                raise PythonUploadPaperSessionBuildError(
                    "dispatch risk facts differ from the pinned deployment"
                )
            return result

        coordinator = PythonEventCoordinator(
            journal=journal,
            versions=self.version_store,
            sandbox=self.sandbox,
            now=self.now,
        )
        handoff = PythonOutboxIntentHandoff(
            journal=journal,
            ledger=runtime.ledger,
            authorize=RiskAwareProposalAuthorizer(proposal_inputs),
        )

        def prohibit_unmanaged_send(*_args: Any, **_kwargs: Any) -> None:
            raise PythonUploadPaperSessionBuildError(
                "managed Backtrader port must provide the only PAPER sender"
            )

        dispatch = PaperOrderDispatchService(
            ledger=runtime.ledger,
            facts_provider=dispatch_facts,
            sender=prohibit_unmanaged_send,
            now=self.now,
        )
        return ManagedPythonPaperSession(
            runtime=runtime,
            run_id=self.run_id,
            version_store=self.version_store,
            version_id=record.version_id,
            journal=journal,
            coordinator=coordinator,
            handoff=handoff,
            dispatch=dispatch,
            feeds=self.feeds,
            data_by_con_id=self.data_by_con_id,
            resolve_contract_identity=self.resolve_contract_identity,
            queue_size=self.queue_size,
            idle_poll_seconds=self.idle_poll_seconds,
            now=self.now,
        )

    def _load_frozen_upload(self) -> StrategyVersionRecord:
        try:
            record = self.version_store.get_version(self.version_id)
            source = self.version_store.read_upload_source(self.version_id)
            if (
                record.version_id != self.version_id
                or record.version_hash != self.version_id
                or record.source_kind is not SourceKind.PYTHON_UPLOAD
                or record.upload_artifact != f"sources/{record.source_hash}.py"
                or record.template_artifact is not None
                or hashlib.sha256(source).hexdigest() != record.source_hash
                or record.spec.source_kind is not SourceKind.PYTHON_UPLOAD
                or record.spec.source_artifact_id != f"upload-{record.source_hash[:16]}"
                or record.compiler_version != PYTHON_WORKER_COMPILER_VERSION
                or record.engine_version != f"backtrader/{backtrader.__version__}"
            ):
                raise ValueError("frozen version/source/runtime provenance")
            text = source.decode("utf-8", errors="strict")
            validate_strategy_source(text)
            spec = record.spec
            policy = spec.bar_policy
            execution = spec.execution_policy
            position = spec.position_policy
            if (
                len(spec.instrument_ids) != 1
                or policy.timeframe != "1d"
                or policy.session != "regular"
                or policy.completed_only is not True
                or policy.calendar_id != "us_equity_rth"
                or policy.timezone != "America/New_York"
                or execution.decision != "completed_bar_available"
                or execution.dispatch != "next_regular_session"
                or execution.order_type != "market"
                or execution.time_in_force != "DAY"
                or execution.outside_rth is not False
                or execution.gap_action != "reject_and_pause"
                or execution.missed_session_action != "expire_and_pause"
                or position.mode is not PositionMode.EXPLICIT_QUANTITY
                or position.long_only is not True
                or position.whole_shares is not True
                or position.allocation_fraction is not None
                or position.rebalance is not None
                or position.initial_entry is not None
            ):
                raise ValueError("frozen Python-upload PAPER policy")
            return record
        except Exception as exc:
            raise PythonUploadPaperSessionBuildError(
                "version is not an installed frozen Python-upload explicit_quantity PAPER version"
            ) from exc

    def _validate_runtime(self, runtime: Any, scope: PaperRuntimeScope) -> None:
        from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime

        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a trusted PaperRuntimeScope")
        if scope.broker != "IBKR" or scope.environment is not Environment.PAPER:
            raise PythonUploadPaperSessionBuildError("Python-upload managed sessions require IBKR/PAPER")
        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("runtime must be an IbkrManagedPaperRuntime")
        if (
            runtime.service.broker != scope.broker
            or runtime.service.account_ref != scope.account_ref
            or runtime.service.environment is not scope.environment
            or runtime.account_ref != scope.account_ref
        ):
            raise PythonUploadPaperSessionBuildError(
                "runtime account/environment differs from the trusted requested scope"
            )
        if runtime._started or runtime.process.pid is not None or runtime.service.lease_held:
            raise PythonUploadPaperSessionBuildError("runtime must be unstarted and unleased during composition")
        if runtime.callback_worker.alive:
            raise PythonUploadPaperSessionBuildError("runtime callback worker must be stopped during composition")

    def _validate_feed_contracts(self, record: StrategyVersionRecord) -> None:
        instrument = record.spec.instrument_ids[0]
        if set(self.feeds) != {instrument}:
            raise PythonUploadPaperSessionBuildError(
                "managed Python-upload PAPER requires exactly the frozen instrument feed"
            )
        feed = self.feeds[instrument]
        if (
            not isinstance(feed, PaperCompletedBarFeed)
            or feed.p.instrument_id != instrument
            or feed.p.timezone != record.spec.bar_policy.timezone
            or feed.p.calendar_id != record.spec.bar_policy.calendar_id
            or feed.p.calendar_version != EXPECTED_CALENDAR_VERSION
        ):
            raise PythonUploadPaperSessionBuildError(
                "completed-bar feed differs from the frozen Python-upload calendar/instrument"
            )
        contract = self.resolve_contract_identity(instrument)
        if (
            not isinstance(contract, ContractIdentity)
            or type(contract.contract_id) is not int
            or contract.contract_id <= 0
            or contract.symbol != instrument
            or contract.security_type != "STK"
            or contract.currency != "USD"
            or self.data_by_con_id.get(contract.contract_id) is not feed
            or set(self.data_by_con_id) != {contract.contract_id}
        ):
            raise PythonUploadPaperSessionBuildError(
                "frozen instrument lacks one verified USD stock contract/feed mapping"
            )

    @staticmethod
    def _shared_runtime_database_path(runtime: Any) -> str:
        try:
            paths = tuple(Path(path).expanduser().resolve() for path in runtime.durable_database_paths)
            ledger_path = Path(runtime.ledger.path).expanduser().resolve()
        except Exception as exc:
            raise PythonUploadPaperSessionBuildError(
                "runtime has no verifiable durable database paths"
            ) from exc
        if not paths or any(path != ledger_path for path in paths):
            raise PythonUploadPaperSessionBuildError(
                "runtime collaborators do not share one SQLite database"
            )
        if Path(runtime.order_index.path).expanduser().resolve() != ledger_path:
            raise PythonUploadPaperSessionBuildError(
                "runtime order index differs from its execution ledger database"
            )
        return str(ledger_path)

    def _create_or_verify_run(self, journal: SQLiteEventJournal, identity: dict[str, str]) -> None:
        try:
            journal.create_run(
                run_id=self.run_id,
                deployment_id=identity["deployment_id"],
                account_ref=identity["account_ref"],
                environment=Environment(identity["environment"]),
                strategy_version_hash=identity["strategy_version_hash"],
            )
        except JournalError as create_error:
            try:
                existing = journal.get_run_identity(self.run_id, allow_paused=True)
            except Exception as read_error:
                raise PythonUploadPaperSessionBuildError(
                    "the pinned Python-upload PAPER run cannot be created or safely reused"
                ) from read_error
            if existing != identity:
                raise PythonUploadPaperSessionBuildError(
                    "existing run identity differs from the pinned Python-upload deployment"
                ) from create_error

    @staticmethod
    def _validate_proposal_inputs(
        inputs: ProposalRiskInputs,
        scope: PaperRuntimeScope,
        record: StrategyVersionRecord,
        instrument_id: str,
    ) -> None:
        if not isinstance(inputs, ProposalRiskInputs):
            raise PythonUploadPaperSessionBuildError("proposal risk provider returned an invalid result")
        grant, facts = inputs.grant, inputs.facts
        if not isinstance(grant, RiskGrant) or not isinstance(facts, RiskFacts):
            raise PythonUploadPaperSessionBuildError(
                "proposal risk provider must return validated grant and account/quote facts"
            )
        if (
            grant.broker != scope.broker
            or grant.account_ref != scope.account_ref
            or grant.environment is not scope.environment
            or grant.strategy_version_hash != record.version_hash
            or instrument_id not in grant.allowed_instruments
            or facts.broker != scope.broker
            or facts.account_ref != scope.account_ref
            or facts.environment is not scope.environment
            or facts.strategy_version_hash != record.version_hash
            or facts.instrument_id != instrument_id
        ):
            raise PythonUploadPaperSessionBuildError(
                "proposal grant/account/quote facts differ from the pinned Python-upload session"
            )
        if (
            not isinstance(inputs.eligible_at, datetime)
            or not isinstance(inputs.expires_at, datetime)
            or inputs.eligible_at.tzinfo is None
            or inputs.eligible_at.utcoffset() is None
            or inputs.expires_at.tzinfo is None
            or inputs.expires_at.utcoffset() is None
            or inputs.eligible_at >= inputs.expires_at
        ):
            raise PythonUploadPaperSessionBuildError("proposal dispatch window must be ordered and timezone-aware")

    @staticmethod
    def _validate_dispatch_snapshot(
        snapshot: DispatchRiskSnapshot,
        scope: PaperRuntimeScope,
        record: StrategyVersionRecord,
        instrument_id: str,
    ) -> None:
        if not isinstance(snapshot, DispatchRiskSnapshot):
            raise PythonUploadPaperSessionBuildError("dispatch risk provider returned an invalid result")
        grant, facts = snapshot.grant, snapshot.facts
        if not isinstance(grant, RiskGrant) or not isinstance(facts, RiskFacts):
            raise PythonUploadPaperSessionBuildError(
                "dispatch risk provider must return validated grant and account/quote facts"
            )
        if (
            grant.broker != scope.broker
            or grant.account_ref != scope.account_ref
            or grant.environment is not scope.environment
            or grant.strategy_version_hash != record.version_hash
            or instrument_id not in grant.allowed_instruments
            or facts.broker != scope.broker
            or facts.account_ref != scope.account_ref
            or facts.environment is not scope.environment
            or facts.strategy_version_hash != record.version_hash
            or facts.instrument_id != instrument_id
        ):
            raise PythonUploadPaperSessionBuildError(
                "dispatch grant/account/quote facts differ from the pinned Python-upload session"
            )


__all__ = ["PythonUploadPaperSessionBuildError", "PythonUploadPaperSessionBuilder"]
