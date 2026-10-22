import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";
import { api } from "@/lib/api";
import { Execution } from "../Execution";

const paperSse = vi.hoisted(() => {
  const state = {
    connect: vi.fn(),
    disconnect: vi.fn(),
    onStatusChange: vi.fn(),
    statusHandler: null as ((status: "disconnected" | "connected" | "reconnecting") => void) | null,
  };
  state.onStatusChange.mockImplementation((handler) => { state.statusHandler = handler; });
  return state;
});

vi.mock("@/hooks/useSSE", () => ({
  useSSE: () => ({
    connect: paperSse.connect,
    disconnect: paperSse.disconnect,
    onStatusChange: paperSse.onStatusChange,
  }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getPaperExecutionStatus: vi.fn().mockResolvedValue({
      configured: false,
      runtime_factory_registered: false,
      composition_ready: false,
      missing_requirements: [
        "strategy_session_builder",
        "attributable_control_authorizer",
        "account_reconciliation_facts",
        "market_data_verified_contracts",
        "risk_cost_facts",
      ],
      phase: "NOT_CONFIGURED",
      control_state: null,
      dispatch_gate_open: false,
      owner_lease_held: false,
      reason: "runtime_composition_not_configured",
      last_error_code: null,
    }),
    getPaperExecutionActivity: vi.fn().mockRejectedValue(new Error("no active PAPER runtime")),
    paperExecutionActivityEventsUrl: vi.fn((afterSeq: number) => `/api/execution/paper/activity/events?after_seq=${afterSeq}`),
    pausePaperExecution: vi.fn(),
    previewExecutionBacktest: vi.fn(),
    createExecutionBacktest: vi.fn(),
    previewPythonUploadBacktest: vi.fn(),
    createPythonUploadBacktest: vi.fn(),
  },
}));

function renderExecution() {
  return render(<MemoryRouter><Execution /></MemoryRouter>);
}

describe("Execution page", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    paperSse.connect.mockReset();
    paperSse.disconnect.mockReset();
    paperSse.onStatusChange.mockClear();
    paperSse.statusHandler = null;
    vi.mocked(api.getPaperExecutionActivity).mockRejectedValue(new Error("no active PAPER runtime"));
    vi.mocked(api.getPaperExecutionStatus).mockResolvedValue({
      configured: false,
      runtime_factory_registered: false,
      composition_ready: false,
      missing_requirements: [
        "strategy_session_builder",
        "attributable_control_authorizer",
        "account_reconciliation_facts",
        "market_data_verified_contracts",
        "risk_cost_facts",
      ],
      phase: "NOT_CONFIGURED",
      control_state: null,
      dispatch_gate_open: false,
      owner_lease_held: false,
      reason: "runtime_composition_not_configured",
      last_error_code: null,
    });
  });

  it("shows the verified offline Backtrader fixture and makes its limits explicit", async () => {
    renderExecution();

    expect(screen.getByRole("heading", { name: "Backtrader, one bar at a time." })).toBeInTheDocument();
    expect(screen.getByText("Backtrader 1.9.78.123")).toBeInTheDocument();
    expect(screen.getByText("sma_cross · 1.0")).toBeInTheDocument();
    expect(screen.getByText("12 synthetic daily bars")).toBeInTheDocument();
    expect(screen.getByText("3 · BUY / SELL / BUY")).toBeInTheDocument();
    expect(screen.getByText("Open position reported")).toBeInTheDocument();
    expect(screen.getByText(/isolated Backtrader offline upload API is available/)).toBeInTheDocument();
    expect(screen.getAllByText(/source claims, point-in-time availability, calendar/)).toHaveLength(2);
    expect(await screen.findByText(/No trusted runtime composition is configured/)).toBeInTheDocument();
    expect(screen.getByText("NOT_CONFIGURED")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review strategy" })).toBeInTheDocument();
  });

  it("shows persisted PAPER activity while keeping broker positions and risk limits distinct", async () => {
    vi.mocked(api.getPaperExecutionActivity).mockResolvedValue({
      broker: "ibkr",
      environment: "paper",
      control_state: "PAUSED",
      control_reason: "recovery_required",
      history_limit: 50,
      latest_activity_seq: 6,
      orders: [{
        intent_id: "intent-123",
        created_at: "2026-10-22T10:00:00Z",
        updated_at: "2026-10-22T10:01:00Z",
        state: "PARTIALLY_FILLED",
        reserved_amount: "25.00",
        filled_qty: 1,
        remaining_qty: 1,
        average_fill_price: "12.50",
        broker_order_id: 77,
        attempt_id: "attempt-1",
        intent: { instrument_id: "AAPL", side: "BUY", integer_qty: 2, order_type: "market", time_in_force: "DAY", reference_price: "12.50" },
        fills: [{ broker_exec_id: "exec-1", quantity: 1, price: "12.50", occurred_at: "2026-10-22T10:01:00Z" }],
        fees: [{ fee_fact_id: "fee-1", broker_exec_id: "exec-1", amount: "0.25", currency: "USD", received_at: "2026-10-22T10:01:01Z", association_status: "LINKED", review_status: "NO_REVISION_SEEN", amount_status: "REPORTED_UNCONFIRMED" }],
      }],
      recovery: { ready_to_reserve: 0, ready_to_send: 0, unresolved_sends: 1, active_orders: 1 },
      risk: { source: "ledger_reservations_only", reserved_cash: "25.00", reserved_sell_shares: 0, risk_limits: "unavailable" },
      positions: { source: "unknown", as_of: null, positions: [], reason: "no fresh authoritative broker snapshot is available" },
    });
    renderExecution();

    expect(await screen.findByText("Persisted PAPER activity")).toBeInTheDocument();
    expect(screen.getByText("BUY 2 AAPL")).toBeInTheDocument();
    expect(screen.getByText("Order intent-123 · 1 filled · 1 remaining")).toBeInTheDocument();
    expect(screen.getByText("Unknown: no fresh authoritative broker snapshot is available.")).toBeInTheDocument();
    expect(screen.getByText("Ledger reservations only; current risk limits are unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Reported fees · unconfirmed: 0.25 USD")).toBeInTheDocument();
  });

  it("subscribes after the activity snapshot cursor and refreshes on persisted events", async () => {
    vi.mocked(api.getPaperExecutionStatus).mockResolvedValue({
      configured: true,
      runtime_factory_registered: true,
      composition_ready: true,
      missing_requirements: [],
      phase: "READY",
      control_state: "PAUSED",
      dispatch_gate_open: false,
      owner_lease_held: true,
      reason: null,
      last_error_code: null,
    });
    vi.mocked(api.getPaperExecutionActivity).mockResolvedValue({
      broker: "ibkr",
      environment: "paper",
      control_state: "PAUSED",
      control_reason: null,
      history_limit: 50,
      latest_activity_seq: 12,
      orders: [],
      recovery: { ready_to_reserve: 0, ready_to_send: 0, unresolved_sends: 0, active_orders: 0 },
      risk: { source: "ledger_reservations_only", reserved_cash: "0", reserved_sell_shares: 0, risk_limits: "unavailable" },
      positions: { source: "unknown", as_of: null, positions: [], reason: "no snapshot" },
    });
    renderExecution();

    await waitFor(() => expect(paperSse.connect).toHaveBeenCalledTimes(1));
    expect(api.paperExecutionActivityEventsUrl).toHaveBeenCalledWith(12);
    act(() => paperSse.statusHandler?.("connected"));
    expect(screen.getByText("Live activity updates connected")).toBeInTheDocument();
    const handlers = paperSse.connect.mock.calls[0][1] as { message: () => void };
    act(() => handlers.message());
    await waitFor(() => expect(api.getPaperExecutionActivity).toHaveBeenCalledTimes(2));
    act(() => paperSse.statusHandler?.("reconnecting"));
    expect(screen.getByText("Live updates interrupted; reconnecting. Periodic snapshots continue.")).toBeInTheDocument();
  });

  it("explains each missing PAPER composition requirement in actionable language", async () => {
    vi.mocked(api.getPaperExecutionStatus).mockResolvedValue({
      configured: true,
      runtime_factory_registered: true,
      composition_ready: false,
      missing_requirements: [
        "strategy_session_builder",
        "attributable_control_authorizer",
        "account_reconciliation_facts",
        "market_data_verified_contracts",
        "risk_cost_facts",
      ],
      phase: "NOT_CONFIGURED",
      control_state: null,
      dispatch_gate_open: false,
      owner_lease_held: false,
      reason: "runtime_composition_incomplete",
      last_error_code: null,
    });
    renderExecution();

    expect(await screen.findByRole("heading", { name: "What is needed to complete PAPER readiness" })).toBeInTheDocument();
    expect(screen.getByText("A session builder that loads the approved, immutable strategy version.")).toBeInTheDocument();
    expect(screen.getByText("An operator authorization linked to an explicit PAPER mandate.")).toBeInTheDocument();
    expect(screen.getByText("Broker account identity, positions, open orders, and reconciliation evidence.")).toBeInTheDocument();
    expect(screen.getByText("Verified instrument and market-data contracts with freshness and session checks.")).toBeInTheDocument();
    expect(screen.getByText("Pre-trade risk limits, position-sizing inputs, and commission and slippage estimates.")).toBeInTheDocument();
  });

  it("shows the latest PAPER runtime error code", async () => {
    vi.mocked(api.getPaperExecutionStatus).mockResolvedValue({
      configured: true,
      runtime_factory_registered: true,
      composition_ready: true,
      missing_requirements: [],
      phase: "ERROR",
      control_state: "PAUSED",
      dispatch_gate_open: false,
      owner_lease_held: true,
      reason: "managed_python_strategy_failure",
      last_error_code: "managed_python_session_failed",
    });
    renderExecution();

    expect(await screen.findByText("Last runtime error code")).toBeInTheDocument();
    expect(screen.getByText("managed_python_session_failed")).toBeInTheDocument();
  });

  it("refreshes PAPER runtime status and shows a later error without reloading", async () => {
    vi.useFakeTimers();
    vi.mocked(api.getPaperExecutionStatus)
      .mockResolvedValueOnce({
        configured: true,
        runtime_factory_registered: true,
        composition_ready: true,
        missing_requirements: [],
        phase: "READY",
        control_state: "PAUSED",
        dispatch_gate_open: false,
        owner_lease_held: true,
        reason: null,
        last_error_code: null,
      })
      .mockResolvedValueOnce({
        configured: true,
        runtime_factory_registered: true,
        composition_ready: true,
        missing_requirements: [],
        phase: "ERROR",
        control_state: "PAUSED",
        dispatch_gate_open: false,
        owner_lease_held: true,
        reason: "managed_python_strategy_failure",
        last_error_code: "managed_python_session_failed",
      });

    renderExecution();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByText("managed_python_session_failed")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });

    expect(screen.getByText("managed_python_session_failed")).toBeInTheDocument();
    expect(api.getPaperExecutionStatus).toHaveBeenCalledTimes(2);
  });

  it("offers only a risk-reducing pause for an active paper runtime", async () => {
    vi.mocked(api.getPaperExecutionStatus).mockResolvedValue({
      configured: true,
      runtime_factory_registered: true,
      composition_ready: true,
      missing_requirements: [],
      phase: "READY",
      control_state: "RUNNING",
      dispatch_gate_open: true,
      owner_lease_held: true,
      reason: null,
      last_error_code: null,
    });
    vi.mocked(api.pausePaperExecution).mockResolvedValue({
      configured: true,
      runtime_factory_registered: true,
      composition_ready: true,
      missing_requirements: [],
      phase: "READY",
      control_state: "PAUSED",
      dispatch_gate_open: false,
      owner_lease_held: true,
      reason: "authenticated_operator_pause",
      last_error_code: null,
    });
    renderExecution();

    const pauseButton = await screen.findByRole("button", { name: "Pause new orders" });
    expect(screen.getByText(/does not cancel open orders/)).toBeInTheDocument();
    fireEvent.click(pauseButton);

    await waitFor(() => expect(api.pausePaperExecution).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Runtime is paused. The dispatch gate is closed./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause new orders" })).not.toBeInTheDocument();
  });

  it("runs the trusted template and renders the persisted report summary", async () => {
    vi.mocked(api.previewExecutionBacktest).mockResolvedValue({
      supported: true,
      review_hash: "a".repeat(64),
      preview_token: "1700000000." + "b".repeat(32) + "." + "c".repeat(64),
      template_source_sha256: "d".repeat(64),
      compiler_version: "vibe-trusted-sma/1.0",
      strategy_spec: { parameters: { fast_period: 10, slow_period: 30 } },
      summary: {
        symbol: "AAPL", instrument_id: "AAPL.US", source: "yfinance", start_date: "2025-01-01", end_date: "2025-12-31",
        allocation_fraction: 0.95, market_data_sha256: "e".repeat(64), data_manifest_sha256: "f".repeat(64),
      },
      warnings: ["Provider claims remain unverified."],
    });
    vi.mocked(api.createExecutionBacktest).mockResolvedValue({
      run_id: "bt_20261020_120000_abcdef123456",
      status: "success",
      execution_report: {
        engine: "Backtrader 1.9.78.123 / BackBroker",
        execution_mode: "backtest",
        strategy_version_hash: "strategy-version-1234567890",
        data_manifest: { synthetic: false, provider: "yfinance", range_start: "2025-01-01", range_end: "2025-12-31" },
        equity_curve: [{}, {}, {}],
        final_value: 101250,
        total_fees: 12.5,
        metrics: { total_return_fraction: 0.0125 },
      },
    });
    renderExecution();

    fireEvent.click(screen.getByRole("button", { name: "Review strategy" }));

    await waitFor(() => expect(api.previewExecutionBacktest).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "AAPL",
      instrument_id: "AAPL.US",
      strategy_description: "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA.",
    })));
    expect(api.createExecutionBacktest).not.toHaveBeenCalled();
    expect(await screen.findByText("Review this strategy")).toBeInTheDocument();
    expect(screen.getByText("Trusted template parameters")).toBeInTheDocument();
    expect(screen.getByText(/Fast SMA: 10 days · Slow SMA: 30 days/)).toBeInTheDocument();
    expect(screen.getByText("e".repeat(64))).toBeInTheDocument();
    expect(screen.getByText("f".repeat(64))).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run offline backtest" }));

    await waitFor(() => expect(api.createExecutionBacktest).toHaveBeenCalledWith(expect.objectContaining({
      symbol: "AAPL",
      instrument_id: "AAPL.US",
      source: "yfinance",
      strategy_description: "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA.",
      allocation_fraction: 0.95,
      preview_token: "1700000000." + "b".repeat(32) + "." + "c".repeat(64),
    })));
    expect(await screen.findByText("Saved Backtrader report")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /bt_20261020_120000_abcdef123456/ })).toHaveAttribute(
      "href",
      "/runs/bt_20261020_120000_abcdef123456?view=dashboard",
    );
    expect(screen.getByText("101250.00")).toBeInTheDocument();
    expect(screen.getByText("1.25%")).toBeInTheDocument();
    expect(screen.getByText("12.50")).toBeInTheDocument();
    expect(screen.getByText("3 bars · yfinance")).toBeInTheDocument();
    expect(screen.getByText("backtest · offline")).toBeInTheDocument();
    expect(screen.getByText("strategy-ver…")).toBeInTheDocument();
    expect(screen.getByText("NOT_CONFIGURED")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Download Python callback template" })).toHaveAttribute("download", "vibe_strategy_template.py");
  });

  it("shows the explicit RSI template parameters and threshold semantics in review", async () => {
    vi.mocked(api.previewExecutionBacktest).mockResolvedValue({
      supported: true,
      review_hash: "a".repeat(64),
      preview_token: "rsi-preview-token-12345678901234567890123456789012",
      template_source_sha256: "d".repeat(64),
      compiler_version: "vibe-trusted-templates/2.0",
      strategy_spec: {
        template_id: "rsi_threshold",
        parameters: { period: 14, entry_below: 30, exit_above: 70 },
        position_policy: { mode: "target_fraction", long_only: true, whole_shares: true, initial_entry: "wait_for_cross" },
        execution_policy: { decision: "completed_bar_available", dispatch: "next_regular_session", order_type: "market", time_in_force: "DAY", outside_rth: false, gap_action: "skip", missed_session_action: "expire" },
      },
      summary: { symbol: "AAPL", instrument_id: "AAPL.US", source: "yfinance", start_date: "2025-01-01", end_date: "2025-12-31", allocation_fraction: 0.95 },
      warnings: [],
    });
    renderExecution();

    fireEvent.change(screen.getByRole("textbox", { name: "Describe the strategy" }), {
      target: { value: "Buy when 14-period RSI crosses below 30; exit when RSI crosses above 70." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Review strategy" }));

    expect(await screen.findByText(/RSI\(14\) · entry < 30 · exit > 70/)).toBeInTheDocument();
    expect(screen.getByText(/Buy only when RSI crosses strictly below/)).toBeInTheDocument();
    expect(screen.getByText(/Exit only when RSI crosses strictly above/)).toBeInTheDocument();
    expect(screen.getByText("Supported strategy template. Check these rules and inputs before running the offline backtest.")).toBeInTheDocument();
    expect(screen.queryByText(/Supported SMA template/)).not.toBeInTheDocument();
  });

  it("shows unknown report identity instead of fixture defaults when a saved report omits fields", async () => {
    vi.mocked(api.previewExecutionBacktest).mockResolvedValue({
      supported: true,
      review_hash: "unknown-fields-review",
      preview_token: "unknown-fields-preview-token-123456789012345678901234",
      strategy_spec: { parameters: { fast_period: 10, slow_period: 30 } },
      summary: { symbol: "AAPL", instrument_id: "AAPL.US", source: "yfinance", allocation_fraction: 0.95 },
      warnings: [],
    });
    vi.mocked(api.createExecutionBacktest).mockResolvedValue({
      run_id: "bt_missing_report_identity",
      status: "success",
      execution_report: { final_value: 1000, metrics: { total_return_fraction: 0 } },
    });
    renderExecution();

    fireEvent.click(screen.getByRole("button", { name: "Review strategy" }));
    await screen.findByText("Review this strategy");
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run offline backtest" }));

    await screen.findByText("Saved Backtrader report");
    const unknownValues = screen.getAllByText("Unknown");
    expect(unknownValues.length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText("sma_cross · 1.0")).not.toBeInTheDocument();
    expect(screen.queryByText("Backtrader 1.9.78.123")).not.toBeInTheDocument();
  });

  it("collects a user clarification, keeps it in the transcript, then confirms the exact resulting preview", async () => {
    vi.mocked(api.previewExecutionBacktest)
      .mockResolvedValueOnce({
        supported: false,
        clarification: "Need explicit moving-average periods.",
        clarification_transcript: {
          transcript_id: "clarification-transcript-1",
          turn_id: "clarification-turn-1",
          turn_count: 1,
        },
        clarification_form: {
          schema_version: "strategy-clarification/1",
          status: "needs_clarification",
          question: "What periods should the two moving averages use?",
          missing_fields: [{ name: "fast_period", label: "Fast average period" }, { name: "slow_period", label: "Slow average period" }],
        },
      })
      .mockResolvedValueOnce({
        supported: true,
        review_hash: "clarified-review",
        preview_token: "clarified-preview-token-12345678901234567890123456789012",
        strategy_spec: { parameters: { fast_period: 8, slow_period: 21 } },
        summary: { symbol: "AAPL", instrument_id: "AAPL.US", source: "yfinance", start_date: "2025-01-01", end_date: "2025-12-31", allocation_fraction: 0.95 },
        warnings: [],
      });
    vi.mocked(api.createExecutionBacktest).mockResolvedValue({ run_id: "run-clarified", status: "success", execution_report: {} });
    renderExecution();

    fireEvent.click(screen.getByRole("button", { name: "Review strategy" }));
    expect(await screen.findByText("What periods should the two moving averages use?")).toBeInTheDocument();
    expect(screen.getByText("Fast average period")).toBeInTheDocument();
    expect(screen.getByText("Slow average period")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Your clarification" }), { target: { value: "Use an 8-day fast average and a 21-day slow average." } });
    fireEvent.click(screen.getByRole("button", { name: "Continue preview" }));

    const transcript = "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA.\n\nUser clarification: Use an 8-day fast average and a 21-day slow average.";
    await waitFor(() => expect(api.previewExecutionBacktest).toHaveBeenNthCalledWith(2, expect.objectContaining({
      strategy_description: transcript,
      clarification_transcript_id: "clarification-transcript-1",
      clarification_parent_turn_id: "clarification-turn-1",
      clarification_turn_id: expect.any(String),
      clarification_answer: "Use an 8-day fast average and a 21-day slow average.",
    })));
    expect(await screen.findByText("Trusted template parameters")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run offline backtest" }));
    await waitFor(() => expect(api.createExecutionBacktest).toHaveBeenCalledWith(expect.objectContaining({
      strategy_description: transcript,
      preview_token: "clarified-preview-token-12345678901234567890123456789012",
    })));
  });

  it("discards an outdated NL preview when the user edits its inputs while the request is pending", async () => {
    let resolvePreview!: (value: Awaited<ReturnType<typeof api.previewExecutionBacktest>>) => void;
    vi.mocked(api.previewExecutionBacktest).mockReturnValueOnce(new Promise((resolve) => { resolvePreview = resolve; }));
    renderExecution();
    fireEvent.click(screen.getByRole("button", { name: "Review strategy" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Describe the strategy" }), { target: { value: "A newer strategy description" } });
    await act(async () => resolvePreview({ supported: false, clarification: "Stale preview response" }));
    expect(screen.queryByText("Stale preview response")).not.toBeInTheDocument();
    expect(screen.queryByText("Review this strategy")).not.toBeInTheDocument();
  });

  it("uploads Python, completed-bar CSV, and a separate schedule to the isolated endpoint", async () => {
    vi.mocked(api.previewPythonUploadBacktest).mockResolvedValue({
      supported: true,
      review_hash: "review-hash-123",
      preview_token: "python-preview-token-12345678901234567890123456789012",
      backend: "backtrader",
      mode: "backtest",
      engine_version: "backtrader/1.9.78.123",
      compiler_version: "vibe-python-event/1",
      worker_image: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      source_sha256: "source-hash",
      market_data_sha256: "market-hash",
      data_manifest_hash: "manifest-hash",
      summary: {
        strategy_id: "custom-python-strategy", instrument_id: "AAPL.US", provider: "caller_uploaded_data",
        bar_count: 1, range_start: "2025-02-03", range_end: "2025-02-03", calendar: "us_equity_rth@caller-v1",
        initial_cash: 100000, commission_rate: 0.001, slippage_fraction: 0.005,
      },
      warnings: ["Provider labels are caller-declared."],
    });
    vi.mocked(api.createPythonUploadBacktest).mockResolvedValue({
      run_id: "btpy_20261020_120000_abcdef123456",
      status: "success",
      execution_report: {
        engine: "Backtrader 1.9.78.123 / BackBroker",
        execution_mode: "backtest",
        strategy_version_hash: "frozen-hash-123456",
        final_value: 101000,
        total_fees: 10,
        metrics: { total_return_fraction: 0.01 },
        data_manifest: { synthetic: false, provider: "caller_uploaded_data" },
        equity_curve: [{}, {}, {}],
        execution_integrity: { status: "verified" },
      },
    });
    renderExecution();
    const sourceText = "def on_event(event, state):\n    return {'state': state, 'order_proposals': []}\n";
    const csvText = "session_date,start_at,end_at,available_at,open,high,low,close,volume\n";
    const scheduleText = JSON.stringify([{ session_date: "2025-02-03", open_at: "2025-02-03T14:30:00Z", close_at: "2025-02-03T21:00:00Z" }]);
    const withText = (file: File, content: string) => {
      Object.defineProperty(file, "text", { value: () => Promise.resolve(content) });
      return file;
    };
    const source = withText(new File([sourceText], "strategy.py", { type: "text/x-python" }), sourceText);
    const csv = withText(new File([csvText], "bars.csv", { type: "text/csv" }), csvText);
    const schedule = withText(new File([scheduleText], "sessions.json", { type: "application/json" }), scheduleText);
    fireEvent.change(screen.getByLabelText("Strategy .py file"), { target: { files: [source] } });
    fireEvent.change(screen.getByLabelText("Completed daily bars CSV"), { target: { files: [csv] } });
    fireEvent.change(screen.getByLabelText("Separate session schedule JSON"), { target: { files: [schedule] } });
    fireEvent.submit(screen.getByRole("button", { name: "Validate and review upload" }).closest("form")!);

    await waitFor(() => expect(api.previewPythonUploadBacktest).toHaveBeenCalledWith(expect.objectContaining({
      strategy_id: "custom-python-strategy",
      instrument_id: "AAPL.US",
      strategy_source: expect.stringContaining("def on_event"),
      market_data_csv: expect.stringContaining("session_date,start_at"),
      expected_sessions: [expect.objectContaining({ session_date: "2025-02-03" })],
      slippage_fraction: 0,
      market_data_metadata: expect.objectContaining({
        calendar_id: "us_equity_rth",
        instrument_id: "AAPL.US",
        adjustment_policy: "caller_declared_unadjusted",
      }),
    })));
    expect(api.createPythonUploadBacktest).not.toHaveBeenCalled();
    expect(await screen.findByText("Review the exact upload")).toBeInTheDocument();
    expect(screen.getByText("source-hash")).toBeInTheDocument();
    expect(screen.getByText("market-hash")).toBeInTheDocument();
    expect(screen.getByText("sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("0.500%")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run Backtrader" }));

    await waitFor(() => expect(api.createPythonUploadBacktest).toHaveBeenCalledWith(expect.objectContaining({
      strategy_id: "custom-python-strategy",
      preview_token: "python-preview-token-12345678901234567890123456789012",
    })));
    expect(await screen.findByText("Saved Python Backtrader report")).toBeInTheDocument();
    expect(screen.getByText(/Immutable strategy version: frozen-hash/)).toBeInTheDocument();
    expect(screen.getByText("101000.00")).toBeInTheDocument();
    expect(screen.getByText("1.00%")).toBeInTheDocument();
    expect(screen.getByText("10.00")).toBeInTheDocument();
    expect(screen.getByText("3 bars · caller_uploaded_data")).toBeInTheDocument();
    expect(screen.getByText("Independent report replay: Verified")).toBeInTheDocument();
  });

  it("shows the normalized bar and source-line details when an uploaded strategy fails", async () => {
    vi.mocked(api.previewPythonUploadBacktest).mockResolvedValue({
      supported: true, review_hash: "review", preview_token: "python-preview-token-12345678901234567890123456789012",
      backend: "backtrader", mode: "backtest", engine_version: "backtrader/1.9.78.123",
      compiler_version: "vibe-python-event/1", source_sha256: "source", market_data_sha256: "market",
      data_manifest_hash: "manifest", summary: { strategy_id: "strategy", instrument_id: "AAPL.US", provider: "fixture_vendor", bar_count: 1, range_start: "2025-02-03", range_end: "2025-02-03", calendar: "fixture@1", initial_cash: 1000, commission_rate: 0.001, slippage_fraction: 0 }, warnings: [],
    });
    vi.mocked(api.createPythonUploadBacktest).mockRejectedValue(
      new Error("Bar 1: Strategy code raised an error (NameError) on source line 3"),
    );
    renderExecution();
    const withText = (file: File, content: string) => {
      Object.defineProperty(file, "text", { value: () => Promise.resolve(content) });
      return file;
    };
    const sourceText = "def on_event(event, state):\n    raise NameError()\n";
    fireEvent.change(screen.getByLabelText("Strategy .py file"), { target: { files: [withText(new File([sourceText], "strategy.py"), sourceText)] } });
    fireEvent.change(screen.getByLabelText("Completed daily bars CSV"), { target: { files: [withText(new File(["fixture"], "bars.csv"), "fixture")] } });
    const scheduleText = JSON.stringify([{ session_date: "2025-02-03", open_at: "2025-02-03T14:30:00Z", close_at: "2025-02-03T21:00:00Z" }]);
    fireEvent.change(screen.getByLabelText("Separate session schedule JSON"), { target: { files: [withText(new File([scheduleText], "sessions.json"), scheduleText)] } });
    fireEvent.submit(screen.getByRole("button", { name: "Validate and review upload" }).closest("form")!);
    await screen.findByText("Review the exact upload");
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run Backtrader" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Backtest failed: Bar 1: Strategy code raised an error (NameError) on source line 3",
    );
  });
});
