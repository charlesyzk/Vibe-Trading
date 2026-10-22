"""Trusted, offline composition for one frozen NL2 PAPER session.

The builder receives every account, authorization, market-data, and risk
collaborator from the trusted application root. It binds the selected frozen
template to the runtime's existing SQLite database and constructs a managed
session without starting the runtime, acquiring a lease, or contacting IBKR.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import backtrader

from src.bt_runtime.calendars import EXPECTED_CALENDAR_VERSION
from src.bt_runtime.feeds.paper_queue import PaperCompletedBarFeed
from src.bt_runtime.strategies.sma import (
    TRUSTED_TEMPLATE_COMPILER_VERSION,
    TRUSTED_TEMPLATE_SOURCE,
    TRUSTED_TEMPLATE_SOURCE_SHA256,
    compile_strategy,
)
from src.execution.backtrader_dispatch import ContractIdentity
from src.execution.contracts import Environment, PositionMode, RiskGrant, SourceKind, is_sha256
from src.execution.dispatch_service import DispatchRiskSnapshot, PaperOrderDispatchService
from src.execution.event_journal import JournalError, SQLiteEventJournal
from src.execution.managed_python_paper_session import ManagedPythonPaperSession
from src.execution.outbox_dispatch import (
    ProposalRiskInputs,
    PythonOutboxIntentHandoff,
    RiskAwareProposalAuthorizer,
)
from src.execution.paper_execution_manager import PaperRuntimeScope
from src.execution.risk import RiskFacts
from src.execution.trusted_template_paper import (
    TargetFractionPaperSizer,
    TrustedTemplateEventCoordinator,
)
from src.execution.versions import StrategyVersionRecord, StrategyVersionStore


class TrustedTemplatePaperSessionBuildError(RuntimeError):
    """Trusted frozen-version PAPER composition failed closed."""


ProposalRiskInputsProvider = Callable[[Any], ProposalRiskInputs]
DispatchRiskFactsProvider = Callable[..., DispatchRiskSnapshot]


class TrustedTemplatePaperSessionBuilder:
    """Construct one already-frozen NL2 managed session from injected facts.

    This callable is suitable for ``ManagedPythonPaperDeploymentFactory``.
    All mutable external facts remain behind the two explicitly supplied
    providers; the builder neither invents values nor calls them during
    construction. ``version_id``, ``run_id`` and ``deployment_id`` are pinned
    by the trusted composition root, never read from a request or strategy.
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
        self.feeds = dict(feeds)
        self.data_by_con_id = dict(data_by_con_id)
        self.resolve_contract_identity = resolve_contract_identity
        self.now = now
        self.queue_size = queue_size
        self.idle_poll_seconds = idle_poll_seconds

    def __call__(self, runtime, scope: PaperRuntimeScope) -> ManagedPythonPaperSession:
        self._validate_runtime(runtime, scope)
        record = self._load_frozen_template()
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

        def proposal_inputs(proposal):
            if proposal.run_id != self.run_id or proposal.identity != identity:
                raise TrustedTemplatePaperSessionBuildError(
                    "proposal identity differs from the pinned NL2 PAPER session"
                )
            if proposal.bar.instrument_id not in record.spec.instrument_ids:
                raise TrustedTemplatePaperSessionBuildError(
                    "proposal instrument differs from the frozen NL2 version"
                )
            result = self.proposal_risk_inputs(proposal)
            self._validate_proposal_inputs(result, scope, record, proposal.bar.instrument_id)
            return result

        def dispatch_facts(intent, *, excluding_intent_id):
            result = self.dispatch_risk_facts(
                intent, excluding_intent_id=excluding_intent_id,
            )
            self._validate_dispatch_snapshot(result, scope, record, intent.instrument_id)
            if result.deployment_id != self.deployment_id:
                raise TrustedTemplatePaperSessionBuildError(
                    "dispatch risk facts differ from the pinned deployment"
                )
            return result

        sizer = TargetFractionPaperSizer(proposal_inputs)
        coordinator = TrustedTemplateEventCoordinator(
            journal=journal, versions=self.version_store, size_signal=sizer,
        )
        handoff = PythonOutboxIntentHandoff(
            journal=journal,
            ledger=runtime.ledger,
            authorize=RiskAwareProposalAuthorizer(proposal_inputs),
        )

        def prohibit_unmanaged_send(*_args, **_kwargs) -> None:
            raise TrustedTemplatePaperSessionBuildError(
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
            version_id=self.version_id,
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

    def _load_frozen_template(self) -> StrategyVersionRecord:
        try:
            record = self.version_store.get_version(self.version_id)
            artifact = self.version_store.read_template_source(self.version_id)
            if record.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE:
                raise ValueError("source_kind")
            if record.spec.position_policy.mode.value != "target_fraction":
                raise ValueError("position_mode")
            if record.template_artifact is None or record.upload_artifact is not None:
                raise ValueError("artifact kind")
            if (
                record.source_hash != TRUSTED_TEMPLATE_SOURCE_SHA256
                or artifact != TRUSTED_TEMPLATE_SOURCE
                or record.compiler_version != TRUSTED_TEMPLATE_COMPILER_VERSION
                or record.engine_version != f"backtrader/{backtrader.__version__}"
            ):
                raise ValueError("trusted artifact provenance")
            if record.spec.template_version != "1.0" or record.spec.template_id not in {
                "sma_cross", "rsi_threshold",
            }:
                raise ValueError("template identity")
            spec = record.spec
            if (
                spec.bar_policy.timeframe != "1d"
                or spec.bar_policy.session != "regular"
                or spec.bar_policy.completed_only is not True
                or spec.bar_policy.calendar_id != "us_equity_rth"
                or spec.bar_policy.timezone != "America/New_York"
                or spec.execution_policy.decision != "completed_bar_available"
                or spec.execution_policy.dispatch != "next_regular_session"
                or spec.execution_policy.order_type != "market"
                or spec.execution_policy.time_in_force != "DAY"
                or spec.execution_policy.outside_rth is not False
                or spec.position_policy.mode is not PositionMode.TARGET_FRACTION
                or spec.position_policy.long_only is not True
                or spec.position_policy.whole_shares is not True
                or spec.position_policy.allocation_fraction is None
                or spec.position_policy.rebalance != "on_signal_transition"
                or spec.position_policy.initial_entry != "wait_for_cross"
            ):
                raise ValueError("managed session policy")
            compile_strategy(record.spec)
            return record
        except Exception as exc:
            raise TrustedTemplatePaperSessionBuildError(
                "version is not an installed frozen NL2 target_fraction template"
            ) from exc

    def _validate_runtime(self, runtime, scope: PaperRuntimeScope) -> None:
        from src.execution.ibkr_managed_runtime import IbkrManagedPaperRuntime

        if not isinstance(scope, PaperRuntimeScope):
            raise TypeError("scope must be a trusted PaperRuntimeScope")
        if scope.broker != "IBKR" or scope.environment is not Environment.PAPER:
            raise TrustedTemplatePaperSessionBuildError(
                "trusted NL2 managed sessions require an IBKR/PAPER scope"
            )
        if not isinstance(runtime, IbkrManagedPaperRuntime):
            raise TypeError("runtime must be an IbkrManagedPaperRuntime")
        if (
            runtime.service.broker != scope.broker
            or runtime.service.account_ref != scope.account_ref
            or runtime.service.environment is not scope.environment
            or runtime.account_ref != scope.account_ref
        ):
            raise TrustedTemplatePaperSessionBuildError(
                "runtime account/environment differs from the trusted requested scope"
            )
        if runtime._started or runtime.process.pid is not None or runtime.service.lease_held:
            raise TrustedTemplatePaperSessionBuildError(
                "runtime must be unstarted and unleased during session composition"
            )
        if runtime.callback_worker.alive:
            raise TrustedTemplatePaperSessionBuildError(
                "runtime callback worker must be stopped during session composition"
            )

    @staticmethod
    def _shared_runtime_database_path(runtime) -> str:
        try:
            paths = tuple(Path(path).expanduser().resolve() for path in runtime.durable_database_paths)
            ledger_path = Path(runtime.ledger.path).expanduser().resolve()
        except Exception as exc:
            raise TrustedTemplatePaperSessionBuildError(
                "runtime has no verifiable durable database paths"
            ) from exc
        if not paths or any(path != ledger_path for path in paths):
            raise TrustedTemplatePaperSessionBuildError(
                "runtime collaborators do not share one SQLite database"
            )
        if Path(runtime.order_index.path).expanduser().resolve() != ledger_path:
            raise TrustedTemplatePaperSessionBuildError(
                "runtime order index differs from its execution ledger database"
            )
        return str(ledger_path)

    def _validate_feed_contracts(self, record: StrategyVersionRecord) -> None:
        instruments = set(record.spec.instrument_ids)
        if len(instruments) != 1 or set(self.feeds) != instruments:
            raise TrustedTemplatePaperSessionBuildError(
                "managed NL2 PAPER requires exactly one frozen instrument and matching feed"
            )
        instrument = next(iter(instruments))
        feed = self.feeds[instrument]
        if (
            not isinstance(feed, PaperCompletedBarFeed)
            or feed.p.instrument_id != instrument
            or feed.p.timezone != record.spec.bar_policy.timezone
            or feed.p.calendar_id != record.spec.bar_policy.calendar_id
            or feed.p.calendar_version != EXPECTED_CALENDAR_VERSION
        ):
            raise TrustedTemplatePaperSessionBuildError(
                "completed-bar feed differs from the frozen template calendar/instrument"
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
            raise TrustedTemplatePaperSessionBuildError(
                "frozen instrument lacks one verified USD stock contract/feed mapping"
            )

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
                raise TrustedTemplatePaperSessionBuildError(
                    "the pinned PAPER run cannot be created or safely reused"
                ) from read_error
            if existing != identity:
                raise TrustedTemplatePaperSessionBuildError(
                    "existing run identity differs from the pinned NL2 PAPER deployment"
                ) from create_error

    @staticmethod
    def _validate_proposal_inputs(
        inputs: ProposalRiskInputs,
        scope: PaperRuntimeScope,
        record: StrategyVersionRecord,
        instrument_id: str,
    ) -> None:
        if not isinstance(inputs, ProposalRiskInputs):
            raise TrustedTemplatePaperSessionBuildError(
                "proposal risk provider returned an invalid result"
            )
        grant, facts = inputs.grant, inputs.facts
        if not isinstance(grant, RiskGrant) or not isinstance(facts, RiskFacts):
            raise TrustedTemplatePaperSessionBuildError(
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
            raise TrustedTemplatePaperSessionBuildError(
                "proposal grant/account/quote facts differ from the pinned PAPER session"
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
            raise TrustedTemplatePaperSessionBuildError(
                "proposal dispatch window must be ordered and timezone-aware"
            )

    @staticmethod
    def _validate_dispatch_snapshot(
        snapshot: DispatchRiskSnapshot,
        scope: PaperRuntimeScope,
        record: StrategyVersionRecord,
        instrument_id: str,
    ) -> None:
        if not isinstance(snapshot, DispatchRiskSnapshot):
            raise TrustedTemplatePaperSessionBuildError(
                "dispatch risk provider returned an invalid result"
            )
        grant, facts = snapshot.grant, snapshot.facts
        if not isinstance(grant, RiskGrant) or not isinstance(facts, RiskFacts):
            raise TrustedTemplatePaperSessionBuildError(
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
            raise TrustedTemplatePaperSessionBuildError(
                "dispatch grant/account/quote facts differ from the pinned PAPER session"
            )


__all__ = [
    "TrustedTemplatePaperSessionBuildError",
    "TrustedTemplatePaperSessionBuilder",
]
