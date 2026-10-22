"""Trusted NL2 template events for the managed PAPER execution chain.

Only the explicitly supported SMA template is evaluated here. Template code
is never loaded or evaluated from the version artifact: the immutable artifact
hash and compiler/engine labels are checked against the installed trusted
compiler, then a small deterministic event evaluator commits the same durable
event/checkpoint/outbox records used by the isolated Python path.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math
from typing import Any

import backtrader

from src.bt_runtime.strategies.sma import (
    TRUSTED_TEMPLATE_COMPILER_VERSION,
    TRUSTED_TEMPLATE_SOURCE,
    TRUSTED_TEMPLATE_SOURCE_SHA256,
    compile_strategy,
)
from src.execution.contracts import Bar, PositionMode, SourceKind
from src.execution.event_journal import EventNotReady, SQLiteEventJournal, WorkerEventResult
from src.execution.event_worker import EventWorkerError, EventWorkerOutcome
from src.execution.outbox_dispatch import OutboxProposal, ProposalRiskInputsProvider
from src.execution.position_sizing import SizingReason, size_target_fraction
from src.execution.versions import StrategyVersionStore


class TrustedTemplatePaperError(RuntimeError):
    """A frozen trusted template cannot produce a safe PAPER event."""


def _bar_payload(bar: Bar | dict[str, Any]) -> dict[str, Any]:
    parsed = bar if isinstance(bar, Bar) else Bar.model_validate(bar)
    if not parsed.is_final:
        raise TrustedTemplatePaperError("trusted template accepts completed bars only")
    return {"bar": parsed.model_dump(mode="json")}


class TargetFractionPaperSizer:
    """Size a trusted template signal from fresh host-owned PAPER facts.

    The strategy/template supplies only BUY or SELL intent. This host-side
    adapter reads the version-bound allocation policy and current trusted
    account/quote facts; it never accepts a quantity or account field from the
    strategy event. The ordinary outbox authorizer fetches facts again and
    risk-checks the resulting exact whole-share quantity before intent creation.
    """

    def __init__(self, risk_inputs: ProposalRiskInputsProvider) -> None:
        if not callable(risk_inputs):
            raise TypeError("risk_inputs must be a trusted host provider")
        self.risk_inputs = risk_inputs

    def __call__(
        self,
        *,
        side: str,
        spec: Any,
        run_id: str,
        event_id: str,
        seq: int,
        identity: dict[str, str],
        bar: Bar,
    ) -> int | None:
        if side not in {"BUY", "SELL"}:
            raise TrustedTemplatePaperError("trusted template signal side is invalid")
        if spec.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE:
            raise TrustedTemplatePaperError("target fraction sizing requires a trusted template")
        if spec.position_policy.mode is not PositionMode.TARGET_FRACTION:
            raise TrustedTemplatePaperError("trusted PAPER template requires target_fraction sizing")
        probe = OutboxProposal(
            run_id=run_id,
            event_id=event_id,
            seq=seq,
            proposal_index=0,
            proposal={"side": side, "integer_qty": 1, "order_type": "market", "time_in_force": "DAY"},
            identity=identity,
            bar=bar,
        )
        inputs = self.risk_inputs(probe)
        grant, facts = inputs.grant, inputs.facts
        # Apply the same age limits as order authorization before deriving a
        # quantity. Authorization will fetch a second fresh sample afterward.
        quote_age = (facts.now - facts.quote_as_of).total_seconds()
        account_age = (facts.now - facts.account_as_of).total_seconds()
        if (
            not facts.account_snapshot_complete
            or quote_age < 0
            or quote_age > grant.quote_max_age_seconds
            or account_age < 0
            or account_age > grant.account_max_age_seconds
        ):
            raise TrustedTemplatePaperError("target sizing requires fresh complete account and quote facts")
        if (
            facts.quote_price <= 0
            or facts.strategy_buy_reservations < 0
            or facts.account_buy_reservations < 0
            or facts.available_cash < 0
        ):
            raise TrustedTemplatePaperError("target sizing requires non-negative reservations and positive prices")
        if facts.account_ref != identity["account_ref"] or facts.strategy_version_hash != identity["strategy_version_hash"]:
            raise TrustedTemplatePaperError("target sizing facts differ from the frozen execution identity")
        if facts.instrument_id != bar.instrument_id or facts.environment.value != identity["environment"]:
            raise TrustedTemplatePaperError("target sizing facts differ from the current paper event")
        if (
            grant.account_ref != identity["account_ref"]
            or grant.strategy_version_hash != identity["strategy_version_hash"]
            or grant.environment.value != identity["environment"]
            or grant.broker != facts.broker
            or bar.instrument_id not in grant.allowed_instruments
        ):
            raise TrustedTemplatePaperError("target sizing grant differs from the frozen execution identity")
        allocation = spec.position_policy.allocation_fraction
        if allocation is None:
            raise TrustedTemplatePaperError("target_fraction allocation is missing")
        if side == "BUY" and facts.strategy_position_qty > 0:
            return None
        if side == "SELL" and facts.strategy_position_qty == 0:
            return None
        # RiskFacts.available_cash is before the account-wide reservation
        # aggregate. Strategy reservations are already included there, so
        # subtracting both fields would count the strategy slice twice.
        cash_after_reservations = max(
            Decimal("0"), facts.available_cash - facts.account_buy_reservations,
        )
        reserved_strategy_shares = int(
            (facts.strategy_buy_reservations / (
                facts.quote_price * (Decimal("1") + facts.price_buffer_fraction)
            )).to_integral_value(rounding=ROUND_CEILING)
        )
        try:
            result = size_target_fraction(
                entering_target=side == "BUY",
                allocation_fraction=allocation if side == "BUY" else None,
                account_equity=facts.net_liquidation if side == "BUY" else None,
                strategy_budget=grant.strategy_budget if side == "BUY" else None,
                cash_available=cash_after_reservations if side == "BUY" else None,
                reference_price=facts.quote_price,
                fee_reserve=facts.estimated_fees if side == "BUY" else None,
                price_buffer_fraction=facts.price_buffer_fraction,
                held_qty=facts.strategy_position_qty + reserved_strategy_shares,
                sellable_qty=facts.strategy_position_qty - facts.strategy_sell_reservations if side == "SELL" else None,
                account_fresh=True,
                position_fresh=True,
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise TrustedTemplatePaperError("trusted target sizing rejected invalid risk facts") from exc
        if result.reason is SizingReason.NO_ORDER_NEEDED:
            return None
        if result.accepted_qty is None or result.accepted_qty <= 0:
            raise TrustedTemplatePaperError(f"trusted target sizing failed: {result.reason.value}")
        if side == "SELL" and result.accepted_qty != facts.strategy_position_qty:
            raise TrustedTemplatePaperError("trusted PAPER exits require the complete strategy-owned long position")
        return result.accepted_qty


class TrustedTemplateEventCoordinator:
    """Release one bar, evaluate a frozen trusted rule, and commit its output."""

    def __init__(
        self,
        *,
        journal: SQLiteEventJournal,
        versions: StrategyVersionStore,
        size_signal: TargetFractionPaperSizer,
    ) -> None:
        if not isinstance(journal, SQLiteEventJournal) or not isinstance(versions, StrategyVersionStore):
            raise TypeError("trusted template coordinator requires a SQLite journal and version store")
        if not isinstance(size_signal, TargetFractionPaperSizer):
            raise TypeError("trusted template coordinator requires host target-fraction sizing")
        self.journal = journal
        self.versions = versions
        self.size_signal = size_signal

    def process_next(
        self,
        run_id: str,
        *,
        next_bar: Bar | dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> EventWorkerOutcome:
        identity = self.journal.get_run_identity(run_id)
        record = self.versions.get_version(identity["strategy_version_hash"])
        if record.source_kind is not SourceKind.NATURAL_LANGUAGE_TEMPLATE:
            raise EventWorkerError("trusted template coordinator rejects Python upload versions")
        if record.compiler_version != TRUSTED_TEMPLATE_COMPILER_VERSION:
            raise EventWorkerError("frozen template compiler version is not installed")
        if record.engine_version != f"backtrader/{backtrader.__version__}":
            raise EventWorkerError("frozen Backtrader engine version is not installed")
        if record.source_hash != TRUSTED_TEMPLATE_SOURCE_SHA256:
            raise EventWorkerError("frozen template source is not the installed trusted compiler artifact")
        if self.versions.read_template_source(record.version_id) != TRUSTED_TEMPLATE_SOURCE:
            raise EventWorkerError("frozen template artifact differs from the trusted compiler source")
        try:
            compiled = compile_strategy(record.spec)
        except (TypeError, ValueError) as exc:
            raise EventWorkerError("frozen strategy spec is not supported by the trusted template compiler") from exc
        if record.spec.template_version != "1.0" or record.spec.template_id not in {"sma_cross", "rsi_threshold"}:
            raise EventWorkerError("managed trusted PAPER supports sma_cross@1.0 and rsi_threshold@1.0 only")

        try:
            released = self.journal.release_next_event(run_id)
        except EventNotReady:
            if next_bar is None:
                raise
            payload = _bar_payload(next_bar)
            self.journal.stage_next_event(run_id, payload, event_id=event_id)
            released = self.journal.release_next_event(run_id)
        if set(released.payload) != {"bar"}:
            raise EventWorkerError("trusted template event must contain exactly one completed bar")
        bar = Bar.model_validate(released.payload["bar"])
        if not bar.is_final or bar.instrument_id not in record.spec.instrument_ids:
            raise EventWorkerError("released bar differs from the frozen template instrument or completion policy")

        previous_state = self.journal.get_checkpoint(run_id)
        try:
            if previous_state and previous_state.get("template_id") != record.spec.template_id:
                raise ValueError("checkpoint template identity")
            if previous_state and previous_state.get("template_version") != record.spec.template_version:
                raise ValueError("checkpoint template version")
            close = Decimal(str(bar.close))
            if not close.is_finite() or close <= 0:
                raise ValueError("current close")
            if record.spec.template_id == "sma_cross":
                next_state, side = _next_sma_state(previous_state, close, compiled.fast_period, compiled.slow_period)
            else:
                next_state, side = _next_rsi_state(previous_state, close, compiled.parameters)
        except (AttributeError, InvalidOperation, TypeError, ValueError, KeyError) as exc:
            raise EventWorkerError("trusted template checkpoint is malformed") from exc
        result_side_qty: int | None = None
        if side is not None:
            result_side_qty = self.size_signal(
                side=side,
                spec=record.spec,
                run_id=run_id,
                event_id=released.event_id,
                seq=released.seq,
                identity=identity,
                bar=bar,
            )
        proposals = () if side is None or result_side_qty is None else ({
            "side": side,
            "integer_qty": result_side_qty,
            "order_type": "market",
            "time_in_force": "DAY",
            "reason": f"{record.spec.template_id}_signal_transition",
        },)
        result = WorkerEventResult(
            event_id=released.event_id,
            state={
                **next_state,
            },
            order_proposals=proposals,
        )
        committed = self.journal.commit_result(run_id, result)
        return EventWorkerOutcome(event=released, committed=committed, result=result)


def _next_sma_state(
    previous_state: dict[str, Any], close: Decimal, fast_period: int | None, slow_period: int | None,
) -> tuple[dict[str, Any], str | None]:
    if fast_period is None or slow_period is None:
        raise ValueError("SMA periods are missing")
    if previous_state and set(previous_state) != {"template_id", "template_version", "closes"}:
        raise ValueError("SMA checkpoint shape")
    prior_closes = [Decimal(str(value)) for value in previous_state.get("closes", [])]
    if len(prior_closes) > slow_period or any(not value.is_finite() or value <= 0 for value in prior_closes):
        raise ValueError("SMA checkpoint values")
    side: str | None = None
    if len(prior_closes) >= slow_period:
        previous_fast = sum(prior_closes[-fast_period:], Decimal("0")) / fast_period
        previous_slow = sum(prior_closes[-slow_period:], Decimal("0")) / slow_period
        current_closes = prior_closes + [close]
        current_fast = sum(current_closes[-fast_period:], Decimal("0")) / fast_period
        current_slow = sum(current_closes[-slow_period:], Decimal("0")) / slow_period
        if previous_fast <= previous_slow and current_fast > current_slow:
            side = "BUY"
        elif previous_fast >= previous_slow and current_fast < current_slow:
            side = "SELL"
    next_closes = (prior_closes + [close])[-slow_period:]
    return {
        "template_id": "sma_cross", "template_version": "1.0",
        "closes": [str(value) for value in next_closes],
    }, side


def _next_rsi_state(
    previous_state: dict[str, Any], close: Decimal, parameters: dict[str, int | float],
) -> tuple[dict[str, Any], str | None]:
    period = parameters.get("period")
    entry_below = parameters.get("entry_below")
    exit_above = parameters.get("exit_above")
    if type(period) is not int or type(entry_below) not in (int, float) or type(exit_above) not in (int, float):
        raise ValueError("RSI parameters are malformed")
    if previous_state:
        if set(previous_state) != {"template_id", "template_version", "rsi_state"}:
            raise ValueError("RSI checkpoint shape")
        state = previous_state["rsi_state"]
    else:
        state = {
            "seen_closes": 0, "warmup_closes": [], "previous_close": None,
            "avg_gain": None, "avg_loss": None, "previous_rsi": None,
        }
    expected = {"seen_closes", "warmup_closes", "previous_close", "avg_gain", "avg_loss", "previous_rsi"}
    if not isinstance(state, dict) or set(state) != expected:
        raise ValueError("RSI state shape")
    seen = state["seen_closes"]
    warmup = state["warmup_closes"]
    if type(seen) is not int or seen < 0 or not isinstance(warmup, list) or len(warmup) > period + 1:
        raise ValueError("RSI warmup state")
    side: str | None = None
    current_close = float(close)
    if not math.isfinite(current_close) or current_close <= 0:
        raise ValueError("RSI close")
    if seen < period + 1:
        warmup = [*warmup, str(close)]
        seen += 1
        if seen == period + 1:
            closes = [float(Decimal(str(value))) for value in warmup]
            changes = [right - left for left, right in zip(closes, closes[1:])]
            gains = [max(change, 0.0) for change in changes]
            losses = [max(-change, 0.0) for change in changes]
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            current_rsi = _rsi_value(avg_gain, avg_loss)
            previous_close = current_close
            previous_rsi = current_rsi
            warmup = []
        else:
            avg_gain = avg_loss = previous_close = previous_rsi = None
    else:
        previous_close = float(Decimal(str(state["previous_close"])))
        avg_gain = float(Decimal(str(state["avg_gain"])))
        avg_loss = float(Decimal(str(state["avg_loss"])))
        previous_rsi = float(Decimal(str(state["previous_rsi"])))
        change = current_close - previous_close
        avg_gain = ((period - 1) * avg_gain + max(change, 0.0)) / period
        avg_loss = ((period - 1) * avg_loss + max(-change, 0.0)) / period
        current_rsi = _rsi_value(avg_gain, avg_loss)
        entered_from_at_or_above = previous_rsi > entry_below or _rsi_equal(previous_rsi, float(entry_below))
        strictly_below_entry = current_rsi < entry_below and not _rsi_equal(current_rsi, float(entry_below))
        exited_from_at_or_below = previous_rsi < exit_above or _rsi_equal(previous_rsi, float(exit_above))
        strictly_above_exit = current_rsi > exit_above and not _rsi_equal(current_rsi, float(exit_above))
        if entered_from_at_or_above and strictly_below_entry:
            side = "BUY"
        elif exited_from_at_or_below and strictly_above_exit:
            side = "SELL"
        seen += 1
        previous_close = current_close
        previous_rsi = current_rsi
        warmup = []
    if any(value is not None and not math.isfinite(value) for value in (avg_gain, avg_loss, previous_close, previous_rsi)):
        raise ValueError("RSI state contains a non-finite value")
    return {
        "template_id": "rsi_threshold", "template_version": "1.0",
        "rsi_state": {
            "seen_closes": seen, "warmup_closes": warmup,
            "previous_close": None if previous_close is None else str(previous_close),
            "avg_gain": None if avg_gain is None else str(avg_gain),
            "avg_loss": None if avg_loss is None else str(avg_loss),
            "previous_rsi": None if previous_rsi is None else str(previous_rsi),
        },
    }, side


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _rsi_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-12)
