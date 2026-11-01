import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { AlertTriangle, ArrowDownRight, ArrowRight, Check, Database, FlaskConical, LoaderCircle, ShieldAlert } from "lucide-react";
import { Link } from "react-router";
import { cn } from "@/lib/utils";
import { api, ApiError, type PaperIntegrationTestResult, type PaperIntegrationTestStatus, type PaperStrategyCandidatesResponse } from "@/lib/api";
import { useSSE, type SSEStatus } from "@/hooks/useSSE";

const copy = {
  en: {
    eyebrow: "Execution lab / offline",
    title: "Backtrader, one bar at a time.",
    intro: "Run a trusted SMA or RSI template with provider history or the selected local IBKR read-only connection. Backtests never start a broker strategy or place orders.",
    engine: "Execution backend",
    version: "Strategy version",
    mode: "Run mode",
    dataset: "Data source",
    backendValue: "Backtrader 1.9.78.123",
    unknown: "Unknown",
    versionValue: "sma_cross · 1.0",
    modeValue: "Offline / synthetic",
    dataValue: "12 synthetic daily bars",
    reportEyebrow: "Verified fixture · not a saved user run",
    reportTitle: "SMA cross sample",
    reportDescription: "A real Cerebro + BackBroker run over a deterministic synthetic series.",
    orders: "Native orders",
    fills: "Completed fills",
    residual: "End position",
    orderValue: "3 · BUY / SELL / BUY",
    fillValue: "3",
    residualValue: "Open position reported",
    sizing: "First target: 3,960 shares · $40,000 strategy budget",
    fees: "BackBroker percentage commission · 0.1% · $25 fee reserve",
    traceTitle: "Execution trace",
    traceDecision: "Completed bar",
    traceDecisionDetail: "SMA relationship evaluated after the bar is complete",
    traceIntent: "Order intent",
    traceIntentDetail: "Whole-share target sized once with a price buffer",
    traceFill: "Next bar open",
    traceFillDetail: "BackBroker fill and commission captured in report",
    boundaryTitle: "Current operating boundary",
    boundaryIntro: "Offline backtests and the configured single-order PAPER workflow are available within the boundaries below.",
    available: "Verified offline slice",
    availableDetail: "Trusted SMA template · synthetic fixture plus user-created provider-backed backtests, with provenance limits shown in each report.",
    python: "Uploaded Python",
    pythonDetail: "Uploaded strategies can be frozen and backtested offline. A Python version with a verified IBKR replay that proves the exact authorized AAPL order can be selected for the one-shot PAPER flow; broker-side execution is a separate operator action.",
    pythonUploadTitle: "Run an isolated Python upload",
    pythonUploadDescription: "Upload a Python file, completed-bar CSV, and session schedule JSON. Freeze quantity and MARKET/LIMIT policy with the Backtrader report. A compatible IBKR PAPER report can then be selected for the fixed one-shot AAPL test.",
    pythonPositionQuantity: "Explicit whole-share quantity",
    pythonOrderType: "Backtest order type",
    pythonLimitPrice: "Limit price (USD)",
    downloadTemplate: "Download Python callback template",
    downloadPaperExample: "Download one-shot PAPER example",
    pythonSourceFile: "Strategy .py file",
    marketCsvFile: "Completed daily bars CSV",
    scheduleFile: "Separate session schedule JSON",
    ibkrSymbol: "IBKR symbol",
    ibkrProfile: "IBKR read-only profile",
    ibkrPort: "Local API socket port",
    fetchIbkrBars: "Fetch daily bars from IBKR",
    fetchingIbkrBars: "Fetching IBKR history…",
    ibkrFetched: "IBKR bars loaded; review the exact data before running.",
    ibkrPaperProofReady: "server-verified PAPER source proof attached",
    ibkrPaperProof: "IBKR PAPER source proof",
    ibkrPaperProofYes: "Verified for this exact CSV and session schedule",
    ibkrPaperProofNo: "No server-verified PAPER source proof",
    strategyId: "Strategy ID",
    timezone: "IANA timezone",
    calendarId: "Calendar ID",
    calendarVersion: "Calendar version",
    provider: "Provider label",
    currency: "Currency",
    availability: "Availability model",
    adjustment: "Adjustment policy",
    corporateAction: "Corporate-action policy",
    warmupStart: "Warm-up start",
    usePaperDates: "Use selected PAPER backtest dates",
    usePaperDatesHint: "Uses the selected report's evaluation day and the previous XNYS session. Confirm that the evaluation day is still the latest completed session, then fetch bars again.",
    warmupEnd: "Warm-up end",
    evaluationStart: "Evaluation start",
    evaluationEnd: "Evaluation end",
    initialCash: "Initial cash (USD)",
    commissionRate: "Commission rate",
    slippageFraction: "Slippage fraction",
    pythonRun: "Run Python backtest",
    pythonRunning: "Validating bars and running…",
    pythonReviewButton: "Validate and review upload",
    pythonReviewing: "Validating source and bars…",
    pythonReviewTitle: "Review the exact upload",
    pythonReviewDescription: "Confirm these frozen inputs before the isolated Backtrader run starts.",
    confirmPythonUpload: "Confirm and run Backtrader",
    editPythonUpload: "Edit upload inputs",
    sourceHash: "Source SHA-256",
    marketHash: "Market CSV SHA-256",
    manifestHash: "Data manifest SHA-256",
    barCount: "Completed bars",
    dataRange: "Imported data range",
    pythonEngine: "Engine / worker protocol",
    workerImage: "Digest-pinned worker image",
    pythonReviewScope: "Review hash",
    pythonWorkerMissing: "The isolated Python worker is not configured; this upload cannot run yet.",
    pythonRunHint: "For the current PAPER flow use instrument AAPL, quantity 1, LIMIT $400, then fetch bars with the IBKR PAPER button. The server binds a short-lived proof to that exact CSV and session schedule; provider labels entered by hand cannot qualify a version. A compatible report must emit exactly one unfilled BUY 1 AAPL limit order.",
    pythonNoFiles: "Choose all three files and provide the metadata before running.",
    pythonRunSuccess: "Saved Python Backtrader report",
    pythonVersion: "Immutable strategy version",
    realData: "Real market data",
    realDataDetail: "IBKR PAPER imports bind the fetched bars and XNYS schedule to server-observed provenance. Publication timing and adjustment semantics remain unverified; other imports retain their declared provenance.",
    broker: "IBKR paper",
    brokerDetail: "The managed IBKR PAPER flow is available for the configured one-shot order. Selecting a version does not connect to the broker or place an order.",
    paperChecking: "Checking the managed PAPER runtime status…",
    paperStatusUnavailable: "Runtime status could not be loaded. No broker connection was started.",
    paperNotConfigured: "No trusted runtime composition is configured. No account state or broker orders are exposed.",
    paperCompositionIncomplete: "A runtime factory is registered, but trusted strategy, authorization, account, market-data, or risk wiring is incomplete.",
    paperStopped: "Runtime composition is configured but stopped. The dispatch gate is closed.",
    paperPaused: "Runtime is paused. The dispatch gate is closed.",
    paperRunning: "Runtime is active and its dispatch gate is open.",
    paperStartButton: "Connect and reconcile (paused)",
    paperStartHint: "Connects the pinned PAPER strategy and reconciles account facts. Dispatch stays paused.",
    paperActivateButton: "Enable PAPER strategy",
    paperActivateHint: "Allows this frozen strategy to submit orders to the IBKR PAPER account.",
    paperControlBusy: "Updating PAPER runtime…",
    paperControlFailure: "PAPER control did not complete. Refresh status before retrying.",
    paperPauseButton: "Pause new orders",
    paperPauseHint: "Keeps broker callbacks and reconciliation running; does not cancel open orders.",
    paperPauseFailure: "The runtime did not confirm a durable pause. Check status before retrying.",
    paperIntegrationTitle: "Single-order PAPER check",
    paperUserGuide: "Open PAPER user guide (Chinese)",
    paperSignalDateHint: "At execution, the backtest evaluation date must match the latest completed IBKR session. If it has advanced, fetch bars, backtest, and select a new version.",
    paperCandidatesTitle: "Frozen Backtest Versions",
    paperCandidatesDescription: "Select a frozen Python strategy only when its verified IBKR Backtrader run proves the exact AAPL BUY 1 LMT $400 order. Selection does not submit an order.",
    paperCandidateReady: "Current fixed PAPER version",
    paperCandidateSelectable: "Compatible with this PAPER order",
    paperCandidateBacktestOnly: "Verified backtest · no PAPER deployment",
    paperCandidateSelect: "Use for this PAPER check",
    paperCandidateSelecting: "Updating strategy…",
    paperCandidateSelectError: "Strategy selection failed. The prior PAPER deployment was preserved.",
    paperCandidatesEmpty: "No successful frozen Backtrader reports were found.",
    paperIntegrationOrder: "AAPL · BUY 1 · LMT $400 · DAY · regular hours only",
    paperIntegrationBacktestVerified: "Frozen version has a verified Backtrader report",
    paperIntegrationBacktestBlocked: "A matching verified Backtrader report is required before PAPER use",
    paperIntegrationWindow: "Allowed window (UTC)",
    paperIntegrationWindowClosed: "This order is locked until the configured session window opens.",
    paperIntegrationWindowOpen: "The configured order window is open.",
    paperIntegrationDisabled: "This one-shot order is disabled by its local configuration.",
    paperIntegrationButton: "Run the one-shot PAPER order",
    paperIntegrationRunning: "Submitting the configured PAPER check…",
    paperIntegrationError: "The one-shot action did not complete. Refresh the runtime and activity before retrying.",
    paperIntegrationVerified: "A fresh broker order and position reconciliation completed.",
    paperIntegrationReconcileFailed: "The broker snapshot did not reconcile. The dispatch gate is paused; review activity before continuing.",
    paperIntegrationOrderSeen: "Broker order activity was recorded.",
    paperIntegrationNoOrder: "No broker order was observed during the response window.",
    paperActivityTitle: "Persisted PAPER activity",
    paperActivityStopped: "Runtime stopped: showing saved order history. Reconnect and reconcile to see current broker positions.",
    paperActivityConnecting: "Connecting live activity updates…",
    paperActivityLive: "Live activity updates connected",
    paperActivityStale: "Live updates interrupted; reconnecting. Periodic snapshots continue.",
    paperActivityUnavailable: "No managed runtime activity is available. This view does not start a broker connection.",
    paperOrders: "Recent orders",
    paperNoOrders: "No persisted orders in this scope.",
    paperOrder: "Order",
    paperBrokerOrderId: "Broker order ID",
    paperExecutionId: "Execution ID",
    paperCancelOrder: "Cancel this PAPER order",
    paperCancelling: "Sending cancellation…",
    paperCancelSent: "Cancellation requested; check the broker status for confirmation.",
    paperCancelError: "Cancellation was not confirmed. Check the order and your cancel grant before retrying.",
    paperFilled: "filled",
    paperRemaining: "remaining",
    paperPositionTitle: "Broker positions",
    paperPositionUnknown: "Unknown: no fresh authoritative broker snapshot is available.",
    paperPositionAsOf: "Reconciled snapshot",
    paperRiskTitle: "Risk facts",
    paperRiskLedgerOnly: "Ledger reservations only; current risk limits are unavailable.",
    paperReservedCash: "Reserved cash",
    paperReservedShares: "Reserved sell shares",
    paperRecoveryTitle: "Recovery queue",
    paperUnresolved: "Unresolved sends",
    paperActiveOrders: "Active orders",
    paperFeesUnconfirmed: "Reported fees · unconfirmed",
    warning: "Synthetic evidence only",
    warningDetail: "This fixture validates engine timing, sizing, fills, fees, cash, and a residual position. It does not establish market-data accuracy, strategy performance, or broker readiness.",
    createTitle: "Run a trusted SMA or RSI template",
    createDescription: "Fetch daily bars from one provider and execute a versioned SMA or RSI template with Cerebro + BackBroker.",
    symbol: "Market symbol",
    instrument: "Instrument ID",
    source: "Data provider",
    ibkrSource: "IBKR local API (selected read-only profile)",
    ibkrPaperProfile: "IBKR PAPER local session",
    ibkrLiveProfile: "IBKR LIVE local session · read-only",
    startDate: "Start date",
    endDate: "End date",
    strategy: "Describe the strategy",
    strategyHint: "Describe the rule in your own words. Supported templates are long-only SMA crosses or explicit-period RSI threshold crosses; extra filters are rejected.",
    allocation: "Allocation",
    run: "Review strategy",
    reviewTitle: "Review this strategy",
    reviewAccepted: "Supported strategy template. Check these rules and inputs before running the offline backtest.",
    reviewUnsupported: "This request does not match the supported template.",
    clarification: "Please describe both SMA periods and the buy-above / sell-below cross rules.",
    missingDetails: "Still needed",
    yourClarification: "Your clarification",
    clarificationPlaceholder: "Add the missing strategy details in your own words.",
    transcriptSaved: "Server-saved clarification turns",
    continueClarification: "Continue preview",
    confirmRun: "Confirm and run offline backtest",
    editInputs: "Edit inputs",
    fastPeriod: "Fast SMA",
    slowPeriod: "Slow SMA",
    templateParameters: "Trusted template parameters",
    rsiBuy: "Buy only when RSI crosses strictly below the entry threshold; the initial sample never triggers an entry.",
    rsiSell: "Exit only when RSI crosses strictly above the exit threshold; threshold equality does not trigger.",
    ruleBuy: "Buy when the fast SMA crosses above the slow SMA.",
    ruleSell: "Sell when the fast SMA crosses below the slow SMA.",
    reviewScope: "Review hash",
    reviewing: "Preparing rule preview…",
    positionPolicy: "Position policy",
    decisionTiming: "Decision and dispatch",
    orderPolicy: "Order and session policy",
    strategyBudget: "Strategy budget",
    feeReserve: "Fee reserve",
    priceBuffer: "Price buffer",
    commissionModel: "Commission rate",
    slippageModel: "Slippage fraction",
    initialCashLabel: "Initial cash",
    days: "days",
    targetFraction: "target fraction",
    longOnlyValue: "long only",
    wholeSharesValue: "whole shares",
    waitForCross: "wait for first cross",
    completedBar: "completed bar available",
    nextRegularSession: "next regular session",
    marketDay: "Market / DAY",
    outsideRthOff: "outside RTH off",
    gapSkip: "gap: skip",
    missedExpire: "missed session: expire",
    templateSource: "Template source SHA-256",
    compilerVersion: "Compiler version",
    running: "Fetching bars and running…",
    runSuccess: "Saved Backtrader report",
    runFailure: "Backtest failed",
    runIntro: "This run is provider-backed and remains unverified for source claims, point-in-time availability, calendar, adjustments, and symbol-to-instrument mapping.",
    finalValue: "Final account value",
    totalReturn: "Total return",
    feesValue: "Total fees",
    reportCheck: "Independent report replay",
    verified: "Verified",
    noExecution: "No user-created run yet. The synthetic fixture below remains available as a deterministic engine check.",
    required: "Enter a symbol, instrument ID, and valid date range.",
    openRun: "Open saved run",
  },
  zh: {
    eyebrow: "执行实验室 / 离线",
    title: "逐根 bar 验证 Backtrader。",
    intro: "使用行情 provider 或当前选择的 IBKR 本地只读连接，运行受信任的 SMA/RSI 模板。回测不会启动券商策略或下单。",
    engine: "执行后端",
    version: "策略版本",
    mode: "运行模式",
    dataset: "数据来源",
    backendValue: "Backtrader 1.9.78.123",
    unknown: "未知",
    versionValue: "sma_cross · 1.0",
    modeValue: "离线 / 合成数据",
    dataValue: "12 根合成日线",
    reportEyebrow: "已验证样例 · 不是已保存的用户回测",
    reportTitle: "SMA 交叉样例",
    reportDescription: "在确定性合成序列上运行真实 Cerebro + BackBroker。",
    orders: "原生订单",
    fills: "完成成交",
    residual: "期末持仓",
    orderValue: "3 笔 · 买入 / 卖出 / 买入",
    fillValue: "3 笔",
    residualValue: "报告包含未平仓持仓",
    sizing: "首次目标：3,960 股 · 策略预算 $40,000",
    fees: "BackBroker 百分比佣金 · 0.1% · $25 费用预留",
    traceTitle: "执行时序",
    traceDecision: "已完成的 bar",
    traceDecisionDetail: "bar 完成后计算 SMA 关系",
    traceIntent: "订单意图",
    traceIntentDetail: "整股目标只计算一次，并加入价格缓冲",
    traceFill: "下一根 bar 开盘",
    traceFillDetail: "报告记录 BackBroker 成交和佣金",
    boundaryTitle: "当前运行边界",
    boundaryIntro: "离线回测和配置范围内的 PAPER 单笔流程可按以下边界使用。",
    available: "已验证的离线切片",
    availableDetail: "受信任 SMA 模板 · 合成样例和用户创建的 provider 行情回测；每份报告都会显示来源限制。",
    python: "上传 Python",
    pythonDetail: "上传策略可冻结并离线回测。若 Python 版本的 IBKR 回测报告证明它会发出本次授权的 AAPL 订单，就可选入单笔 PAPER 流程；券商执行仍由操作员单独触发。",
    pythonUploadTitle: "运行隔离的 Python 上传策略",
    pythonUploadDescription: "上传 Python 文件、完整日线 CSV 和交易时段 JSON。在 Backtrader 报告中冻结股数及 MARKET/LIMIT 策略；符合条件的 IBKR PAPER 报告才可选入固定 AAPL 单笔联调。",
    pythonPositionQuantity: "明确整股数量",
    pythonOrderType: "回测订单类型",
    pythonLimitPrice: "限价（USD）",
    downloadTemplate: "下载 Python 回调模板",
    downloadPaperExample: "下载单笔 PAPER 联调示例",
    pythonSourceFile: "策略 .py 文件",
    marketCsvFile: "已完成日线 CSV",
    scheduleFile: "独立交易时段 JSON",
    ibkrSymbol: "IBKR 代码",
    ibkrProfile: "IBKR 只读连接配置",
    ibkrPort: "本地 API 端口",
    fetchIbkrBars: "从 IBKR 获取日线",
    fetchingIbkrBars: "正在获取 IBKR 历史行情…",
    ibkrFetched: "IBKR 日线已载入；运行前请检查冻结行情。",
    ibkrPaperProofReady: "已附加服务端 PAPER 来源凭证",
    ibkrPaperProof: "IBKR PAPER 来源凭证",
    ibkrPaperProofYes: "已绑定到这份 CSV 和交易日程",
    ibkrPaperProofNo: "没有服务端验证的 PAPER 来源凭证",
    strategyId: "策略 ID",
    timezone: "IANA 时区",
    calendarId: "日历 ID",
    calendarVersion: "日历版本",
    provider: "行情来源标签",
    currency: "货币",
    availability: "数据可用时间模型",
    adjustment: "复权规则",
    corporateAction: "公司行动规则",
    warmupStart: "暖机开始日期",
    usePaperDates: "使用当前 PAPER 回测日期",
    usePaperDatesHint: "填入已选报告的评估日和前一个 XNYS 交易日。请确认评估日仍为最新完整交易日，再重新获取日线。",
    warmupEnd: "暖机结束日期",
    evaluationStart: "评估开始日期",
    evaluationEnd: "评估结束日期",
    initialCash: "初始现金（USD）",
    commissionRate: "佣金费率",
    slippageFraction: "滑点比例",
    pythonRun: "运行 Python 回测",
    pythonRunning: "正在校验行情并运行…",
    pythonReviewButton: "校验并预览上传内容",
    pythonReviewing: "正在校验源码和行情…",
    pythonReviewTitle: "确认本次上传内容",
    pythonReviewDescription: "启动隔离的 Backtrader 回测前，确认这些将被冻结的输入。",
    confirmPythonUpload: "确认并运行 Backtrader",
    editPythonUpload: "修改上传内容",
    sourceHash: "源码 SHA-256",
    marketHash: "行情 CSV SHA-256",
    manifestHash: "数据清单 SHA-256",
    barCount: "完整日线数",
    dataRange: "导入数据范围",
    pythonEngine: "引擎 / worker 协议",
    workerImage: "固定摘要的 worker 镜像",
    pythonReviewScope: "预览校验值",
    pythonWorkerMissing: "隔离 Python worker 尚未配置，当前无法运行该上传策略。",
    pythonRunHint: "当前 PAPER 流程请使用标的 AAPL、数量 1、LIMIT $400，再通过 IBKR PAPER 按钮获取日线。服务端会把短时凭证绑定到这份 CSV 和交易日程；手动填写行情来源标签不能让版本获得部署资格。符合条件的报告须恰好产生一笔未成交 AAPL BUY 1 限价单。",
    pythonNoFiles: "请选择三个文件并填写所需元信息。",
    pythonRunSuccess: "已保存 Python Backtrader 报告",
    pythonVersion: "不可变策略版本",
    realData: "真实行情",
    realDataDetail: "IBKR PAPER 导入会绑定实际拉取的日线和 XNYS 交易日程；历史发布时间与复权语义仍未核验，其他导入数据沿用其声明的来源。",
    broker: "IBKR 模拟盘",
    brokerDetail: "已提供配置范围内的托管 IBKR PAPER 单笔流程。选择策略不会连接券商或发出订单。",
    paperChecking: "正在读取 PAPER runtime 状态…",
    paperStatusUnavailable: "无法读取 runtime 状态。本次没有启动券商连接。",
    paperNotConfigured: "尚未配置受信任的 runtime composition。页面不暴露账户状态或券商订单。",
    paperCompositionIncomplete: "Runtime factory 已注册，但策略、授权、账户、行情或风控接线尚不完整。",
    paperStopped: "Runtime composition 已配置但处于停止状态，派单闸门关闭。",
    paperPaused: "Runtime 已暂停，派单闸门关闭。",
    paperRunning: "Runtime 正在运行，派单闸门已打开。",
    paperStartButton: "连接并对账（保持暂停）",
    paperStartHint: "连接已冻结的 PAPER 策略并对账账户事实；此步骤不会开放派单。",
    paperActivateButton: "启用模拟策略",
    paperActivateHint: "允许这个已冻结策略向 IBKR PAPER 账户提交订单。",
    paperControlBusy: "正在更新 PAPER runtime…",
    paperControlFailure: "PAPER 控制操作未完成。请刷新状态后再决定是否重试。",
    paperPauseButton: "暂停新增派单",
    paperPauseHint: "继续处理券商回报与对账；不会撤销已有委托。",
    paperPauseFailure: "Runtime 未确认持久暂停。请先检查状态，再决定是否重试。",
    paperIntegrationTitle: "单笔 PAPER 联调",
    paperUserGuide: "打开 PAPER 使用手册",
    paperSignalDateHint: "执行时，回测评估日必须与 IBKR 最新完整交易日一致；若已变化，请重新拉取日线、回测并选择新版本。",
    paperCandidatesTitle: "已冻结的回测版本",
    paperCandidatesDescription: "只有冻结策略的 IBKR Backtrader 报告证明会发出本次限定的 AAPL 买入 1 股、$400 限价单时，才可选作联调版本。选择不会下单。",
    paperCandidateReady: "当前固定 PAPER 版本",
    paperCandidateSelectable: "符合本次 PAPER 订单限制",
    paperCandidateBacktestOnly: "回测已验证 · 暂无 PAPER 部署",
    paperCandidateSelect: "用于本次 PAPER 联调",
    paperCandidateSelecting: "正在切换策略…",
    paperCandidateSelectError: "策略切换失败，原 PAPER 部署保持不变。",
    paperCandidatesEmpty: "没有找到成功且完整性校验通过的冻结策略回测报告。",
    paperIntegrationOrder: "AAPL · 买入 1 股 · 限价 $400 · DAY · 仅常规交易时段",
    paperIntegrationBacktestVerified: "冻结版本已有匹配且复核通过的 Backtrader 报告",
    paperIntegrationBacktestBlocked: "缺少匹配且复核通过的 Backtrader 报告，暂不可进入 PAPER",
    paperIntegrationWindow: "允许时段（UTC）",
    paperIntegrationWindowClosed: "尚未到配置的交易时段，订单入口会保持关闭。",
    paperIntegrationWindowOpen: "当前处于已配置的下单时段。",
    paperIntegrationDisabled: "本机配置已禁用这笔一次性订单。",
    paperIntegrationButton: "执行单笔模拟盘验证",
    paperIntegrationRunning: "正在提交配置好的 PAPER 验证…",
    paperIntegrationError: "单笔操作未完成。重试前请刷新 runtime 和活动记录。",
    paperIntegrationVerified: "已完成最新券商订单与持仓对账。",
    paperIntegrationReconcileFailed: "券商快照对账未通过。派单闸门已暂停，请先检查活动记录。",
    paperIntegrationOrderSeen: "已记录券商订单回报。",
    paperIntegrationNoOrder: "等待窗口内没有观察到券商订单。",
    paperActivityTitle: "已持久化的 PAPER 活动",
    paperActivityStopped: "运行已停止：当前显示已保存的订单历史。请重新连接并对账以查看券商当前持仓。",
    paperActivityConnecting: "正在连接实时活动更新…",
    paperActivityLive: "实时活动更新已连接",
    paperActivityStale: "实时更新中断，正在重连；页面仍会定期刷新快照。",
    paperActivityUnavailable: "当前没有可读取的托管 runtime 活动。本视图不会启动券商连接。",
    paperOrders: "最近委托",
    paperNoOrders: "此 scope 下没有已持久化委托。",
    paperOrder: "委托",
    paperBrokerOrderId: "券商订单号",
    paperExecutionId: "成交编号",
    paperCancelOrder: "撤销这笔 PAPER 委托",
    paperCancelling: "正在发送撤单请求…",
    paperCancelSent: "撤单请求已发送；请查看券商状态确认结果。",
    paperCancelError: "撤单未确认，请先检查订单状态及撤单授权。",
    paperFilled: "已成交",
    paperRemaining: "剩余",
    paperPositionTitle: "券商持仓",
    paperPositionUnknown: "未知：当前没有新鲜且权威的券商对账快照。",
    paperPositionAsOf: "对账快照时间",
    paperRiskTitle: "风险事实",
    paperRiskLedgerOnly: "仅显示账本预留；当前风险限额不可用。",
    paperReservedCash: "预留现金",
    paperReservedShares: "预留卖出股数",
    paperRecoveryTitle: "恢复队列",
    paperUnresolved: "发送结果未决",
    paperActiveOrders: "活动委托",
    paperFeesUnconfirmed: "已报告费用 · 未确认",
    warning: "仅合成数据证据",
    warningDetail: "此样例验证引擎时序、数量、成交、费用、现金和期末未平仓持仓，不代表行情准确性、策略表现或券商接入就绪。",
    createTitle: "运行受信任的 SMA 或 RSI 模板",
    createDescription: "从一个行情 provider 获取日线数据，使用版本化 SMA 或 RSI 模板和 Cerebro + BackBroker 执行。",
    symbol: "行情代码",
    instrument: "Instrument ID",
    source: "数据 provider",
    ibkrSource: "IBKR 本地 API（当前选择的只读连接）",
    ibkrPaperProfile: "IBKR PAPER 本地会话",
    ibkrLiveProfile: "IBKR LIVE 本地会话 · 只读",
    startDate: "开始日期",
    endDate: "结束日期",
    strategy: "用自然语言描述策略",
    strategyHint: "用自己的话描述策略规则。当前支持双均线交叉或明确周期的 RSI 阈值交叉；额外条件会被拒绝。",
    allocation: "资金分配比例",
    run: "预览策略规则",
    reviewTitle: "请先审阅策略",
    reviewAccepted: "已匹配受支持的策略模板。确认以下规则和输入后，才会运行离线回测。",
    reviewUnsupported: "当前描述不符合受支持的策略模板。",
    clarification: "请同时说明两条 SMA 周期，以及上穿买入、下穿卖出的条件。",
    missingDetails: "仍需补充",
    yourClarification: "补充策略说明",
    clarificationPlaceholder: "请用自己的话补充缺少的策略细节。",
    transcriptSaved: "已保存到服务端的澄清轮次",
    continueClarification: "继续生成预览",
    confirmRun: "确认并运行离线回测",
    editInputs: "修改输入",
    fastPeriod: "短期 SMA",
    slowPeriod: "长期 SMA",
    templateParameters: "受信任模板参数",
    rsiBuy: "仅当 RSI 严格下穿入场阈值时买入；样本开始时已低于阈值不会追买。",
    rsiSell: "仅当 RSI 严格上穿退出阈值时平仓；等于阈值时不触发。",
    ruleBuy: "短期 SMA 上穿长期 SMA 时买入。",
    ruleSell: "短期 SMA 下穿长期 SMA 时卖出。",
    reviewScope: "预览校验值",
    reviewing: "正在生成规则预览…",
    positionPolicy: "仓位规则",
    decisionTiming: "决策与派单时点",
    orderPolicy: "订单与时段规则",
    strategyBudget: "策略预算",
    feeReserve: "费用预留",
    priceBuffer: "价格缓冲",
    commissionModel: "佣金费率",
    slippageModel: "滑点比例",
    initialCashLabel: "初始现金",
    days: "日",
    targetFraction: "按比例配置目标仓位",
    longOnlyValue: "只做多",
    wholeSharesValue: "整股",
    waitForCross: "等待首次上穿",
    completedBar: "完整日线可用后决策",
    nextRegularSession: "下一常规交易时段派单",
    marketDay: "市价单 / DAY",
    outsideRthOff: "不允许常规时段外交易",
    gapSkip: "跳空时跳过",
    missedExpire: "错过时段后过期",
    templateSource: "模板源码 SHA-256",
    compilerVersion: "编译器版本",
    running: "正在获取行情并执行…",
    runSuccess: "已保存 Backtrader 报告",
    runFailure: "回测失败",
    runIntro: "本次运行使用 provider 行情；来源声明、历史时点可用性、交易日历、复权规则以及代码到 instrument 的映射仍未核验。",
    finalValue: "期末账户价值",
    totalReturn: "总收益率",
    feesValue: "总费用",
    reportCheck: "独立报告复算",
    verified: "已通过",
    noExecution: "还没有用户创建的回测。下方合成样例仍可用于确定性引擎检查。",
    required: "请填写行情代码、Instrument ID 和有效日期区间。",
    openRun: "查看已保存的回测",
  },
} as const;

type Copy = (typeof copy)[keyof typeof copy];

const paperCandidateReasons: Record<string, [string, string]> = {
  source_kind_not_supported_by_fixed_python_upload_composition: ["Requires an uploaded Python strategy.", "需要上传 Python 策略。"],
  instrument_outside_fixed_AAPL_test_scope: ["Instrument must be AAPL.", "标的必须是 AAPL。"],
  bar_policy_outside_completed_AAPL_RTH_daily_scope: ["Requires completed regular-session AAPL daily bars.", "需要 AAPL 常规时段完整日线。"],
  execution_policy_outside_fixed_LMT_DAY_RTH_envelope: ["Execution must use LIMIT, DAY and regular trading hours.", "执行方式必须是限价、DAY、常规交易时段。"],
  position_sizing_outside_fixed_long_only_one_share_envelope: ["Position sizing must be long-only and one whole share.", "仓位须为只做多、整数 1 股。"],
  frozen_parameters_outside_fixed_LMT_400_order: ["Frozen parameters must specify 1 share at a $400 limit.", "冻结参数须为 1 股、限价 $400。"],
  backtest_data_lacks_server_verified_ibkr_paper_import: ["Fetch the backtest data through the IBKR PAPER import.", "请通过 IBKR PAPER 拉取回测数据。"],
  backtest_must_cover_one_aapl_evaluation_session: ["The AAPL evaluation must cover exactly one trading session.", "AAPL 评估区间必须恰好为一个交易日。"],
  backtest_must_emit_exactly_one_order: ["The backtest must produce exactly one order.", "回测必须恰好产生一笔订单。"],
  backtest_order_differs_from_fixed_AAPL_BUY_1_LMT_400: ["The unfilled order must be BUY 1 AAPL, limit $400.", "未成交订单必须为买入 1 股 AAPL、限价 $400。"],
  backtest_must_have_no_fills_before_paper_deployment: ["The backtest must have no fills before PAPER deployment.", "PAPER 部署前，回测不能有成交记录。"],
};

function EvidenceStat({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="min-w-0 border-s-2 border-warning/70 ps-3">
      <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
      <div className="mt-1 font-mono text-sm font-semibold tabular-nums text-foreground">{value}</div>
      {detail && <div className="mt-1 text-xs leading-relaxed text-muted-foreground">{detail}</div>}
    </div>
  );
}

function TraceStep({ title, detail, final = false }: { title: string; detail: string; final?: boolean }) {
  return (
    <div className="relative flex gap-3">
      <div className="flex flex-col items-center">
        <span className={cn("mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full border", final ? "border-warning/50 bg-warning/10 text-warning" : "border-border bg-background text-muted-foreground")}>
          {final ? <Check className="h-3.5 w-3.5" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />}
        </span>
        {!final && <span className="mt-1 h-full min-h-6 w-px bg-border" aria-hidden="true" />}
      </div>
      <div className="pb-5">
        <div className="text-sm font-medium">{title}</div>
        <p className="mt-1 max-w-md text-xs leading-relaxed text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}

function BoundaryRow({ title, detail, state, caution = false }: { title: string; detail: string; state: string; caution?: boolean }) {
  return (
    <div className="grid gap-2 border-t border-border/60 py-3 sm:grid-cols-[minmax(130px,0.7fr)_minmax(0,2fr)_auto] sm:items-start">
      <div className="text-sm font-medium">{title}</div>
      <div className="text-xs leading-relaxed text-muted-foreground">{detail}</div>
      <span className={cn("w-fit rounded-sm px-2 py-1 font-mono text-[10px] uppercase tracking-wide", caution ? "bg-warning/10 text-warning" : "bg-muted text-muted-foreground")}>{state}</span>
    </div>
  );
}

export function Execution() {
  const {
    connect: connectPaperActivity,
    disconnect: disconnectPaperActivity,
    onStatusChange: onPaperActivityStatusChange,
  } = useSSE();
  const refreshPaperActivityRef = useRef<() => void>(() => undefined);
  const paperActivityCursorRef = useRef(0);
  const activityRefreshTimerRef = useRef<number | null>(null);
  const previewGenerationRef = useRef(0);
  const pythonPreviewGenerationRef = useRef(0);
  const { i18n, t } = useTranslation();
  const text: Copy = i18n.resolvedLanguage?.startsWith("zh") ? copy.zh : copy.en;
  const [symbol, setSymbol] = useState("AAPL");
  const [instrumentId, setInstrumentId] = useState("AAPL.US");
  const [source, setSource] = useState<"yfinance" | "stooq" | "ibkr">("yfinance");
  const [ibkrProfileId, setIbkrProfileId] = useState<"ibkr-paper-local" | "ibkr-live-local-readonly">("ibkr-paper-local");
  const [ibkrPort, setIbkrPort] = useState<4001 | 4002 | 7496 | 7497>(4002);
  const [startDate, setStartDate] = useState(() => {
    const date = new Date();
    date.setFullYear(date.getFullYear() - 1);
    return date.toISOString().slice(0, 10);
  });
  const [endDate, setEndDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [strategyDescription, setStrategyDescription] = useState(
    () => i18n.resolvedLanguage?.startsWith("zh")
      ? "10日均线上穿30日均线买入，10日均线下穿30日均线卖出"
      : "Buy when 10-day SMA crosses above 30-day SMA; sell when 10-day SMA crosses below 30-day SMA.",
  );
  const [allocation, setAllocation] = useState(0.95);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [review, setReview] = useState<{
    payload: {
      symbol: string; instrument_id: string; start_date: string; end_date: string;
      source: "yfinance" | "stooq" | "ibkr"; ibkr_profile_id?: "ibkr-paper-local" | "ibkr-live-local-readonly"; strategy_description: string; allocation_fraction: number;
    };
    preview: Awaited<ReturnType<typeof api.previewExecutionBacktest>>;
    transcript?: Awaited<ReturnType<typeof api.previewExecutionBacktest>>["clarification_transcript"];
  } | null>(null);
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const [pendingClarification, setPendingClarification] = useState<{
    transcriptId: string; parentTurnId: string; turnId: string; answer: string;
  } | null>(null);
  const [result, setResult] = useState<{ run_id: string; execution_report: Record<string, unknown> } | null>(null);
  const [pythonStrategyId, setPythonStrategyId] = useState("custom-python-strategy");
  const [pythonInstrumentId, setPythonInstrumentId] = useState("AAPL");
  const [pythonIbkrSymbol, setPythonIbkrSymbol] = useState("AAPL");
  const [pythonIbkrProfileId, setPythonIbkrProfileId] = useState<"ibkr-paper-local" | "ibkr-live-local-readonly">("ibkr-paper-local");
  const [pythonIbkrPort, setPythonIbkrPort] = useState<4001 | 4002 | 7496 | 7497>(4002);
  const [pythonIbkrBarCount, setPythonIbkrBarCount] = useState(0);
  const [pythonIbkrImportToken, setPythonIbkrImportToken] = useState<string | null>(null);
  const [pythonPositionQuantity, setPythonPositionQuantity] = useState(1);
  const [pythonExecutionOrderType, setPythonExecutionOrderType] = useState<"market" | "limit">("limit");
  const [pythonLimitPriceUsd, setPythonLimitPriceUsd] = useState("400.00");
  const [pythonSourceFile, setPythonSourceFile] = useState<File | null>(null);
  const [marketCsvFile, setMarketCsvFile] = useState<File | null>(null);
  const [scheduleFile, setScheduleFile] = useState<File | null>(null);
  const [metadata, setMetadata] = useState({
    provider: "caller_uploaded_data", currency: "USD", timezone: "America/New_York",
    calendarId: "us_equity_rth", calendarVersion: "caller-v1",
    availabilityModel: "completed_bar_available_from_uploaded_timestamps",
    adjustmentPolicy: "caller_declared_unadjusted",
    corporateActionPolicy: "caller_declared_unadjusted",
    warmupStart: "2025-02-03", warmupEnd: "2025-02-03",
    evaluationStart: "2025-02-04", evaluationEnd: "2025-02-07",
  });
  const [initialCash, setInitialCash] = useState(100_000);
  const [commissionRate, setCommissionRate] = useState(0.001);
  const [slippageFraction, setSlippageFraction] = useState(0);
  const [pythonBusy, setPythonBusy] = useState(false);
  const [pythonError, setPythonError] = useState("");
  const [pythonReview, setPythonReview] = useState<{
    payload: Parameters<typeof api.previewPythonUploadBacktest>[0];
    preview: Awaited<ReturnType<typeof api.previewPythonUploadBacktest>>;
  } | null>(null);
  const [pythonResult, setPythonResult] = useState<{ run_id: string; execution_report: Record<string, unknown> } | null>(null);
  const [paperRuntime, setPaperRuntime] = useState<Awaited<ReturnType<typeof api.getPaperExecutionStatus>> | null>(null);
  const [paperRuntimeUnavailable, setPaperRuntimeUnavailable] = useState(false);
  const [paperActivity, setPaperActivity] = useState<Awaited<ReturnType<typeof api.getPaperExecutionActivity>> | null>(null);
  const [paperActivityUnavailable, setPaperActivityUnavailable] = useState(false);
  const [paperCancelBusy, setPaperCancelBusy] = useState<string | null>(null);
  const [paperCancelNotice, setPaperCancelNotice] = useState<{ intentId: string; ok: boolean } | null>(null);
  const [paperActivityStreamStatus, setPaperActivityStreamStatus] = useState<SSEStatus>("disconnected");
  const [paperPauseBusy, setPaperPauseBusy] = useState(false);
  const [paperPauseError, setPaperPauseError] = useState(false);
  const [paperControlBusy, setPaperControlBusy] = useState(false);
  const [paperControlError, setPaperControlError] = useState(false);
  const [paperIntegrationTest, setPaperIntegrationTest] = useState<PaperIntegrationTestStatus | null>(null);
  const [paperIntegrationBusy, setPaperIntegrationBusy] = useState(false);
  const [paperIntegrationError, setPaperIntegrationError] = useState<"generic" | "signal_date" | null>(null);
  const [paperIntegrationResult, setPaperIntegrationResult] = useState<PaperIntegrationTestResult | null>(null);
  const [paperStrategyCandidates, setPaperStrategyCandidates] = useState<PaperStrategyCandidatesResponse | null>(null);
  const [paperCandidateSelecting, setPaperCandidateSelecting] = useState<string | null>(null);
  const [paperCandidateSelectError, setPaperCandidateSelectError] = useState(false);

  useEffect(() => {
    onPaperActivityStatusChange(setPaperActivityStreamStatus);
    return () => onPaperActivityStatusChange(() => undefined);
  }, [onPaperActivityStatusChange]);

  useEffect(() => {
    let active = true;
    let inFlight = false;
    const refreshActivity = () => {
      api.getPaperExecutionActivity().then((activity) => {
        if (active) {
          paperActivityCursorRef.current = activity.latest_activity_seq;
          setPaperActivity(activity);
          setPaperActivityUnavailable(false);
        }
      }).catch(() => {
        if (active) setPaperActivityUnavailable(true);
      });
    };
    refreshPaperActivityRef.current = refreshActivity;
    const refreshStatus = () => {
      if (inFlight) return;
      inFlight = true;
      api.getPaperExecutionStatus().then((status) => {
        if (active) {
          setPaperRuntime(status);
          setPaperRuntimeUnavailable(false);
          if (status.configured) {
            api.getPaperIntegrationTestStatus().then((integrationStatus) => {
              if (active) setPaperIntegrationTest(integrationStatus);
            }).catch(() => {
              if (active) setPaperIntegrationTest(null);
            });
            api.getPaperStrategyCandidates().then((candidates) => {
              if (active) setPaperStrategyCandidates(candidates);
            }).catch(() => {
              if (active) setPaperStrategyCandidates(null);
            });
          } else {
            setPaperIntegrationTest(null);
            setPaperStrategyCandidates(null);
          }
        }
      }).catch(() => {
        if (active) setPaperRuntimeUnavailable(true);
      }).finally(() => {
        inFlight = false;
      });
      refreshActivity();
    };

    refreshStatus();
    const interval = window.setInterval(refreshStatus, 10_000);
    return () => {
      active = false;
      refreshPaperActivityRef.current = () => undefined;
      window.clearInterval(interval);
    };
  }, []);

  const hasPaperActivitySnapshot = paperActivity !== null;
  useEffect(() => {
    if (paperRuntime?.owner_lease_held !== true || !hasPaperActivitySnapshot) return;
    connectPaperActivity(
      api.paperExecutionActivityEventsUrl(paperActivityCursorRef.current),
      {
        message: () => {
          if (activityRefreshTimerRef.current !== null) {
            window.clearTimeout(activityRefreshTimerRef.current);
          }
          activityRefreshTimerRef.current = window.setTimeout(() => {
            activityRefreshTimerRef.current = null;
            refreshPaperActivityRef.current();
          }, 150);
        },
      },
    );
    return () => {
      disconnectPaperActivity();
      if (activityRefreshTimerRef.current !== null) {
        window.clearTimeout(activityRefreshTimerRef.current);
        activityRefreshTimerRef.current = null;
      }
    };
  }, [connectPaperActivity, disconnectPaperActivity, hasPaperActivitySnapshot, paperRuntime?.owner_lease_held]);

  const pausePaperExecution = async () => {
    setPaperPauseBusy(true);
    setPaperPauseError(false);
    try {
      const status = await api.pausePaperExecution();
      setPaperRuntime(status);
      setPaperRuntimeUnavailable(false);
    } catch {
      setPaperPauseError(true);
    } finally {
      setPaperPauseBusy(false);
    }
  };

  const startPaperExecution = async () => {
    const versionId = paperRuntime?.strategy_version_id;
    if (!versionId) return;
    setPaperControlBusy(true);
    setPaperControlError(false);
    try {
      const status = await api.startPaperExecution(versionId);
      setPaperRuntime(status);
      setPaperRuntimeUnavailable(false);
      refreshPaperActivityRef.current();
    } catch {
      setPaperControlError(true);
    } finally {
      setPaperControlBusy(false);
    }
  };

  const activatePaperExecution = async () => {
    setPaperControlBusy(true);
    setPaperControlError(false);
    try {
      const status = await api.activatePaperExecution();
      setPaperRuntime(status);
      setPaperRuntimeUnavailable(false);
      refreshPaperActivityRef.current();
    } catch {
      setPaperControlError(true);
    } finally {
      setPaperControlBusy(false);
    }
  };

  const runPaperIntegrationTest = async () => {
    setPaperIntegrationBusy(true);
    setPaperIntegrationError(null);
    setPaperIntegrationResult(null);
    try {
      const result = await api.runPaperIntegrationTest();
      setPaperIntegrationResult(result);
      if (result.activity) {
        paperActivityCursorRef.current = result.activity.latest_activity_seq;
        setPaperActivity(result.activity);
        setPaperActivityUnavailable(false);
      }
      const [runtimeStatus, integrationStatus] = await Promise.all([
        api.getPaperExecutionStatus(),
        api.getPaperIntegrationTestStatus(),
      ]);
      setPaperRuntime(runtimeStatus);
      setPaperRuntimeUnavailable(false);
      setPaperIntegrationTest(integrationStatus);
      refreshPaperActivityRef.current();
    } catch (cause) {
      setPaperIntegrationError(cause instanceof ApiError && cause.code === "paper_signal_session_mismatch" ? "signal_date" : "generic");
    } finally {
      setPaperIntegrationBusy(false);
    }
  };

  const cancelPaperOrder = async (intentId: string) => {
    setPaperCancelBusy(intentId);
    setPaperCancelNotice(null);
    try {
      await api.cancelPaperExecutionOrder(intentId);
      setPaperCancelNotice({ intentId, ok: true });
    } catch {
      setPaperCancelNotice({ intentId, ok: false });
    } finally {
      setPaperCancelBusy(null);
      refreshPaperActivityRef.current();
    }
  };

  const selectPaperStrategyCandidate = async (strategyVersionId: string) => {
    setPaperCandidateSelecting(strategyVersionId);
    setPaperCandidateSelectError(false);
    try {
      await api.selectPaperStrategyCandidate(strategyVersionId);
      const [runtimeStatus, integrationStatus, candidates] = await Promise.all([
        api.getPaperExecutionStatus(),
        api.getPaperIntegrationTestStatus(),
        api.getPaperStrategyCandidates(),
      ]);
      setPaperRuntime(runtimeStatus);
      setPaperIntegrationTest(integrationStatus);
      setPaperStrategyCandidates(candidates);
      setPaperRuntimeUnavailable(false);
    } catch {
      setPaperCandidateSelectError(true);
    } finally {
      setPaperCandidateSelecting(null);
    }
  };

  const paperRuntimeDetail = paperRuntimeUnavailable
    ? text.paperStatusUnavailable
    : paperRuntime === null
      ? text.paperChecking
      : !paperRuntime.configured
        ? text.paperNotConfigured
        : paperRuntime.dispatch_gate_open
          ? text.paperRunning
          : !paperRuntime.composition_ready
            ? text.paperCompositionIncomplete
          : paperRuntime.phase === "STOPPED"
            ? text.paperStopped
            : text.paperPaused;
  const paperRuntimeState = paperRuntimeUnavailable
    ? "STATUS UNKNOWN"
    : paperRuntime?.configured && !paperRuntime.composition_ready
      ? "COMPOSITION INCOMPLETE"
      : paperRuntime?.phase ?? "CHECKING";
  const paperMissingRequirements = (paperRuntime?.missing_requirements ?? []).map((requirement) => ({
    code: requirement,
    description: t(`execution.paperRequirement.${requirement}`, { defaultValue: requirement }),
  }));

  const currentBacktestPayload = () => ({
    symbol: symbol.trim(),
    instrument_id: instrumentId.trim(),
    source,
    ...(source === "ibkr" ? { ibkr_profile_id: ibkrProfileId, ibkr_port: ibkrPort } : {}),
    start_date: startDate,
    end_date: endDate,
    strategy_description: strategyDescription.trim(),
    allocation_fraction: allocation,
  });

  const previewBacktest = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const generation = ++previewGenerationRef.current;
    pythonPreviewGenerationRef.current += 1;
    setPythonReview(null);
    setPendingClarification(null);
    setClarificationAnswer("");
    setPythonError("");
    setBusy(true);
    setError("");
    setResult(null);
    setPythonResult(null);
    try {
      const payload = currentBacktestPayload();
      const preview = await api.previewExecutionBacktest(payload);
      if (generation === previewGenerationRef.current) {
        setReview({ payload, preview, transcript: preview.clarification_transcript });
      }
    } catch (cause) {
      if (generation === previewGenerationRef.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (generation === previewGenerationRef.current) setBusy(false);
    }
  };

  const continueClarification = async () => {
    if (!review?.preview.clarification_form || review.preview.clarification_form.status !== "needs_clarification") return;
    const answer = clarificationAnswer.trim();
    if (!answer) return;
    const transcript = review.transcript;
    if (!transcript) {
      setError(text.clarification);
      return;
    }
    const turn = pendingClarification
      && pendingClarification.transcriptId === transcript.transcript_id
      && pendingClarification.parentTurnId === transcript.turn_id
      && pendingClarification.answer === answer
      ? pendingClarification
      : {
        transcriptId: transcript.transcript_id,
        parentTurnId: transcript.turn_id,
        turnId: crypto.randomUUID(),
        answer,
      };
    setPendingClarification(turn);
    const generation = ++previewGenerationRef.current;
    pythonPreviewGenerationRef.current += 1;
    setPythonReview(null);
    setPythonError("");
    setBusy(true);
    setError("");
    setResult(null);
    const payload = {
      ...review.payload,
      strategy_description: `${review.payload.strategy_description}\n\nUser clarification: ${answer}`,
    };
    try {
      const preview = await api.previewExecutionBacktest({
        ...payload,
        clarification_transcript_id: turn.transcriptId,
        clarification_turn_id: turn.turnId,
        clarification_parent_turn_id: turn.parentTurnId,
        clarification_answer: turn.answer,
      });
      if (generation === previewGenerationRef.current) {
        setReview({ payload, preview, transcript: preview.clarification_transcript });
        setClarificationAnswer("");
        setPendingClarification(null);
      }
    } catch (cause) {
      if (generation === previewGenerationRef.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (generation === previewGenerationRef.current) setBusy(false);
    }
  };

  const confirmBacktest = async () => {
    if (!review?.preview.supported || !review.preview.preview_token) return;
    setBusy(true);
    setError("");
    setResult(null);
    setPythonResult(null);
    try {
      const created = await api.createExecutionBacktest({
        ...review.payload,
        preview_token: review.preview.preview_token,
      });
      setResult({ run_id: created.run_id, execution_report: created.execution_report });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const buildPythonUploadPayload = async (): Promise<Parameters<typeof api.previewPythonUploadBacktest>[0]> => {
    if (!pythonSourceFile || !marketCsvFile || !scheduleFile) throw new Error(text.pythonNoFiles);
    const [strategySource, marketDataCsv, scheduleText] = await Promise.all([
      pythonSourceFile.text(), marketCsvFile.text(), scheduleFile.text(),
    ]);
    const parsedSchedule: unknown = JSON.parse(scheduleText);
    const expectedSessions = Array.isArray(parsedSchedule)
      ? parsedSchedule
      : (parsedSchedule && typeof parsedSchedule === "object" && "sessions" in parsedSchedule
        ? (parsedSchedule as { sessions?: unknown }).sessions : undefined);
    if (!Array.isArray(expectedSessions)) throw new Error("Session schedule JSON must contain an array.");
    return {
      strategy_id: pythonStrategyId.trim(),
      instrument_id: pythonInstrumentId.trim(),
      strategy_source: strategySource,
      market_data_csv: marketDataCsv,
      market_data_metadata: {
        provider: metadata.provider,
        instrument_id: pythonInstrumentId.trim(),
        currency: metadata.currency,
        timezone: metadata.timezone,
        calendar_id: metadata.calendarId,
        calendar_version: metadata.calendarVersion,
        availability_model: metadata.availabilityModel,
        adjustment_policy: metadata.adjustmentPolicy,
        corporate_action_policy: metadata.corporateActionPolicy,
        fetched_at: new Date().toISOString(),
        warmup_start: metadata.warmupStart,
        warmup_end: metadata.warmupEnd,
        evaluation_start: metadata.evaluationStart,
        evaluation_end: metadata.evaluationEnd,
        download_parameters: { submitted_via: "execution_page" },
      },
      expected_sessions: expectedSessions as Parameters<typeof api.previewPythonUploadBacktest>[0]["expected_sessions"],
      ...(pythonIbkrImportToken ? { ibkr_import_token: pythonIbkrImportToken } : {}),
      position_quantity: pythonPositionQuantity,
      execution_order_type: pythonExecutionOrderType,
      ...(pythonExecutionOrderType === "limit" ? { limit_price_usd: pythonLimitPriceUsd } : {}),
      initial_cash: initialCash,
      commission_rate: commissionRate,
      slippage_fraction: slippageFraction,
    };
  };

  const fetchPythonIbkrBars = async () => {
    setPythonBusy(true);
    setPythonError("");
    setPythonReview(null);
    setPythonResult(null);
    setResult(null);
    try {
      const fetched = await api.previewIbkrPythonMarketData({
        symbol: pythonIbkrSymbol.trim(),
        instrument_id: pythonInstrumentId.trim(),
        profile_id: pythonIbkrProfileId,
        port: pythonIbkrPort,
        warmup_start: metadata.warmupStart,
        warmup_end: metadata.warmupEnd,
        evaluation_start: metadata.evaluationStart,
        evaluation_end: metadata.evaluationEnd,
      });
      if (!fetched.supported || !fetched.market_data_csv || !fetched.market_data_metadata || !fetched.expected_sessions) {
        throw new Error(fetched.reason || "IBKR daily history could not be imported.");
      }
      const nextMetadata = fetched.market_data_metadata;
      setMarketCsvFile(new File([fetched.market_data_csv], "ibkr_daily_bars.csv", { type: "text/csv" }));
      setScheduleFile(new File([JSON.stringify(fetched.expected_sessions, null, 2)], "xnys_sessions.json", { type: "application/json" }));
      setPythonIbkrBarCount(fetched.bar_count ?? fetched.expected_sessions.length);
      setPythonIbkrImportToken(fetched.ibkr_import_token ?? null);
      setMetadata((old) => ({
        ...old,
        provider: String(nextMetadata.provider ?? "ibkr"),
        currency: String(nextMetadata.currency ?? "USD"),
        timezone: String(nextMetadata.timezone ?? "America/New_York"),
        calendarId: String(nextMetadata.calendar_id ?? "XNYS"),
        calendarVersion: String(nextMetadata.calendar_version ?? ""),
        availabilityModel: String(nextMetadata.availability_model ?? ""),
        adjustmentPolicy: String(nextMetadata.adjustment_policy ?? ""),
        corporateActionPolicy: String(nextMetadata.corporate_action_policy ?? ""),
      }));
    } catch (cause) {
      setPythonError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPythonBusy(false);
    }
  };

  const previewPythonUpload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const generation = ++pythonPreviewGenerationRef.current;
    previewGenerationRef.current += 1;
    setReview(null);
    setClarificationAnswer("");
    setError("");
    if (!pythonSourceFile || !marketCsvFile || !scheduleFile) {
      setPythonError(text.pythonNoFiles);
      return;
    }
    setPythonBusy(true);
    setPythonError("");
    setPythonReview(null);
    setPythonResult(null);
    setResult(null);
    try {
      const payload = await buildPythonUploadPayload();
      const preview = await api.previewPythonUploadBacktest(payload);
      if (!preview.supported) throw new Error(preview.reason || "Uploaded source or market data failed validation.");
      if (payload.ibkr_import_token) setPythonIbkrImportToken(null);
      if (generation === pythonPreviewGenerationRef.current) setPythonReview({ payload, preview });
    } catch (cause) {
      if (generation === pythonPreviewGenerationRef.current) setPythonError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (generation === pythonPreviewGenerationRef.current) setPythonBusy(false);
    }
  };

  const confirmPythonUpload = async () => {
    if (!pythonReview?.preview.preview_token) return;
    setPythonBusy(true);
    setPythonError("");
    setPythonResult(null);
    setResult(null);
    try {
      const created = await api.createPythonUploadBacktest({
        strategy_id: pythonReview.payload.strategy_id,
        instrument_id: pythonReview.payload.instrument_id,
        strategy_source: pythonReview.payload.strategy_source,
        position_quantity: pythonReview.payload.position_quantity,
        execution_order_type: pythonReview.payload.execution_order_type,
        ...(pythonReview.payload.limit_price_usd ? { limit_price_usd: pythonReview.payload.limit_price_usd } : {}),
        initial_cash: pythonReview.payload.initial_cash,
        commission_rate: pythonReview.payload.commission_rate,
        slippage_fraction: pythonReview.payload.slippage_fraction,
        preview_token: pythonReview.preview.preview_token,
      });
      setPythonResult({ run_id: created.run_id, execution_report: created.execution_report });
      setPythonReview(null);
    } catch (cause) {
      setPythonError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPythonBusy(false);
    }
  };

  const report = result?.execution_report;
  const pythonReport = pythonResult?.execution_report;
  const pythonMetrics = pythonReport?.metrics as Record<string, unknown> | undefined;
  const pythonTotalReturn = pythonMetrics?.total_return_fraction;
  const activeReport = pythonReport ?? report;
  const metrics = activeReport?.metrics as Record<string, unknown> | undefined;
  const finalValue = activeReport?.final_value;
  const totalReturn = metrics?.total_return_fraction ?? metrics?.total_return;
  const totalFees = activeReport?.total_fees;
  const activeManifest = activeReport?.data_manifest as Record<string, unknown> | undefined;
  const activeProvenance = activeReport?.provenance as Record<string, unknown> | undefined;
  const equityCurve = activeReport?.equity_curve;
  const activeBarCount = Array.isArray(equityCurve) ? equityCurve.length : undefined;
  const activeEngine = typeof activeReport?.engine === "string"
    ? activeReport.engine
    : typeof activeReport?.execution_backend === "string"
      ? activeReport.execution_backend
      : activeReport ? text.unknown : text.backendValue;
  const activeVersionHash = activeReport?.strategy_version_hash ?? activeReport?.strategy_version_id;
  const activeVersion = typeof activeVersionHash === "string"
    ? `${String(activeVersionHash).slice(0, 12)}…`
    : activeReport ? text.unknown : text.versionValue;
  const activeMode = typeof activeReport?.execution_mode === "string"
    ? `${activeReport.execution_mode} · offline`
    : typeof activeReport?.mode === "string"
      ? activeReport.mode
      : activeReport ? text.unknown : text.modeValue;
  const activeSource = typeof activeManifest?.provider === "string"
    ? activeManifest.provider
    : typeof activeProvenance?.data_source === "string"
      ? activeProvenance.data_source
      : activeManifest?.synthetic === true ? "synthetic fixture" : activeReport ? text.unknown : "imported bars";
  const activeDataset = activeBarCount === undefined
    ? activeReport ? text.unknown : text.dataValue
    : `${activeBarCount} bars · ${activeSource}`;

  const fieldClass = "mt-1 h-10 w-full rounded-md border border-border bg-background px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

  return (
    <div className="min-h-full p-5 sm:p-6 lg:p-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6">
        <header className="relative overflow-hidden rounded-xl border border-border/60 bg-card shadow-sm">
          <div className="absolute inset-y-0 start-0 w-1 bg-warning" aria-hidden="true" />
          <div className="grid gap-7 p-6 ps-8 sm:p-8 sm:ps-10 lg:grid-cols-[minmax(0,1.2fr)_minmax(260px,0.8fr)] lg:items-end">
            <div>
              <div className="flex items-center gap-2 font-mono text-[10px] font-semibold uppercase tracking-[0.18em] text-warning">
                <FlaskConical className="h-3.5 w-3.5" />
                {text.eyebrow}
              </div>
              <h1 className="mt-4 max-w-2xl text-3xl font-semibold leading-tight tracking-tight sm:text-4xl">{text.title}</h1>
              <p className="mt-3 max-w-2xl text-sm leading-relaxed text-muted-foreground">{text.intro}</p>
            </div>
            <div className="grid grid-cols-2 gap-x-5 gap-y-4 border-t border-border/60 pt-5 lg:border-s lg:border-t-0 lg:ps-6 lg:pt-0">
              <EvidenceStat label={text.engine} value={activeEngine} />
              <EvidenceStat label={text.version} value={activeVersion} />
              <EvidenceStat label={text.mode} value={activeMode} />
              <EvidenceStat label={text.dataset} value={activeDataset} />
            </div>
          </div>
        </header>

        <section aria-labelledby="execution-create-title" className="rounded-xl border border-border/60 bg-card p-5 shadow-sm sm:p-6">
          <div className="flex flex-col gap-1">
            <h2 id="execution-create-title" className="text-lg font-semibold">{text.createTitle}</h2>
            <p className="max-w-3xl text-xs leading-relaxed text-muted-foreground">{text.createDescription}</p>
          </div>
          <form className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4" onSubmit={previewBacktest} onChange={() => {
            previewGenerationRef.current += 1;
            pythonPreviewGenerationRef.current += 1;
            setReview(null);
            setClarificationAnswer("");
            setBusy(false);
            setPythonReview(null);
            setPythonBusy(false);
          }}>
            <label className="text-xs font-medium">{text.symbol}<input className={fieldClass} value={symbol} onChange={(event) => setSymbol(event.target.value)} maxLength={32} required /></label>
            <label className="text-xs font-medium">{text.instrument}<input className={fieldClass} value={instrumentId} onChange={(event) => setInstrumentId(event.target.value)} maxLength={64} required /></label>
            <label className="text-xs font-medium">{text.source}<select className={fieldClass} value={source} onChange={(event) => setSource(event.target.value as "yfinance" | "stooq" | "ibkr")}><option value="yfinance">Yahoo Finance via yfinance</option><option value="stooq">Stooq</option><option value="ibkr">{text.ibkrSource}</option></select></label>
            {source === "ibkr" ? <>
              <label className="text-xs font-medium">IBKR profile<select className={fieldClass} value={ibkrProfileId} onChange={(event) => { const next = event.target.value as typeof ibkrProfileId; setIbkrProfileId(next); setIbkrPort(next === "ibkr-paper-local" ? 4002 : 4001); }}><option value="ibkr-paper-local">{text.ibkrPaperProfile}</option><option value="ibkr-live-local-readonly">{text.ibkrLiveProfile}</option></select></label>
              <label className="text-xs font-medium">{text.ibkrPort}<select className={fieldClass} value={ibkrPort} onChange={(event) => setIbkrPort(Number(event.target.value) as typeof ibkrPort)}>{ibkrProfileId === "ibkr-paper-local" ? <><option value={4002}>IB Gateway PAPER · 4002</option><option value={7497}>TWS PAPER · 7497</option></> : <><option value={4001}>IB Gateway LIVE · 4001 (read-only)</option><option value={7496}>TWS LIVE · 7496 (read-only)</option></>}</select></label>
            </> : null}
            <label className="text-xs font-medium">{text.allocation}<input className={fieldClass} type="number" min="0.01" max="1" step="0.01" value={allocation} onChange={(event) => setAllocation(Number(event.target.value))} required /></label>
            <label className="text-xs font-medium">{text.startDate}<input className={fieldClass} type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} required /></label>
            <label className="text-xs font-medium">{text.endDate}<input className={fieldClass} type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} required /></label>
            <label className="text-xs font-medium sm:col-span-2 lg:col-span-4">{text.strategy}<textarea className="mt-1 min-h-20 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring" value={strategyDescription} onChange={(event) => setStrategyDescription(event.target.value)} maxLength={4096} required /></label>
            <div className="flex flex-col gap-2 sm:col-span-2 lg:col-span-4">
              <button className="inline-flex h-10 w-fit items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60" type="submit" disabled={busy || !symbol.trim() || !instrumentId.trim() || !strategyDescription.trim()}>
                {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}{busy ? text.reviewing : text.run}
              </button>
              <p className="text-xs leading-relaxed text-muted-foreground">{text.strategyHint}</p>
              <p className="text-xs leading-relaxed text-warning">{text.runIntro}</p>
              {error && <p role="alert" className="text-sm text-destructive">{text.runFailure}: {error}</p>}
            </div>
          </form>
          {review && (
            <div className="mt-5 rounded-lg border border-border bg-muted/30 p-4" aria-live="polite">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="font-semibold">{text.reviewTitle}</h3>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    {review.preview.supported ? text.reviewAccepted : text.reviewUnsupported}
                  </p>
                </div>
                <button className="h-9 rounded-md border border-border px-3 text-xs font-medium hover:bg-background" type="button" onClick={() => { setReview(null); setPendingClarification(null); setClarificationAnswer(""); }}>{text.editInputs}</button>
              </div>
              {review.preview.supported ? (() => {
                const spec = review.preview.strategy_spec ?? {};
                const periods = (spec.parameters ?? {}) as Record<string, unknown>;
                const isRsi = spec.template_id === "rsi_threshold";
                const position = (spec.position_policy ?? {}) as Record<string, unknown>;
                const execution = (spec.execution_policy ?? {}) as Record<string, unknown>;
                const summary = review.preview.summary ?? {};
                return (
                  <>
                    <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                      <EvidenceStat label={text.strategy} value={String(summary.strategy_description ?? "—")} />
                      <EvidenceStat label={text.symbol} value={String(summary.symbol ?? "—")} />
                      <EvidenceStat label={text.instrument} value={String(summary.instrument_id ?? "—")} />
                      <EvidenceStat label={text.source} value={String(summary.source ?? "—")} />
                      <EvidenceStat label={text.marketHash} value={String(summary.market_data_sha256 ?? "—")} />
                      <EvidenceStat label={text.manifestHash} value={String(summary.data_manifest_sha256 ?? "—")} />
                      <EvidenceStat label={text.allocation} value={`${(Number(summary.allocation_fraction ?? 0) * 100).toFixed(0)}%`} />
                      <EvidenceStat label={text.startDate} value={String(summary.start_date ?? "—")} />
                      <EvidenceStat label={text.endDate} value={String(summary.end_date ?? "—")} />
                      <EvidenceStat label={text.templateSource} value={String(review.preview.template_source_sha256 ?? "—")} />
                      <EvidenceStat label={text.compilerVersion} value={String(review.preview.compiler_version ?? "—")} />
                      <EvidenceStat label={text.templateParameters} value={isRsi
                        ? `RSI(${String(periods.period ?? "—")}) · entry < ${String(periods.entry_below ?? "—")} · exit > ${String(periods.exit_above ?? "—")}`
                        : `${text.fastPeriod}: ${String(periods.fast_period ?? "—")} ${text.days} · ${text.slowPeriod}: ${String(periods.slow_period ?? "—")} ${text.days}`} />
                    </div>
                    <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                      <EvidenceStat label={text.positionPolicy} value={`${position.mode === "target_fraction" ? text.targetFraction : String(position.mode ?? "—")} · ${position.long_only ? text.longOnlyValue : "—"} · ${position.whole_shares ? text.wholeSharesValue : "fractional shares"} · ${position.initial_entry === "wait_for_cross" ? text.waitForCross : String(position.initial_entry ?? "—")}`} />
                      <EvidenceStat label={text.decisionTiming} value={`${execution.decision === "completed_bar_available" ? text.completedBar : String(execution.decision ?? "—")} → ${execution.dispatch === "next_regular_session" ? text.nextRegularSession : String(execution.dispatch ?? "—")}`} />
                      <EvidenceStat label={text.orderPolicy} value={`${execution.order_type === "market" && execution.time_in_force === "DAY" ? text.marketDay : `${String(execution.order_type)} / ${String(execution.time_in_force)}`} · ${execution.outside_rth === false ? text.outsideRthOff : "outside RTH on"} · ${execution.gap_action === "skip" ? text.gapSkip : String(execution.gap_action)} · ${execution.missed_session_action === "expire" ? text.missedExpire : String(execution.missed_session_action)}`} />
                      <EvidenceStat label={text.strategyBudget} value={`$${Number(summary.strategy_budget ?? 0).toLocaleString()}`} />
                      <EvidenceStat label={text.feeReserve} value={`$${Number(summary.fee_reserve ?? 0).toFixed(2)}`} />
                      <EvidenceStat label={text.priceBuffer} value={`${(Number(summary.price_buffer_fraction ?? 0) * 100).toFixed(2)}%`} />
                      <EvidenceStat label={text.commissionModel} value={`${(Number(summary.commission_rate ?? 0) * 100).toFixed(3)}%`} />
                      <EvidenceStat label={text.slippageModel} value={`${(Number(summary.slippage_fraction ?? 0) * 100).toFixed(3)}%`} />
                      <EvidenceStat label={text.initialCashLabel} value={`$${Number(summary.initial_cash ?? 0).toLocaleString()}`} />
                    </div>
                    <ul className="mt-4 list-disc space-y-1 ps-5 text-xs leading-relaxed text-foreground">
                      <li>{isRsi ? text.rsiBuy : text.ruleBuy}</li>
                      <li>{isRsi ? text.rsiSell : text.ruleSell}</li>
                    </ul>
                    <div className="mt-3 space-y-1 text-xs text-warning">
                      {(review.preview.warnings ?? []).map((warning) => <p key={warning}>{warning}</p>)}
                    </div>
                    <p className="mt-3 break-all font-mono text-[10px] text-muted-foreground">{text.reviewScope}: {review.preview.review_hash}</p>
                    <button className="mt-4 inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60" type="button" onClick={confirmBacktest} disabled={busy}>
                      {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}{busy ? text.running : text.confirmRun}
                    </button>
                  </>
                );
              })() : (() => {
                const clarificationForm = review.preview.clarification_form;
                const needsClarification = clarificationForm?.schema_version === "strategy-clarification/1"
                  && clarificationForm.status === "needs_clarification";
                const remainingClarificationChars = Math.max(
                  0,
                  4096 - review.payload.strategy_description.length - "\n\nUser clarification: ".length,
                );
                return (
                  <div className="mt-3 space-y-3 text-sm text-warning">
                    {review.transcript && <p className="text-xs text-muted-foreground">
                      {text.transcriptSaved}: {review.transcript.turn_count} / 32
                    </p>}
                    <p role="status">{needsClarification
                      ? clarificationForm.question
                      : review.preview.clarification || review.preview.reason || text.clarification}</p>
                    {needsClarification && clarificationForm.missing_fields.length > 0 && (
                      <div>
                        <p className="text-xs font-semibold">{text.missingDetails}</p>
                        <ul className="mt-1 list-disc ps-5 text-xs">
                          {clarificationForm.missing_fields.map((field) => <li key={field.name}>{field.label}</li>)}
                        </ul>
                      </div>
                    )}
                    {needsClarification && (
                      <div className="grid gap-2">
                        <label className="text-xs font-medium text-foreground" htmlFor="strategy-clarification-answer">{text.yourClarification}</label>
                        <textarea
                          id="strategy-clarification-answer"
                          className="min-h-20 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          placeholder={text.clarificationPlaceholder}
                          value={clarificationAnswer}
                          onChange={(event) => {
                            if (!pendingClarification) setClarificationAnswer(event.target.value);
                          }}
                          maxLength={Math.min(512, remainingClarificationChars)}
                        />
                        <button
                          className="inline-flex h-10 w-fit items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60"
                          type="button"
                          onClick={continueClarification}
                          disabled={busy || !clarificationAnswer.trim() || remainingClarificationChars === 0}
                        >
                          {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}{text.continueClarification}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })()}
            </div>
          )}
          {result ? (
            <div className="mt-5 rounded-lg border border-success/30 bg-success/5 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="font-semibold">{text.runSuccess}</h3>
                <Link className="font-mono text-xs text-primary underline-offset-4 hover:underline" to={`/runs/${encodeURIComponent(result.run_id)}?view=dashboard`}>{text.openRun} · {result.run_id}</Link>
              </div>
              <div className="mt-4 grid gap-4 sm:grid-cols-3">
                <EvidenceStat label={text.finalValue} value={typeof finalValue === "number" ? finalValue.toFixed(2) : "—"} />
                <EvidenceStat label={text.totalReturn} value={typeof totalReturn === "number" ? `${(totalReturn * 100).toFixed(2)}%` : "—"} />
                <EvidenceStat label={text.feesValue} value={typeof totalFees === "number" ? totalFees.toFixed(2) : "—"} />
              </div>
              <div className="mt-3 break-all font-mono text-[10px] leading-relaxed text-muted-foreground">
                <p>{text.version}: {String(report?.strategy_version_hash ?? report?.strategy_version_id ?? text.unknown)}</p>
                <p>{text.reportCheck}: {((report?.execution_integrity as Record<string, unknown> | undefined)?.status === "verified") ? text.verified : text.unknown}</p>
              </div>
              <pre className="mt-4 max-h-80 overflow-auto rounded-md bg-background/80 p-3 font-mono text-[10px] leading-relaxed text-muted-foreground">{JSON.stringify(result.execution_report, null, 2)}</pre>
            </div>
          ) : !error ? <p className="mt-4 text-xs text-muted-foreground">{text.noExecution}</p> : null}
        </section>

        <section aria-labelledby="python-upload-title" className="rounded-xl border border-border/60 bg-card p-5 shadow-sm sm:p-6">
          <div className="flex flex-col gap-1">
            <h2 id="python-upload-title" className="text-lg font-semibold">{text.pythonUploadTitle}</h2>
            <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:justify-between">
              <p className="max-w-4xl text-xs leading-relaxed text-muted-foreground">{text.pythonUploadDescription}</p>
              <a className="shrink-0 font-mono text-[10px] text-primary underline-offset-4 hover:underline" download="vibe_strategy_template.py" href="/templates/vibe_strategy_template.py">{text.downloadTemplate}</a>
              <a className="shrink-0 font-mono text-[10px] text-primary underline-offset-4 hover:underline" download="paper_aapl_one_shot.py" href="/templates/paper_aapl_one_shot.py">{text.downloadPaperExample}</a>
            </div>
          </div>
          <form className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4" onSubmit={previewPythonUpload} onChange={() => {
            pythonPreviewGenerationRef.current += 1;
            previewGenerationRef.current += 1;
            setPythonReview(null);
            setPythonBusy(false);
            setReview(null);
            setClarificationAnswer("");
            setBusy(false);
            setPythonIbkrBarCount(0);
          }}>
            <fieldset className="contents" disabled={pythonBusy || Boolean(pythonReview)}>
            <label className="text-xs font-medium">{text.strategyId}<input className={fieldClass} value={pythonStrategyId} maxLength={64} onChange={(event) => setPythonStrategyId(event.target.value)} required /></label>
            <label className="text-xs font-medium">{text.instrument}<input className={fieldClass} value={pythonInstrumentId} maxLength={64} onChange={(event) => setPythonInstrumentId(event.target.value)} required /></label>
            <label className="text-xs font-medium">{text.ibkrSymbol}<input className={fieldClass} value={pythonIbkrSymbol} maxLength={32} onChange={(event) => setPythonIbkrSymbol(event.target.value)} required /></label>
            <label className="text-xs font-medium">{text.ibkrProfile}<select className={fieldClass} value={pythonIbkrProfileId} onChange={(event) => { const next = event.target.value as typeof pythonIbkrProfileId; setPythonIbkrProfileId(next); setPythonIbkrPort(next === "ibkr-paper-local" ? 4002 : 4001); }}><option value="ibkr-paper-local">IBKR PAPER (DU account)</option><option value="ibkr-live-local-readonly">IBKR LIVE (read-only)</option></select></label>
            <label className="text-xs font-medium">{text.ibkrPort}<select className={fieldClass} value={pythonIbkrPort} onChange={(event) => setPythonIbkrPort(Number(event.target.value) as typeof pythonIbkrPort)}>{pythonIbkrProfileId === "ibkr-paper-local" ? <><option value={4002}>IB Gateway PAPER · 4002</option><option value={7497}>TWS PAPER · 7497</option></> : <><option value={4001}>IB Gateway LIVE · 4001 (read-only)</option><option value={7496}>TWS LIVE · 7496 (read-only)</option></>}</select></label>
            <label className="text-xs font-medium">{text.pythonPositionQuantity}<input className={fieldClass} type="number" min="1" max="100000" step="1" value={pythonPositionQuantity} onChange={(event) => setPythonPositionQuantity(Number(event.target.value))} required /></label>
            <label className="text-xs font-medium">{text.pythonOrderType}<select className={fieldClass} value={pythonExecutionOrderType} onChange={(event) => setPythonExecutionOrderType(event.target.value as "market" | "limit")}><option value="market">MARKET · DAY</option><option value="limit">LIMIT · DAY</option></select></label>
            {pythonExecutionOrderType === "limit" && <label className="text-xs font-medium">{text.pythonLimitPrice}<input className={fieldClass} type="number" min="0.01" step="0.01" value={pythonLimitPriceUsd} onChange={(event) => setPythonLimitPriceUsd(event.target.value)} required /></label>}
            <div className="flex items-end"><button className="inline-flex h-10 items-center justify-center gap-2 rounded-md border border-primary px-4 text-sm font-medium text-primary disabled:opacity-60" type="button" onClick={() => void fetchPythonIbkrBars()} disabled={pythonBusy || Boolean(pythonReview) || !pythonIbkrSymbol.trim() || !pythonInstrumentId.trim()}>{pythonBusy && <LoaderCircle className="h-4 w-4 animate-spin" />}{pythonBusy ? text.fetchingIbkrBars : text.fetchIbkrBars}</button></div>
            {pythonIbkrBarCount > 0 && <p className="self-end text-xs text-positive sm:col-span-2 lg:col-span-4">{text.ibkrFetched} · {pythonIbkrBarCount} bars{pythonIbkrImportToken ? ` · ${text.ibkrPaperProofReady}` : ""}</p>}
            <label className="text-xs font-medium">{text.initialCash}<input className={fieldClass} type="number" min="1" step="100" value={initialCash} onChange={(event) => setInitialCash(Number(event.target.value))} required /></label>
            <label className="text-xs font-medium">{text.commissionRate}<input className={fieldClass} type="number" min="0" max="1" step="0.0001" value={commissionRate} onChange={(event) => setCommissionRate(Number(event.target.value))} required /></label>
            <label className="text-xs font-medium">{text.slippageFraction}<input className={fieldClass} type="number" min="0" max="0.25" step="0.0001" value={slippageFraction} onChange={(event) => setSlippageFraction(Number(event.target.value))} required /></label>
            <label className="text-xs font-medium">{text.pythonSourceFile}<input className={`${fieldClass} py-2`} type="file" accept=".py,text/x-python,text/plain" onChange={(event) => setPythonSourceFile(event.target.files?.[0] ?? null)} required /></label>
            <label className="text-xs font-medium">{text.marketCsvFile}<input className={`${fieldClass} py-2`} type="file" accept=".csv,text/csv" onChange={(event) => setMarketCsvFile(event.target.files?.[0] ?? null)} required={!marketCsvFile} /></label>
            <label className="text-xs font-medium sm:col-span-2">{text.scheduleFile}<input className={`${fieldClass} py-2`} type="file" accept=".json,application/json" onChange={(event) => setScheduleFile(event.target.files?.[0] ?? null)} required={!scheduleFile} /></label>
            <label className="text-xs font-medium">{text.provider}<input className={fieldClass} value={metadata.provider} onChange={(event) => setMetadata((old) => ({ ...old, provider: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.currency}<input className={fieldClass} value={metadata.currency} maxLength={8} onChange={(event) => setMetadata((old) => ({ ...old, currency: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.timezone}<input className={fieldClass} value={metadata.timezone} onChange={(event) => setMetadata((old) => ({ ...old, timezone: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.calendarId}<input className={fieldClass} value={metadata.calendarId} onChange={(event) => setMetadata((old) => ({ ...old, calendarId: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.calendarVersion}<input className={fieldClass} value={metadata.calendarVersion} onChange={(event) => setMetadata((old) => ({ ...old, calendarVersion: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.availability}<input className={fieldClass} value={metadata.availabilityModel} onChange={(event) => setMetadata((old) => ({ ...old, availabilityModel: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.adjustment}<input className={fieldClass} value={metadata.adjustmentPolicy} onChange={(event) => setMetadata((old) => ({ ...old, adjustmentPolicy: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.corporateAction}<input className={fieldClass} value={metadata.corporateActionPolicy} onChange={(event) => setMetadata((old) => ({ ...old, corporateActionPolicy: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.warmupStart}<input className={fieldClass} type="date" value={metadata.warmupStart} onChange={(event) => setMetadata((old) => ({ ...old, warmupStart: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.warmupEnd}<input className={fieldClass} type="date" value={metadata.warmupEnd} onChange={(event) => setMetadata((old) => ({ ...old, warmupEnd: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.evaluationStart}<input className={fieldClass} type="date" value={metadata.evaluationStart} onChange={(event) => setMetadata((old) => ({ ...old, evaluationStart: event.target.value }))} required /></label>
            <label className="text-xs font-medium">{text.evaluationEnd}<input className={fieldClass} type="date" value={metadata.evaluationEnd} onChange={(event) => setMetadata((old) => ({ ...old, evaluationEnd: event.target.value }))} required /></label>
            {paperIntegrationTest?.backtest_attestation?.status === "verified" && paperIntegrationTest.backtest_attestation.evaluation_end && paperIntegrationTest.backtest_attestation.suggested_warmup_date && (
              <div className="sm:col-span-2 lg:col-span-4">
                <button type="button" className="rounded-md border border-primary/40 px-3 py-1.5 text-xs text-primary" onClick={() => {
                  const proof = paperIntegrationTest.backtest_attestation;
                  if (!proof?.suggested_warmup_date || !proof.evaluation_end) return;
                  setMetadata((old) => ({ ...old, warmupStart: proof.suggested_warmup_date!, warmupEnd: proof.suggested_warmup_date!, evaluationStart: proof.evaluation_end!, evaluationEnd: proof.evaluation_end! }));
                  setPythonIbkrImportToken(null);
                  setPythonIbkrBarCount(0);
                  setMarketCsvFile(null);
                  setScheduleFile(null);
                }}>{text.usePaperDates}</button>
                <p className="mt-1 text-xs text-muted-foreground">{text.usePaperDatesHint}</p>
              </div>
            )}
            <div className="flex flex-col gap-2 sm:col-span-2 lg:col-span-4">
              <button className="inline-flex h-10 w-fit items-center justify-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60" type="submit" disabled={pythonBusy || Boolean(pythonReview) || !pythonStrategyId.trim() || !pythonInstrumentId.trim()}>
                {pythonBusy && <LoaderCircle className="h-4 w-4 animate-spin" />}{pythonBusy ? text.pythonReviewing : text.pythonReviewButton}
              </button>
              <p className="text-xs leading-relaxed text-warning">{text.pythonRunHint}</p>
              {pythonError && <p role="alert" className="text-sm text-destructive">{text.runFailure}: {pythonError}</p>}
            </div>
            </fieldset>
          </form>
          {pythonReview && (() => {
            const summary = pythonReview.preview.summary ?? {};
            return (
              <div className="mt-5 rounded-lg border border-border bg-muted/30 p-4" aria-live="polite">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-semibold">{text.pythonReviewTitle}</h3>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{text.pythonReviewDescription}</p>
                  </div>
                  <button className="h-9 rounded-md border border-border px-3 text-xs font-medium hover:bg-background" type="button" onClick={() => setPythonReview(null)}>{text.editPythonUpload}</button>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                  <EvidenceStat label={text.strategyId} value={String(summary.strategy_id ?? "—")} />
                  <EvidenceStat label={text.instrument} value={String(summary.instrument_id ?? "—")} />
                  <EvidenceStat label={text.source} value={String(summary.provider ?? "—")} />
                  <EvidenceStat label={text.ibkrPaperProof} value={summary.ibkr_paper_import_attested === true ? text.ibkrPaperProofYes : text.ibkrPaperProofNo} />
                  <EvidenceStat label={text.pythonPositionQuantity} value={String(summary.position_quantity ?? "—")} />
                  <EvidenceStat label={text.pythonOrderType} value={`${String(summary.execution_order_type ?? "—").toUpperCase()} · DAY${summary.limit_price_usd ? ` · $${String(summary.limit_price_usd)}` : ""}`} />
                  <EvidenceStat label={text.barCount} value={String(summary.bar_count ?? "—")} />
                  <EvidenceStat label={text.dataRange} value={`${String(summary.range_start ?? "—")} → ${String(summary.range_end ?? "—")}`} />
                  <EvidenceStat label={text.initialCash} value={`$${Number(summary.initial_cash ?? 0).toLocaleString()}`} />
                  <EvidenceStat label={text.commissionRate} value={`${(Number(summary.commission_rate ?? 0) * 100).toFixed(3)}%`} />
                  <EvidenceStat label={text.slippageModel} value={`${(Number(summary.slippage_fraction ?? 0) * 100).toFixed(3)}%`} />
                  <EvidenceStat label={text.pythonEngine} value={`${String(pythonReview.preview.engine_version ?? "—")} · ${String(pythonReview.preview.compiler_version ?? "—")}`} />
                  <EvidenceStat label={text.workerImage} value={String(pythonReview.preview.worker_image ?? "—")} />
                  <EvidenceStat label={text.sourceHash} value={String(pythonReview.preview.source_sha256 ?? "—")} />
      <EvidenceStat label={text.marketHash} value={String(pythonReview.preview.market_data_sha256 ?? "—")} />
                  <EvidenceStat label={text.manifestHash} value={String(pythonReview.preview.data_manifest_hash ?? "—")} />
                  <EvidenceStat label={text.calendarVersion} value={String(summary.calendar ?? "—")} />
                </div>
                <div className="mt-3 space-y-1 text-xs text-warning">
                  {(pythonReview.preview.warnings ?? []).map((warning) => <p key={warning}>{warning}</p>)}
                </div>
                {pythonReview.preview.can_run === false && <p className="mt-3 text-sm text-destructive">{text.pythonWorkerMissing}</p>}
                <p className="mt-3 break-all font-mono text-[10px] text-muted-foreground">{text.pythonReviewScope}: {pythonReview.preview.review_hash}</p>
                <button className="mt-4 inline-flex h-10 items-center gap-2 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-60" type="button" onClick={confirmPythonUpload} disabled={pythonBusy || !pythonReview.preview.preview_token}>
                  {pythonBusy && <LoaderCircle className="h-4 w-4 animate-spin" />}{pythonBusy ? text.pythonRunning : pythonReview.preview.can_run === false ? text.pythonWorkerMissing : text.confirmPythonUpload}
                </button>
              </div>
            );
          })()}
          {pythonResult && (
            <div className="mt-5 rounded-lg border border-success/30 bg-success/5 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="font-semibold">{text.pythonRunSuccess}</h3>
                <Link className="font-mono text-xs text-primary underline-offset-4 hover:underline" to={`/runs/${encodeURIComponent(pythonResult.run_id)}?view=dashboard`}>{text.openRun} · {pythonResult.run_id}</Link>
              </div>
              <p className="mt-2 break-all font-mono text-[10px] text-muted-foreground">{text.pythonVersion}: {String(pythonResult.execution_report.strategy_version_hash ?? "—")}</p>
              <div className="mt-4 grid gap-4 sm:grid-cols-3">
                <EvidenceStat label={text.finalValue} value={typeof pythonReport?.final_value === "number" ? pythonReport.final_value.toFixed(2) : "—"} />
                <EvidenceStat label={text.totalReturn} value={typeof pythonTotalReturn === "number" ? `${(pythonTotalReturn * 100).toFixed(2)}%` : "—"} />
                <EvidenceStat label={text.feesValue} value={typeof pythonReport?.total_fees === "number" ? pythonReport.total_fees.toFixed(2) : "—"} />
              </div>
              <p className="mt-3 text-xs text-success">{text.reportCheck}: {((pythonReport?.execution_integrity as Record<string, unknown> | undefined)?.status === "verified") ? text.verified : "UNAVAILABLE"}</p>
              <pre className="mt-4 max-h-80 overflow-auto rounded-md bg-background/80 p-3 font-mono text-[10px] leading-relaxed text-muted-foreground">{JSON.stringify(pythonResult.execution_report, null, 2)}</pre>
            </div>
          )}
        </section>

        <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
          <section aria-labelledby="execution-report-title" className="rounded-xl border border-border/60 bg-card shadow-sm">
            <div className="flex flex-col gap-3 border-b border-border/60 p-5 sm:flex-row sm:items-start sm:justify-between sm:p-6">
              <div>
                <div className="font-mono text-[10px] font-semibold uppercase tracking-[0.16em] text-warning">{text.reportEyebrow}</div>
                <h2 id="execution-report-title" className="mt-2 text-lg font-semibold">{text.reportTitle}</h2>
                <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{text.reportDescription}</p>
              </div>
              <span className="inline-flex w-fit items-center gap-1.5 rounded-sm border border-success/30 bg-success/5 px-2 py-1 font-mono text-[10px] uppercase tracking-wide text-success">
                <Check className="h-3 w-3" /> BackBroker verified
              </span>
            </div>

            <div className="grid gap-5 p-5 sm:grid-cols-3 sm:p-6">
              <EvidenceStat label={text.orders} value={text.orderValue} />
              <EvidenceStat label={text.fills} value={text.fillValue} />
              <EvidenceStat label={text.residual} value={text.residualValue} />
            </div>

            <div className="mx-5 mb-5 grid gap-2 border-y border-border/60 py-3 font-mono text-[11px] leading-relaxed text-muted-foreground sm:mx-6 sm:grid-cols-2 sm:gap-4">
              <div className="flex items-start gap-2"><ArrowDownRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />{text.sizing}</div>
              <div className="flex items-start gap-2"><Database className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />{text.fees}</div>
            </div>

            <div className="px-5 pb-5 sm:px-6 sm:pb-6">
              <div className="mb-4 flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
                <ArrowRight className="h-3.5 w-3.5" /> {text.traceTitle}
              </div>
              <div className="grid gap-1 sm:grid-cols-3 sm:gap-4">
                <TraceStep title={text.traceDecision} detail={text.traceDecisionDetail} />
                <TraceStep title={text.traceIntent} detail={text.traceIntentDetail} />
                <TraceStep title={text.traceFill} detail={text.traceFillDetail} final />
              </div>
            </div>
          </section>

          <section aria-labelledby="execution-boundary-title" className="rounded-xl border border-border/60 bg-card shadow-sm">
            <div className="p-5 sm:p-6">
              <div className="flex items-start gap-3">
                <div className="grid h-9 w-9 shrink-0 place-items-center rounded-md bg-warning/10 text-warning"><ShieldAlert className="h-4 w-4" /></div>
                <div>
                  <h2 id="execution-boundary-title" className="text-lg font-semibold">{text.boundaryTitle}</h2>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{text.boundaryIntro}</p>
                </div>
              </div>

              <div className="mt-5">
                <BoundaryRow title={text.available} detail={text.availableDetail} state="EVIDENCE ONLY" />
                <BoundaryRow title={text.python} detail={text.pythonDetail} state="OFFLINE ONLY" caution />
                <BoundaryRow title={text.realData} detail={text.realDataDetail} state="SOURCE DEPENDENT" />
                <BoundaryRow title={text.broker} detail={paperRuntimeDetail} state={paperRuntimeState} caution={paperRuntime?.dispatch_gate_open === true} />
                {paperRuntime?.last_error_code && (
                  <div className="mt-3 rounded-md border border-destructive/30 bg-destructive/5 p-3.5" role="status">
                    <div className="text-xs font-semibold text-destructive">{t("execution.paperLastErrorCode")}</div>
                    <code className="mt-1 block break-all font-mono text-xs text-foreground">{paperRuntime.last_error_code}</code>
                  </div>
                )}
                <div className="mt-4 rounded-lg border border-border/60 bg-background/60 p-4" aria-label={text.paperActivityTitle}>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-xs font-semibold">{text.paperActivityTitle}</h3>
                    {paperRuntime?.owner_lease_held === true && paperActivity !== null && (
                      <span className="text-[10px] text-muted-foreground" role="status" aria-live="polite">
                        {paperActivityStreamStatus === "connected"
                          ? text.paperActivityLive
                          : paperActivityStreamStatus === "reconnecting"
                            ? text.paperActivityStale
                            : text.paperActivityConnecting}
                      </span>
                    )}
                  </div>
                  {paperActivityUnavailable || paperActivity === null ? (
                    <p className="mt-2 text-xs text-muted-foreground">{text.paperActivityUnavailable}</p>
                  ) : (
                    <>
                      {paperActivity.control_reason === "runtime_stopped_broker_snapshot_unavailable" && <p className="mt-2 text-xs text-muted-foreground">{text.paperActivityStopped}</p>}
                      <div className="mt-3 grid gap-3 sm:grid-cols-3">
                        <div className="rounded-md border border-border/50 p-3">
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{text.paperPositionTitle}</div>
                          <p className="mt-1 text-xs font-medium">{paperActivity.positions.source === "ibkr_reconciled_snapshot" ? text.paperPositionAsOf : text.paperPositionUnknown}</p>
                          {paperActivity.positions.as_of && <p className="mt-1 text-[10px] text-muted-foreground">{paperActivity.positions.as_of}</p>}
                          {paperActivity.positions.positions.map((position) => (
                            <p key={position.instrument_id} className="mt-2 text-xs text-foreground">
                              {position.instrument_id}: {position.quantity} {position.currency}
                            </p>
                          ))}
                        </div>
                        <div className="rounded-md border border-border/50 p-3">
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{text.paperRiskTitle}</div>
                          <p className="mt-1 text-xs text-muted-foreground">{text.paperRiskLedgerOnly}</p>
                          <p className="mt-2 text-xs">{text.paperReservedCash}: {paperActivity.risk.reserved_cash}</p>
                          <p className="mt-1 text-xs">{text.paperReservedShares}: {paperActivity.risk.reserved_sell_shares}</p>
                        </div>
                        <div className="rounded-md border border-border/50 p-3">
                          <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{text.paperRecoveryTitle}</div>
                          <p className="mt-2 text-xs">{text.paperUnresolved}: {paperActivity.recovery.unresolved_sends}</p>
                          <p className="mt-1 text-xs">{text.paperActiveOrders}: {paperActivity.recovery.active_orders}</p>
                        </div>
                      </div>
                      <div className="mt-4">
                        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{text.paperOrders}</div>
                        {paperActivity.orders.length === 0 ? (
                          <p className="text-xs text-muted-foreground">{text.paperNoOrders}</p>
                        ) : (
                          <div className="space-y-2">
                            {paperActivity.orders.map((order) => (
                              <div key={order.intent_id} className="rounded-md border border-border/50 p-3">
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <span className="text-xs font-semibold">
                                    {order.intent.side} {order.intent.integer_qty} {order.intent.instrument_id}
                                  </span>
                                  <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium">{order.state}</span>
                                </div>
                                <p className="mt-1 text-[11px] text-muted-foreground">
                                  {text.paperOrder} {order.intent_id} · {order.filled_qty} {text.paperFilled} · {order.remaining_qty} {text.paperRemaining}
                                </p>
                                {order.broker_order_id != null && (
                                  <p className="mt-1 text-[11px] text-muted-foreground">{text.paperBrokerOrderId}: {order.broker_order_id}</p>
                                )}
                                {paperRuntime?.owner_lease_held === true && (paperRuntime.phase === "READY" || paperRuntime.phase === "STOPPING") && (order.state === "ACKNOWLEDGED" || order.state === "PARTIALLY_FILLED") && (
                                  <button type="button" className="mt-2 rounded-md border border-warning/50 px-2 py-1 text-xs text-warning disabled:opacity-50" disabled={paperCancelBusy !== null} onClick={() => void cancelPaperOrder(order.intent_id)}>
                                    {paperCancelBusy === order.intent_id ? text.paperCancelling : text.paperCancelOrder}
                                  </button>
                                )}
                                {paperCancelNotice?.intentId === order.intent_id && <p className="mt-2 text-xs text-muted-foreground" role={paperCancelNotice.ok ? "status" : "alert"}>{paperCancelNotice.ok ? text.paperCancelSent : text.paperCancelError}</p>}
                                {order.fills.length > 0 && (
                                  <div className="mt-2 space-y-1 border-t border-border/40 pt-2">
                                    {order.fills.map((fill) => (
                                      <p key={fill.broker_exec_id} className="text-[11px] text-muted-foreground">
                                        {fill.quantity} @ {fill.price} · {fill.occurred_at} · {text.paperExecutionId}: {fill.broker_exec_id}
                                      </p>
                                    ))}
                                  </div>
                                )}
                                {order.fees.length > 0 && (
                                  <p className="mt-2 text-[10px] text-warning">
                                    {text.paperFeesUnconfirmed}: {order.fees.map((fee) => `${fee.amount} ${fee.currency}`).join(", ")}
                                  </p>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </>
                  )}
                </div>
                {paperRuntime !== null && !paperRuntime.composition_ready && paperMissingRequirements.length > 0 && (
                  <div className="mt-3 rounded-md border border-warning/30 bg-warning/5 p-3.5" aria-label={t("execution.paperMissingRequirementsTitle")}>
                    <h3 className="text-xs font-semibold text-warning">{t("execution.paperMissingRequirementsTitle")}</h3>
                    <ul className="mt-2 space-y-2">
                      {paperMissingRequirements.map(({ code, description }) => (
                        <li key={code} className="text-xs leading-relaxed text-muted-foreground">
                          <span className="font-medium text-foreground">{description}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {paperRuntime?.dispatch_gate_open === true && (
                  <div className="mt-3 rounded-md border border-warning/30 bg-warning/5 p-3.5">
                    <button
                      className="inline-flex h-9 items-center gap-2 rounded-md border border-warning/50 px-3 text-xs font-semibold text-warning hover:bg-warning/10 disabled:cursor-not-allowed disabled:opacity-60"
                      type="button"
                      onClick={pausePaperExecution}
                      disabled={paperPauseBusy}
                    >
                      {paperPauseBusy && <LoaderCircle className="h-3.5 w-3.5 animate-spin" />}
                      {text.paperPauseButton}
                    </button>
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{text.paperPauseHint}</p>
                    {paperPauseError && <p className="mt-2 text-xs text-destructive" role="alert">{text.paperPauseFailure}</p>}
                  </div>
                )}
                {paperRuntime?.configured === true && paperRuntime.composition_ready &&
                  paperRuntime.phase === "STOPPED" && paperRuntime.strategy_version_id && (
                    <div className="mt-3 rounded-md border border-border/60 bg-muted/20 p-3.5">
                      <button
                        className="inline-flex h-9 items-center gap-2 rounded-md border border-border px-3 text-xs font-semibold hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
                        type="button"
                        onClick={startPaperExecution}
                        disabled={paperControlBusy}
                      >
                        {paperControlBusy && <LoaderCircle className="h-3.5 w-3.5 animate-spin" />}
                        {paperControlBusy ? text.paperControlBusy : text.paperStartButton}
                      </button>
                      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{text.paperStartHint}</p>
                      {paperControlError && <p className="mt-2 text-xs text-destructive" role="alert">{text.paperControlFailure}</p>}
                    </div>
                  )}
                {paperRuntime?.configured === true && paperRuntime.composition_ready &&
                  paperRuntime.phase === "READY" && paperRuntime.control_state === "PAUSED" &&
                  !paperIntegrationTest?.configured && (
                    <div className="mt-3 rounded-md border border-warning/30 bg-warning/5 p-3.5">
                      <button
                        className="inline-flex h-9 items-center gap-2 rounded-md border border-warning/50 px-3 text-xs font-semibold text-warning hover:bg-warning/10 disabled:cursor-not-allowed disabled:opacity-60"
                        type="button"
                        onClick={activatePaperExecution}
                        disabled={paperControlBusy}
                      >
                        {paperControlBusy && <LoaderCircle className="h-3.5 w-3.5 animate-spin" />}
                        {paperControlBusy ? text.paperControlBusy : text.paperActivateButton}
                      </button>
                      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{text.paperActivateHint}</p>
                      {paperControlError && <p className="mt-2 text-xs text-destructive" role="alert">{text.paperControlFailure}</p>}
                    </div>
                  )}
                {paperIntegrationTest?.configured === true && (
                  <div className="mt-3 rounded-lg border border-border/70 bg-background/70 p-4" aria-label={text.paperIntegrationTitle}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h3 className="text-xs font-semibold">{text.paperIntegrationTitle}</h3>
                        <a className="mt-1 inline-block text-xs text-primary underline" href="/PAPER_USER_GUIDE.html" target="_blank" rel="noopener noreferrer">{text.paperUserGuide}</a>
                        <p className="mt-1.5 text-xs text-muted-foreground">{text.paperSignalDateHint}</p>
                        <p className="mt-1.5 text-xs text-foreground">{text.paperIntegrationOrder}</p>
                        <p className={cn("mt-1.5 text-[10px]", paperIntegrationTest.backtest_attestation?.status === "verified" ? "text-success" : "text-warning")}>
                          {paperIntegrationTest.backtest_attestation?.status === "verified"
                            ? `${text.paperIntegrationBacktestVerified} · ${paperIntegrationTest.backtest_attestation.evaluation_start ?? "?"} — ${paperIntegrationTest.backtest_attestation.evaluation_end ?? "?"}`
                            : text.paperIntegrationBacktestBlocked}
                        </p>
                        <p className="mt-2 text-[10px] text-muted-foreground">
                          {text.paperIntegrationWindow}: {paperIntegrationTest.valid_window_utc.start} — {paperIntegrationTest.valid_window_utc.end}
                        </p>
                      </div>
                      <span className={cn(
                        "rounded-full border px-2 py-1 text-[10px] font-semibold",
                        paperIntegrationTest.window_open && paperIntegrationTest.enabled
                          ? "border-success/40 bg-success/10 text-success"
                          : "border-border text-muted-foreground",
                      )}>
                        {paperIntegrationTest.enabled
                          ? paperIntegrationTest.window_open ? text.paperIntegrationWindowOpen : text.paperIntegrationWindowClosed
                          : text.paperIntegrationDisabled}
                      </span>
                    </div>
                    <button
                      className="mt-3 inline-flex h-9 items-center gap-2 rounded-md border border-warning/50 px-3 text-xs font-semibold text-warning hover:bg-warning/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-warning disabled:cursor-not-allowed disabled:opacity-50"
                      type="button"
                      onClick={runPaperIntegrationTest}
                      disabled={paperIntegrationBusy || paperIntegrationTest.backtest_attestation?.status !== "verified" || !paperIntegrationTest.enabled || !paperIntegrationTest.window_open || paperRuntime?.configured !== true || paperRuntime.composition_ready !== true || !(paperRuntime.phase === "STOPPED" || (paperRuntime.phase === "READY" && paperRuntime.control_state === "PAUSED"))}
                    >
                      {paperIntegrationBusy && <LoaderCircle className="h-3.5 w-3.5 animate-spin" />}
                      {paperIntegrationBusy ? text.paperIntegrationRunning : text.paperIntegrationButton}
                    </button>
                    {paperIntegrationError && <p className="mt-2 text-xs text-destructive" role="alert">{paperIntegrationError === "signal_date" ? text.paperSignalDateHint : text.paperIntegrationError}</p>}
                    {paperIntegrationResult && (
                      <p className={cn("mt-2 text-xs", paperIntegrationResult.post_order_reconciliation === "verified" ? "text-success" : "text-warning")} role="status">
                        {paperIntegrationResult.post_order_reconciliation === "verified" ? text.paperIntegrationVerified : text.paperIntegrationReconcileFailed}
                        {paperIntegrationResult.activity?.orders.length
                          ? ` ${text.paperIntegrationOrderSeen}`
                          : ` ${text.paperIntegrationNoOrder}`}
                      </p>
                    )}
                  </div>
                )}
                {paperRuntime?.configured === true && (
                  <div className="mt-3 rounded-lg border border-border/70 bg-background/70 p-4" aria-label={text.paperCandidatesTitle}>
                    <h3 className="text-xs font-semibold">{text.paperCandidatesTitle}</h3>
                    <p className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground">{text.paperCandidatesDescription}</p>
                    {!paperStrategyCandidates || paperStrategyCandidates.items.length === 0 ? (
                      <p className="mt-2 text-xs text-muted-foreground">{text.paperCandidatesEmpty}</p>
                    ) : (
                      <div className="mt-3 space-y-2">
                        {paperStrategyCandidates.items.map((candidate) => (
                          <div key={candidate.strategy_version_id} className="rounded-md border border-border/50 p-3">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <span className="text-xs font-semibold">{candidate.strategy_id}</span>
                              <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", candidate.paper_deployable ? "border-success/40 bg-success/10 text-success" : "border-border text-muted-foreground")}>
                                {candidate.currently_deployed ? text.paperCandidateReady : candidate.paper_deployable ? text.paperCandidateSelectable : text.paperCandidateBacktestOnly}
                              </span>
                            </div>
                            <p className="mt-1.5 break-all font-mono text-[10px] text-muted-foreground">{candidate.strategy_version_id}</p>
                            <p className="mt-1 text-[10px] text-muted-foreground">
                              {candidate.source_kind} · {candidate.instrument_ids.join(", ")} · {candidate.execution_order_type.toUpperCase()} · {candidate.position_mode}
                            </p>
                            <p className="mt-1 text-[10px] text-muted-foreground">
                              {candidate.evaluation_start ?? "?"} — {candidate.evaluation_end ?? "?"} · {text.manifestHash}: {candidate.data_manifest_hash.slice(0, 16)}…
                            </p>
                            {!candidate.paper_deployable && candidate.paper_block_reasons.length > 0 && (
                              <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-muted-foreground">
                                {candidate.paper_block_reasons.map((reason) => (
                                  <li key={reason}>{paperCandidateReasons[reason]?.[i18n.resolvedLanguage?.startsWith("zh") ? 1 : 0] ?? reason}</li>
                                ))}
                              </ul>
                            )}
                            {candidate.paper_deployable && !candidate.currently_deployed && (
                              <button
                                type="button"
                                className="mt-2 rounded-md border border-primary/40 px-2.5 py-1 text-[10px] font-medium text-primary disabled:opacity-50"
                                disabled={paperCandidateSelecting !== null || paperRuntime?.phase !== "STOPPED"}
                                onClick={() => void selectPaperStrategyCandidate(candidate.strategy_version_id)}
                              >
                                {paperCandidateSelecting === candidate.strategy_version_id ? text.paperCandidateSelecting : text.paperCandidateSelect}
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                    {paperCandidateSelectError && <p className="mt-2 text-xs text-destructive" role="alert">{text.paperCandidateSelectError}</p>}
                  </div>
                )}
              </div>

              <div className="mt-4 rounded-md border border-warning/30 bg-warning/5 p-3.5">
                <div className="flex items-center gap-2 text-xs font-semibold text-warning">
                  <AlertTriangle className="h-3.5 w-3.5" /> {text.warning}
                </div>
                <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">{text.warningDetail}</p>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
