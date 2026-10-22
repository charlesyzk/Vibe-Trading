## D-120 备份恢复必须原子暂停账户门闸与策略 journal

数据库恢复把已有 execution control 置为 `RECOVERING` 的同时，必须在同一 SQLite `BEGIN IMMEDIATE` 事务将所有 `execution_run.state='RUNNING'` 更新为 `PAUSED`。这样 journal 的 event staging/release 不会仅因旧 run 状态仍为 RUNNING 而绕过账户恢复门。若 `execution_run` 表存在但缺少必要状态字段，恢复回滚并 fail-closed。恢复后的 run 只能通过受信 recovery orchestrator 校验 exact released event 身份后继续；暂停状态下的 outbox 仅能通过明确的 paused recovery 读取。该原子性不替代 broker reconciliation 或异机恢复演练。

## D-121 Backtrader report version 与运行时机器身份分开

用户可读报告保留 `Backtrader 1.9.x / BackBroker` 等显示文本；不可变 `StrategyVersionRecord.engine_version` 必须使用跨 NL2/Python 与 PAPER validator 共用的 `backtrader/{backtrader.__version__}`。不得以显示标签替代运行时兼容身份，否则刚生成的 NL2 report version 会被 PAPER composition 拒绝。engine 变化仍会改变 version hash。

## D-122 DataManifest v1.1 与 provider snapshot 自洽

新 `DataManifest` 默认 v1.1 并绑定三字母大写货币；v1.0 历史结构保留只读解析。MarketDataSnapshot 对 manifest 声明的首末区间、warmup/evaluation 内部边界、bar available_at 与 fetched_at 顺序，以及 source/provider、adjustment 和 currency 一致性做 fail-closed 校验，并只接受 completed bars。此契约不能验证供应商身份、许可、真实点时可用性或 mapping 权威性。

## D-123 恢复 released event 需同库 execution gate 已启动

对于与 PAPER execution_control 共用 SQLite 的 journal，PAUSED→RUNNING 的 released-event 恢复要求同一个 account/environment 的 IBKR gate 已是 RUNNING；RECOVERING、PAUSED、STOPPING、STOPPED、ERROR、scope 缺失或 schema 不支持均拒绝。该门约束持久一致性，不声称自行验证 owner lease/grant/fresh broker reconciliation；managed service 仍必须先完成这些验证后才启动 gate。仅含离线 journal 的数据库不受 PAPER gate 表约束。

## D-124 指定 YFinance 回测请求不经通用 provider fallback

对 NL2 trusted snapshot 的 `source=yfinance` 请求，直接实例化 Vibe 的 concrete YFinance loader，并核对其 loader name；缺少所需 symbol 或身份不符时失败，不用 registry/fallback 补齐。host provenance 记录请求参数、loader class/name、cache hit/miss、宿主调用时间、loader 输出表、normalized OHLCV 与最终 CSV 的独立 SHA-256。loader 输出表 hash 不是 raw HTTP response hash；缓存命中时 retrieved_at 仅表示本次宿主获取调用时间，不证明数据何时由上游发布。来源身份、许可、PIT/可用时点、复权和 broker contract mapping 一律保持 `UNVERIFIED`。此决定不改变 Python upload 的 caller-declared provider 路径，也不意味着其它 source 已摆脱 legacy fallback。

## D-125 Python upload PAPER 需独立 frozen-version composition

Python upload 部署必须使用 `PythonUploadPaperSessionBuilder` 与对应 composition factory，将不可变 version/source hash/compiler/Backtrader engine/policy/feed-contract/共享 SQLite 精确绑定；事件通过 `PythonEventCoordinator` 和显式配置 `PythonSandbox`，再复用 managed Cerebro/PortBroker session 与 runtime 的同一账本。工厂构造只验证本地对象，不启动 SDK、不获取账户 owner lease，也不授权账户写入。该离线 composition 尚未安装到主应用；Python upload PAPER 仍受 OS/网络/凭据隔离的生产级准入、可信 grant/provider 和明确用户授权约束。

## D-126 PAPER app root 与容器隔离必须固定策略身份和 seccomp

B184 要求 `PaperExecutionComposition` 的 runtime factory 持有 lowercase SHA-256 frozen strategy pin；start API 必须在授权器和 runtime factory 前核验请求版本精确匹配，否则返回 409 且零管理/运行副作用。PythonSandbox 每次运行前解析 Docker daemon SecurityOptions，要求唯一 seccomp profile 为 builtin/default；运行时显式传 `seccomp=builtin`。该门只确认当前 Docker daemon 的配置报告和本次容器请求，不证明配置免于内核/容器逃逸，也不替代上传代码的完整安全准入。

## D-127 daily calendar session 的 UTC 开收盘必须落在声明交易日

B185 在 importer 精确校验 bar 时间戳与受信 schedule 后，再将 session 的 UTC `start_at`/`end_at` 转换到 manifest `timezone`，要求两者的 local date 都等于 `session_date`。这样 caller 不能将正确的 UTC 时间和 bar 标签挂到相邻交易日。它保持日线 session 的 local-date 语义，不认证日历授权、供应商事实、迟到/修订数据或 point-in-time availability。

## D-128 策略线程失败后继续持久消费回报，Backtrader 通知等待恢复

B186 在 Cerebro strategy thread 失败并关掉 execution gate 后，若 callback worker 仍健康，先在安全暂停边界 quiesce delivery，再把 sink 切为 no-op 并恢复 worker。Callback pump 继续处理 ledger/recovery facts 和受授权管理撤单；delivery ID 保持未 ACK，旧 PortBroker queue 不再增长。由于原 Cerebro 已退出，Backtrader native notifications 不再消费，必须由显式 session recovery 重放，不能在旧 session 上直接 rearm。该离线 fake PAPER behavior 不证明真实 Gateway 连续性。

## D-131 Execution UI 缺失运行身份时不得展示样例身份

用户创建的报告如果缺少 backend/version/mode/data source 字段，Execution 页必须显示 `Unknown`，不能用静态合成样例中的 Backtrader 版本或 SMA template version 兜底。NL2 与 Python upload 成功卡都显式显示 strategy version hash 和 replay integrity 状态。样例首页无用户报告时仍可保留样例默认值。

## D-132 failed-session recovery 的未 ACK callback 必须保留到显式恢复

Cerebro 已失败时，callback worker 将未完成 Backtrader delivery 保留在 durable inbox，并继续持有 STOPPING/owner lease，直到 recovery 将 native callback 交给新 session。测试不能为了无后台线程而 ACK 或清除该事件；fake runtime 的测试结束前，必须先证明 raw spool 已处理且 unACKed inbox 仍持久，再停测试 worker。生产端仍按原 quiescence/recovery 约束工作。

## D-130 Trusted template sizing claims 由独立 replay 状态重算

B190 的独立 BackBroker replay 返回订单决策时刻的现金、权益、持仓和当根收盘价；host 从 frozen StrategySpec 与被运行时使用的 budget/fee/buffer 配置调用 Decimal `size_target_fraction`，核对 report 的 requested/accepted/target shares/reason 及 sizing provenance。报告不把认可的 sizing 字段仅作为 schema-valid 而直接宣称 verified。信号触发原因和 coordinated decision-log/order tamper 不在此证明范围。

## D-129 Python-upload 独立报告严格约束订单字段 schema

B189 将 B188 建立的封闭 order schema 用于 NL2 trusted SMA/RSI 回测：`target_shares`、`sizing_requested_qty`、`accepted_qty` 等模板 sizing keys 加入明确 schema，未知 order keys 在正式 verifier 拒绝。两入口都在发布 verified report 前校验。Sizing metadata 并未在订单日志 replay 中独立重算；冻结模板实现及相应计算仍是其证据来源。共同 Backtrader/会计算法错误、任意 Broker/Analyzer 篡改、sidecar/config 的独立信任根仍未解决。

B188 在 Python-upload 的独立 BackBroker replay verifier 中对 report 和 replay 的 order rows 启用 strict mode。经校验的字段以外（包括额外 execution/PnL claims）直接拒绝，不允许只比较白名单时静默丢弃附加字段。攻击测试通过修改真实 Backtrader 产物中的额外 order key 验证拒绝，但未证明任意 Broker/Analyzer 攻击、两实现无共因错误或 sidecar 配置有独立信任根。

## D-117 Python explicit_quantity 不足购买力时保留用户股数并由 BackBroker 明确拒单

Python 上传的明确股数是交易意图的一部分，host 和报告链不得按现金/预算/报价静默缩量。离线 BackBroker 会按真实撮合资金判断保留订单为 Margin、零成交；报告保留 proposal 数量并由独立真实 BackBroker 重放校验。PAPER 派单仍由 host 风险授权在发送前阻断或允许，离线 BackBroker Margin 不能替代券商/账户拒单证据。

## D-116 imported manifest 窗口端点必须落在实际完整交易日集合

市场数据 importer 除精确匹配 expected session schedule 外，要求 `warmup_start` 等于已导入首 session，`evaluation_end` 等于末 session，`warmup_end` 与 `evaluation_start` 都在实际交易日集合中；区间先后/包含关系由 `DataManifest` 继续校验。这样所有复用 importer 的下游在生成 feed/报告前都得到可兑现的窗口，不依赖特定 runner 的第二道护栏。该完整性不认证 provider 的声明、许可或点时真实性。

## D-115 managed-session 任意启动失败都必须关闭 run 与 PAPER send gate

从 runtime.start_paused 开始，reconciliation、snapshot verification、shadow replay、outbox handoff、Backtrader hydration、active-order restore、CREATED staging、callback barrier 和 strategy-thread startup 均处在一个失败收敛边界内。抛出异常时必须 best-effort 将 execution_run 持久 PAUSED；若 durable execution scope 已为 RUNNING，则在 owner lease 仍持有时恢复 PAUSED。不能在失败 session 上直接 activate；恢复需要重新构造并完成 fresh reconciliation。因为 journal/ledger 写入也可能失败，调用方仍需把该情况视为无法证明持久暂停，不能声称安全成功。此机制不替代真实 Gateway 故障注入验收。

## D-114 active BUY 恢复必须重建逐笔 execution bits，不用 aggregate 伪造单笔成交

B169 的 `ReconciledOrderState` 携带经验证的 `ReconciledFill` 序列。恢复要求每笔 execution ID 唯一、fill rows 按 aware UTC timestamp 和 ID 确定排序，序列数量/加权价格/最新成交时间/可用佣金和 active order aggregate 一致；持仓快照减去这组 fill 后还必须得到正当的既有 position cost basis。PortBroker 按原始逐笔数量、价格和时间调用真实 Backtrader Order.execute，重建 `executed.exbits`，不触发新的派单。ledger 未提供可靠 per-fill commission 时不猜费用分配。此能力仍限于受支持 BUY order 状态和已对账 snapshot，不代表 Gateway 恢复验收，T41/T42 仍 PARTIAL。

## D-113 Python upload 的 warmup 只建立状态，交易和归一化统计从 evaluation_start 开始

B168 要求数据 manifest 的日期字段构成完整有序区间：`range_start == warmup_start <= warmup_end < evaluation_start <= evaluation_end == range_end`，warmup_end/evaluation_start 都必须出现在已验证 bars 中。所有 warmup bars 继续进入隔离 worker，供用户策略计算指标和状态；但 host 丢弃 warmup 的 order proposals，不创建 BackBroker native order。evaluation_end 后不接收 bars。报告明确输出 manifest evaluation window、evaluation bar count、被忽略 warmup proposal count；equity curve 保留完整输入轨迹，warmup 本身保持无成交。年化 return 的 daily periods 只计 evaluation bars。独立 BackBroker replay 绑定同一 evaluation_start 并核对上述统计窗口。此决定适用于 Python upload；数据供应商和真实行情真实性不因此获得认证。

## D-112 coordinator 必须独立重验 frozen StrategySpec 的 bar 约束

B167 将 `PythonEventCoordinator` 设为独立信任边界：stage 新 completed bar 前以及调用 worker 前，按不可变 StrategySpec 校验 instrument、USD、timezone、XNYS session 日期/精确 UTC 开收盘与 `available_at <= trusted_now`。已持久 released event 和 shadow replay 输入也要重验；违反时先将 journal 持久 PAUSED。受信任 clock 在 Python/managed PAPER composition 中与 completed-bar feed 共用，避免 feed 和 coordinator 对未来 bar 的判断不一致。该约束只验证冻结合同和 pinned calendar 的时间结构，不证明行情供应商来源、许可、point-in-time 或企业行动数据准确；T07/T09 仍 PARTIAL。

# 实施决定与待决事项

更新：2026-10-22。这里记录实现中已经冻结的选择；设计文档建议值若未在此处确认，不代表交易授权。

## D-111 NL2 澄清 transcript 采用本地 append-only turn 链

B166 为每次 trusted-template preview 创建独立 transcript。初始策略描述是根 turn；每条答复通过 caller 生成的 UUID、parent turn UUID、逐轮 answer 与上下文 SHA-256 追加。服务端要求完整描述严格等于 parent 描述后追加 `User clarification: {answer}`，turn 行不可 update/delete；相同最新 turn ID 与内容可重放，内容冲突、stale parent、上下文变化和达到 32 轮上限均拒绝。上下文绑定标的、instrument、日期、source 和 allocation。用户改动输入后新建 preview/transcript；原 preview token 仍按既有请求语义绑定实际运行 payload，不让 transcript metadata 更改执行内容。DB 使用本机 RUNS_DIR 文件权限 0600。当前不把 actor/tenant 纳入 transcript scope，也没有 retention、删除或多用户隐私边界，故不得把此实现当作生产级个人数据生命周期方案；T12 保持 PARTIAL。

## D-110 有 CREATED intent 和 released retry bar 时，只在成交后处理策略事件

B164 只允许当前 run 的 committed prefix 留下唯一、同 scope/version/deployment 且 exact-linked 的 CREATED BUY，同时存在 cursor 上唯一的 released-but-uncommitted 下一 bar。恢复完成后仍 PAUSED；显式 activate 首先让 Cerebro broker cycle 按原 dispatch service 做二次风控并发送旧 intent。策略 event 保持未提交，不在 SEND_STARTED/UNKNOWN、ACKNOWLEDGED 或 PARTIALLY_FILLED 状态下调用 worker。B165 将推进条件收紧为 callback 已将 ledger 收敛为 FILLED，且 execution gate RUNNING、owner lease 有效，Cerebro 线程才恢复 exact event ID/seq/payload 并推 bar。CANCELLED/REJECTED 可能遇到迟到 execution，因此持久暂停并要求 fresh reconciliation。若风险拒绝或发送结果不确定，也保持暂停并重走恢复/对账；不得盲目把 retry bar 与未决订单并行。这个单订单/单 bar 范围不证明 Gateway callback 完整性。

## D-109 唯一尚未发送的 CREATED intent 可随安全停止保留

B163 的 managed-session stop 先持久暂停策略 journal 并关闭 execution send gate，再读取账本恢复状态。仅当唯一未发送工作是 B162 已验证的同 run/version/account/deployment、精确 committed-outbox-linked CREATED BUY 时，允许停止 session/runtime；保留原 intent 状态为 CREATED。此状态表示本地未 reserve/send，不能称为券商撤单、已取消或已完成。重新运行仍须 fresh reconciliation、outbox lineage 校验和显式 activate，派单时再次运行现有二次风控。存在其他活动单、未决发送或不支持状态时，stop 继续拒绝完成并保持 PAUSED。该决定不证明 broker-side status，也不解除 T41/T42 限制。

## D-108 恢复仅暂存唯一精确链接的 CREATED BUY，发送仍须显式 activate

B162 在 fresh reconciliation、committed-prefix shadow replay 和 outbox link 完整性核验之后，只允许一个同 run/version/deployment/account、market/DAY BUY 的 CREATED intent 进入 PortBroker 暂存队列。PAUSED 期间 Cerebro 不创建 native order；显式 activate 后由 Cerebro broker thread 用全新 native ref 经过现有 BacktraderDispatchPort 与当前第二次风控。拒绝时保持 CREATED、零 sender，并沿策略线程失败路径暂停 run/scope。不会重放 Python event 或重复 authorizer。多条、unlinked、scope/version 不符及其他 ledger 状态拒绝；不支持跨重启 ref、SELL、UNKNOWN/READY_TO_SEND恢复。B164 只放宽“单一 exact-linked CREATED BUY + 同 run 单一 released retry bar”这一组合，并要求旧 intent 先有终态 callback；其他 failed-bar 混合仍拒绝。fake PAPER transport 不证明真实 Gateway 账户状态。

## D-106 PAPER 管理 grant 精确绑定可归因身份、scope、动作和时间窗

B161 新增 SQLite-backed `PaperControlGrantStore`。每条 grant 绑定 `Principal.subject`、auth method 与 tenant、IBKR/PAPER account scope、单一 `start/activate/recover/cancel/stop` 动作及 `[valid_from, valid_until)` 时间窗；记录 issuer 身份，撤销后立即拒绝，数据库重开仍生效。manager 的统一 `_authorize()` 可将该 store 作为 `control_authorizer`，且在 start runtime factory 前校验。签发只允许可信部署/控制面代码调用，当前没有签发 API、可归因 identity provider 或生产 root；store 不让 shared-key/loopback 变成 attributable principal，也不能单独作为真实 PAPER readiness。

## D-104 managed Python PAPER 启动前重算 committed event prefix

B158 在 `SQLiteEventJournal` 独立保存创建 run 时的 `initial_checkpoint_json`；managed session 完成 fresh account reconciliation 且保持 PAUSED 后，使用冻结 Python version/compiler 和 durable completed-bar prefix 逐事件 shadow replay。实现只读校验 event/result hash、连续 seq、完整策略输出、outbox proposals 与最终 checkpoint，不调用 commit、handoff 或 dispatch；不匹配时 journal 保持 PAUSED 并阻止 Cerebro 启动/恢复。历史 run 缺少初始 seed 时不能猜测并 fail-closed。当前证据只覆盖 JSON event API，不等于通用 Backtrader native indicator、订单/PnL 或真实 Gateway 状态重建。

## D-105 managed recovery 先补 outbox→ledger，再拒绝无法重建的 native ref

B159 在 fresh reconciliation 与 D-104 shadow replay 都成功后，允许 recovery handoff 读取 RUNNING/PAUSED 的同 run dispatchable outbox。PENDING proposal 用当前可信 authorizer 作一次授权并冻结；已有 RESERVED row 只重放原 prepared intent/decision。Journal 持久化两者的 SHA-256，并在 retry 前再次对照 immutable event proposal/run identity；partial hash mismatch 会持久 PAUSE。此阶段只完成跨库 create/link，不 reserve 或 send。新增 CREATED intent 仍不具备可安全假造的 Backtrader native order/ref，因此 recovery 随后保持 PAUSED 并 fail-closed；这个 durable handoff 解决双库分裂，但不代表完整 session resume。

## D-103 Python upload 的 session schedule 由 pinned XNYS 日历确定

首期 Python upload 只接受 `XNYS@4.13.2` 与 `America/New_York`。可信 API 从 metadata 的 warmup_start 至 evaluation_end 生成完整 inclusive schedule，并要求客户端提交的 session 日期、UTC 开收盘和 early-close 全量匹配；若声明 `calendar_schedule_sha256`，还必须与 canonical schedule 一致。随后 importer 使用宿主 schedule 并将它的 hash 写入 manifest。该限制只适用于 Python upload API，通用 importer 仍可由未来受信任市场适配器调用。pinned package 不构成交易所官方日历授权或真实行情/许可证明。

## D-102 NL2 composition 必须由可信部署 root 显式构造和安装

B156 的 `build_frozen_nl2_paper_composition()` 只接受显式 scope/config/dependencies、安装的冻结 trusted NL2 version、固定 run/deployment identity、risk/facts providers、verified feeds/contracts、clock 与 control authorizer；不从环境变量或 HTTP 请求推断账户和配置。它返回版本 pinned、readiness-complete 的 composition，但不把它安装进 FastAPI，也不启动 SDK/lease/Gateway。app 默认保持 inert；真实生产 root 必须先提供可归因授权及可核验 provider，之后单独审批真实 PAPER 参数和连接。

## D-101 PAPER 活动事件从账本同事务序列化并按 Last-Event-ID 补传

B155 选择 SQLite append-only scope-local event sequence，与相应 order/reservation/send/broker/fill/fee/control ledger mutation 在同一 transaction 写入；API 仅从 manager 当前 trusted scope 查询，SSE 认证后 pull SQLite，不借用可能丢消息的进程内 EventBus。snapshot 暴露 `latest_activity_seq`，首次连接从 snapshot cursor 开始；重连接受标准 `Last-Event-ID` header 或 query，事件仍使用 scope-local seq 作为 SSE ID。检测到缺号关闭连接并发 `activity.gap`，不猜测事件已同步；当前没有事件清理/压缩策略。

## D-100 PAPER activity 只从可信 active scope 派生，账本值不得冒充券商事实

B153 的只读活动投影要求 API 不接收 account/environment 参数；manager 从当前已启动的 trusted `PaperRuntimeScope` 查询最近有界委托、成交、费用、recovery queue 和本地 reservation。行项目裁剪重复 broker/account/environment 字段，fee amount 保持 `REPORTED_UNCONFIRMED`。ledger reservation 摘要不等于实时 risk limit；ledger fill 也不等于账户持仓。只有 runtime 提供可验证的最新 reconciliation snapshot 才展示券商持仓，否则必须返回空 positions + `source=unknown`。此 API 不启动 runtime、不连接 Gateway、不构成真实 PAPER readiness。

## D-099 NL2 managed deployment 固定 immutable version，request 不得选择策略或账户

B151 将 `ManagedPythonPaperDeploymentFactory` 的可选 pin 固定为 lowercase SHA-256 strategy version；session 构造后必须与 factory pin 一致。若 factory 已 pin，`POST /api/execution/paper/start` 请求只能回显相同 version ID，scope 仍只来自 trusted app state。B152 `TrustedTemplatePaperSessionBuilder` 仅接受可信 frozen `sma_cross@1.0` / `rsi_threshold@1.0`、完整受支持的日线/RTH/Market-DAY/long-only/target-fraction 策略，且将 event journal 与 runtime ledger 放进同一 SQLite；account/grant/quote/risk provider、feed 与 contract resolver 都必须显式注入。builder 构造不启动 SDK、不取 lease、不读取实时事实；其存在不表示 app production composition 已配置。 |

## D-098 NL2 PAPER 模板只输出信号，target_fraction 股数由 host 事实计算

B150 的 managed trusted-template 路径保留 `NATURAL_LANGUAGE_TEMPLATE` 来源，不加载 frozen template artifact 为可执行用户代码。event coordinator 校验版本 source hash、模板 bytes、compiler 和 Backtrader engine 版本；SMA 最近收盘与 RSI Wilder seed/状态 checkpoint、event result、outbox 继续写入同一 journal。模板只产生 BUY/SELL transition，`TargetFractionPaperSizer` 从 host 提供且与账户、版本、标的绑定的新鲜 grant/account/quote facts 计算 whole-share quantity；hand-off 再独立拉取事实并对精确数量做授权风险评估。过期、不完整或身份不符的事实在创建 intent 前拒绝。B151 已用 managed session + fake callback 覆盖 SMA BUY 与 RSI BUY→SELL native order。B152 新增显式注入 providers/feed/contract 的可信 session builder；生产 app root、真实授权/账户/报价 provider 与 IBKR PAPER 仍未接通。

## D-097 Python long-only PAPER SELL 必须由 durable host authorization 决定

B149 移除事件桥对 SELL 的无条件拒绝：只允许 schema 中的 BUY/SELL 提案进入同一 host outbox risk handoff；只有 handoff 产生与 proposal 的 side/quantity/version/event/instrument 均一致的 intent，桥才创建对应 native order。越权卖超由 host strategy-position facts 拒绝，拒绝后不得创建 reservation、dispatch 或 Backtrader order；不允许 Python worker 自证持仓。当前集成样本用 fake facts/transport，部署级权威持仓归属与真实 PAPER 仍未验收。

## D-096 trusted NL2 PAPER 路径必须保留来源类型并共用 host 执行闸门

B148 续审确认 `ManagedPythonPaperSession` 原先是 Python upload 专用；自然语言生成的 allowlisted `StrategySpec` 不应被改标为 Python upload 或送入任意 Python worker 来复用它。B150 增加 NL2 SMA/RSI trusted evaluator 与 host target_fraction sizing，并共用 durable event/outbox、风险授权、ledger、PortBroker 与 Broker 回报路径；B151 已验证 SMA BUY 与 RSI BUY/SELL native order/fake callback，B152 将该链路封装为仅接收显式 trusted collaborators 的 session builder。生产 manager composition、grant/facts provider 仍缺，真实 PAPER 前仍须独立账户配置与授权。

## D-095 synthetic BackBroker 报告必须与独立金额 oracle 分开验收

B147 的 12-bar 可手算 fixture 覆盖两次 BUY、一笔 SELL 平仓和期末未平仓；独立核对 SMA(2/3) 交叉时点、整股数量、买卖方向 OHLC 滑点封顶、各侧 commission、final cash、closed trade gross/net P&L、末值与最大回撤。主报告、独立 BackBroker 子进程 replay 与手算 oracle 三者逐项核对；受篡改的费用拒绝。synthetic runner 新增可选 `slippage_fraction`，默认 0、不改变既有样本；该样本只验证合成 SMA 与执行/会计，不能替代真实行情证据或完整指标口径验收。

## D-094 本地 sender 之后的进程崩溃仍保持未知并禁止自动重发

B146 的离线故障替身先将发送意图写入并 `fsync` 外部接受账本，再于本地调用返回/ACK 前杀死策略进程。重启后持久 `SEND_STARTED` 不可重试，dispatch 返回 `RECONCILE/send_outcome_unresolved`，sender 未被再次进入。`SEND_STARTED` 只能表示本地已跨越 no-retry 边界，不能推断远端未接受或已经接受；在真实对账前保持未知。B146 假 Broker 不证明 IBKR/Gateway 行为，T42 继续 PARTIAL。

## D-093 SEND_STARTED 是跨进程崩溃后的不重发边界，不是 Broker exactly-once 证明

B145 用真实 SQLite 事务和子进程 SIGKILL 验证：`SEND_STARTED` 已提交、注入 sender 尚未进入时，进程重启后 intent 保留未决状态；再次 dispatch 只返回 `RECONCILE/send_outcome_unresolved`，不加载风险事实也不进入 sender。因此该状态必须保守解释为发送结果未知，并由受控 reconciliation 收敛；不能自动再发，也不能宣称交易所/Broker 只接受一次。真实 Gateway、网络部分写入和 ACK 丢失仍未验收，T42 继续 PARTIAL。

## D-092 T37 的策略超时必须验证容器内后代进程清理

B144 将容器级 deadline 故障样本升级为攻击性进程树：上传代码 fork 一个忽略 SIGTERM 的后代，并在 handler 安装后写入 tmpfs readiness marker；测试只在 marker 可见后杀死宿主 supervisor，再等待 daemon 执行内部期限与 `--rm` 清理。这样证明当前 Colima/Linux Docker 配置没有因 parent/host 先退出而留下该 worker container。该样本不等同于容器逃逸、daemon/host/runtime 故障或跨平台审计；T37 仍 PARTIAL，不能开放 Python PAPER。



## D-091 failed-session recovery 先注册 replacement feeds 再恢复 native orders

B143 的联合回归证明失败 session 恢复只支持特定 exact-linked BUY，并要求重建 Cerebro 时先以冻结 instrument 名称注册 fresh feeds，再将账户快照重建出的 native order/ref 绑定到对应 feed；随后再恢复 durable callback delivery。若先 restore order，Backtrader feed 尚未由 `Cerebro.adddata()` 赋予名称，订单与 feed identity 校验会失败并保持 fail-closed。B143 在真实 Cerebro/SQLite + fake IBKR 下通过，不声称覆盖 Docker timeout 同时活动单、真实 Gateway、进程/主机 crash 或 SELL 状态；T41/T43 仍 PARTIAL。

## D-090 failed-session recovery 独立授权且完成后仍保持 PAUSED

B142 将恢复策略线程失败单独定义为 `recover` 管理动作，不能复用 activate grant，也不能由 status/start 隐式触发。适用范围仅为 Cerebro/策略线程已经退出且旧 callback worker 健康的同进程恢复；用 reqIds 新候选、fresh reconciliation、callback quiescence 和 durable callback inbox 重放构造替换 Cerebro。恢复后的 service 与 journal 仍为 PAUSED，保留同一个未提交 released event 的 ID/seq/payload；只有单独的 activate 授权才重开 run 并重试。恢复失败需尽力由 RECOVERING 回到 PAUSED，owner lease 保留，order-ID allocator gate 关闭。B143 已在同一 Cerebro/SQLite + fake runtime 流程联合验收 exact-linked partial BUY、未 ACK cumulative callback 的幂等重放和原 released event 的独立激活重试；失败通过 PAUSED bridge 边界注入。callback worker fatal/SDK/Gateway/host crash、Docker timeout 同时活动单及生产 PAPER 仍未验收，不升级 T41/T43。

## D-089 只能在 callback worker quiescent 时转移 delivery claim

B141 允许 `IbkrCallbackPumpWorker` 在存活、健康、已暂停并持有 `process_one` 边界锁时换接 callback sink。pump 只清除能够对应 `delivered=0` durable callback inbox 的进程内 claim；inbox 是重放权威，新 sink 对已恢复 native order 重新投递并 ACK。预期 pending IDs 与 SQLite 不一致、claim 找不到 durable replay source 或 worker 未暂停时拒绝切换。该原语不证明账户快照新鲜、旧 `PortBroker` notification deque 已迁移，也不开放 session recovery API；调用方仍需建立全程 callback barrier 和再次对账。B141 callback/journal/managed-session 合并组 111 passed。T41/T43 继续 PARTIAL。

## D-088 PAUSED released event 的重试必须保留原始 durable identity

B140 增加 `SQLiteEventJournal` 内部 recovery primitive。可信编排器先检查同一 PAUSED run 的 cursor event 已 released、未 committed，event ID/seq/payload hash 与 caller 期望一致，checkpoint 与已提交前缀吻合，且没有 result/outbox 或后续 staged event；事务中只恢复 run gate，不更换事件或推进 cursor。该 primitive 自身不授权策略或 broker 操作，不暴露管理 API，也不单独构成 recovery；任何调用前仍须由 manager 持有 scope lease 完成 callback barrier、broker reconciliation/native-state 恢复。成功后仍保持 service PAUSED，dispatch 必须经独立 activate grant。B140 23 个 journal 离线测试通过；T41/T43 继续 PARTIAL。

## D-084 managed checkpoint reopen 的证据边界

B134 使用 fake stateful sandbox 与 seeded committed prefix，证明非空 Python checkpoint 经 managed session/coordinator 交接后下一事件状态、proposal、cursor 与不中断 reference 一致。B135 又在一条 managed-session reopen 路径中联合验证 exact-linked partial BUY/native ref 恢复、回报补齐旧单、随后按 durable checkpoint 处理下一事件。该联合场景使用 fake IBKR/runtime/sandbox；不证明真实 Gateway 或 BUY 之外订单恢复。B136 证明真实 Docker worker timeout 经 coordinator 包装后仍保留 immutable released event、无 result/outbox/cursor advance，且容器被删除。B137 将真实容器超时接到 managed session，证明 service/journal PAUSED；fake IBKR callback worker 在 PAUSED 后仍处理 nextValidId、部分成交、归因撤单和取消回报。不能把这些隔离证据记为真实纸面交易或完整 crash recovery，T41/T42/T43 仍 PARTIAL。

## D-085 worker timeout 保留 durable released event

B136 记录当容器内策略超时后，已释放事件是重试的唯一权威输入：新请求的不同 event_id/bar 不得覆盖它；只有成功结果提交后 cursor 才前进。测试同时要求确认超时容器清理。B137 补充超时到达 managed session 后暂停 service/journal，并在 PAUSED 下保持 callback worker 可用；fake runtime 中的 durable fill、exact-intent 授权撤单和取消回报能继续推进。该离线样本不证明真实 Gateway 可用性。

## D-086 failed managed session 不允许原地重新激活

B138 固定 fail-stop 语义：一旦策略线程保存异常，原 session 不得通过 `activate()` 重新开放 PAPER dispatch；即便 SDK worker 仍健康也必须保持 PAUSED。后续恢复需另建安全 replacement 路径，先持有 owner lease 完成新鲜账户对账、callback/native order 与 strategy checkpoint 恢复，再由独立 attributable activate grant 显式开闸。当前 replacement 未实现，因此保留 lease 并拒绝原地 rearm。

## D-087 未完成的 safe stop 保留 PAUSED 服务的管理撤单通道

B139 固定一个失败 session 下的收尾语义：若 `ManagedPythonPaperSession.stop()` 因 active order 拒绝 quiesce、尚未让 durable service 进入 STOPPING，则 manager 可以报告 STOPPING/lease-held，同时仍允许具有独立 cancel grant 的主体按本地 intent ID 撤 owned order。IBKR runtime 必须再次核验 service 为 RUNNING 或 PAUSED、lease 与 durable binding；进程/服务健康或状态不符合就拒绝。此决定不允许在 durable service 已 STOPPING/STOPPED 后发送新的 cancel；完整 replacement/recovery 仍需另行实现。

## P0 已冻结

| ID | 决定 | 依据 / 实现位置 |
|---|---|---|
| D-001 | V1 StrategySpec 只接受 schema `1.0`、一个标的、完整美股常规日线；未知字段拒绝 | `agent/src/execution/contracts/models.py` |
| D-002 | V1 只接受 Market/DAY、常规交易时段、完整 bar 可用后决策、下一常规时段派单 | 同上；`us_equity_rth` 的离线 session 表由固定版本 XNYS adapter 生成；真实 provider availability 与生产日历授权仍未通过 |
| D-003 | `target_fraction` 与 `explicit_quantity` 是互斥数量模式；目标比例必须有 allocation_fraction，显式股数不接受该字段 | 同上；目标数量纯函数仍待 P1 |
| D-004 | 规范化 hash 使用 UTF-8 JSON、稳定键排序、无空格分隔；Decimal 序列化为精确十进制字符串；SHA-256 | `canonical_json` / `canonical_sha256` |
| D-005 | 领域事件与派单时间必须带时区，并在契约层归一到 UTC | `EventEnvelope`、`OrderIntent` |
| D-006 | 环境只允许 `paper`、`live` 两个明示值；默认 spec 本身不包含账户授权 | `Environment`；尚无部署/授权实现 |
| D-008 | Backtrader 固定为 `1.9.78.123`，通过可选 `backtrader-runtime` extra 安装并进入 `uv.lock` | Vibe `pyproject.toml` / `uv.lock` |
| D-009 | 官方 IB API 不直接从同名 PyPI 包解析；使用官方 TWS API 1050.02 `source/pythonclient`，以 `setup.py + ibapi/**` 文件树版本/数量/hash 锁定，并在独立 `ibkr-runtime/` 环境构建安装 | `docs/backtrader-system/ibapi-source.lock.json`；`scripts/install_ibapi_from_official_source.py`；树 hash `ff7a0e7aaf077042190426f08aaac11cbd9e4b548507e450f84ac961ba83ed7f`。该 hash 不代表上游 zip archive hash；下载与授权见 [IBKR Python API 安装说明](https://www.interactivebrokers.eu/campus/trading-lessons/accessing-the-tws-python-api-source-code/) |
| D-010 | IB 回报消费只接受 trusted durable order mapping；SDK callback 由必需的 durable sink 先写共享 SQLite spool，再入内存 queue；pump 消费 spool 后写 callback inbox、更新 SQLite ledger，最后以累计状态通知 native BT broker。`Filled` 状态只有在 execDetails 已完整填满账本数量时才作为重复终态接受，否则 fail-stop 并等待 reconciliation | `agent/src/execution/ibkr_callback_pump.py`、`ibkr-runtime/ibkr_runtime/callbacks.py`；B10/B11。spool 使用 WAL 与 `synchronous=FULL`；P3a 组合模式将 inbox ACK 延至 Cerebro 线程成功应用 native order 状态之后，保留未 ACK 项供进程内重放。完整进程恢复和生产服务 wiring 仍未实现。
| D-011 | 上传 Python 的策略事件由可信 `PythonEventCoordinator` 根据 durable run version/state 逐条释放；只送一个完成 bar 和 JSON checkpoint，worker 不接收 DataFrame、文件路径、账户 authority 或下一事件 cursor。worker 输出经严格模型校验后与 checkpoint、outbox 在 journal 中原子提交 | `agent/src/execution/event_worker.py`、`event_journal.py`、`strategy_worker_image/`；B13/B14/B15。容器目前一次处理一个事件；该接口未接入 Cerebro、execution_service 或 API，不能称上传策略完整回测/部署完成 |
| D-012 | Python upload 离线回测由可信 Cerebro bridge 驱动隔离 `on_event(event,state)` worker；每次只释放一个与 DataFrame index 一一对齐的 final `Bar`，worker 只能返回 JSON state 与受限 Market/DAY proposal。Host 不缩放显式整数股数，proposal 转成 native `bt.Order` 后由 BackBroker 成交，报告来源为 host broker facts。因当前每根 bar 冷启动容器，单 run 限 1,000 events；仍不等同于 durable event journal、在线 Broker 或 paper 部署；第二套报告 replay 见 D-014 | `agent/src/bt_runtime/strategies/python_upload.py`；B16/B17/B24 |
| D-013 | 在线 proposal handoff 由 host 从 journal identity 与完整性校验过的 staged event/result 重建 intent；grant、RiskFacts、run identity 三者须绑定一致；LIVE proposal 明确拒绝。risk pass 后先冻结完整 intent/decision，再幂等写入独立 order ledger。该切片只生成 CREATED intent，不预留资金、不发送；reserve/send 前需刷新事实并再次风控。它不构成 P3a 通过 | `agent/src/execution/outbox_dispatch.py`、`event_journal.py`；B25 |
| D-014 | Python upload Backtrader 报告在成功返回前必须由同进程中的第二个 Cerebro/BackBroker 按 host 捕获的有序 decision log 复算；输入 bars、资金/费用、manifest、version、instrument 与 engine 声明绑定到实际复算，订单生命周期、成交/费用、逐 bar cash/equity、closed trades、期末持仓或 metrics 任一不符即 fail-closed。该复算不能证明进程隔离或发现共同算法错误，也不是券商 reconciliation 或 paper 准入 | `agent/src/bt_runtime/report_replay.py`、`python_upload.py`；B26/B27 |
| D-015 | Python upload 的离线回测使用独立 `SQLiteBacktestEventJournal`，不复用含账户授权与 dispatch outbox 的 paper/live journal。worker 结果和 JSON checkpoint 每 event 原子持久化；恢复只在版本/spec/数据/资金/费用配置完全匹配时从第一根 bar 全量重建真实 Cerebro/BackBroker，已提交结果复用、未提交事件按相同 Bar 重试。该流程不恢复在线 Broker、外部订单或 paper reservation | `agent/src/bt_runtime/backtest_event_journal.py`、`python_upload_service.py`、`runs_routes.py`；B28 |
| D-016 | BUY/SELL PAPER intents 均可在离线 `PaperOrderDispatchService` 中经两次可信 facts 检查；SELL 使用独立 share reservation，不能复用现金 hold。最终 `BEGIN IMMEDIATE` 复核本地与 provider 提供的外部卖单预留、freshness、scope、intent 身份和风险，并与唯一 `SEND_STARTED` 原子提交；成交递减余量，REJECT/确认 CANCELLED 释放未成交部分，UNKNOWN/取消未确认保留，终态迟到成交冻结 scope。B101 覆盖单库并发、外部挂单合并、迁移冻结和订单事件状态。该离线代码不提供生产持仓归属、可信 provider、跨 ledger/host 容量保证或 app-level IBKR service wiring；故不开放真实 PAPER。 | `agent/src/execution/dispatch_service.py`、`order_ledger.py`、`backtrader_dispatch.py`；B29/B34–B36/B101 |
| D-017 | 受信任 SMA provider 回测将请求日期解释为含首尾日，并要求整个请求区间内实际交易日与固定 `exchange-calendars==4.13.2` 生成的 XNYS 规则日程完全一致；缺少开头、中间或结尾 session 均拒绝。通过 `import_completed_daily_csv` 核对 session 覆盖、开收盘、bar 完成时刻和 schedule hash。日线 provider 时间戳须为午夜 session-date 标签；模型将 schedule close 作为 available-at 代理，provider 实际发布时间仍未知。该库是可复现的离线日历实现，不声明为交易所官方授权源；provider 的历史可得性、复权和公司行动仍独立标为未验证 | `agent/src/bt_runtime/calendars.py`、`market_data.py`、`agent/backtest/runner.py`；B30/B31 |
| D-018 | 自然语言模板入口在执行前提供只读规则预览；完整回测输入、规范 spec、预算/费用默认值、Backtrader 版本、实际模板源码 SHA-256 和 compiler version 生成 review hash，服务端签发十分钟、一次性的随机 token 并写入本地 SQLite 授权表。runner 校验该源码身份并用同一冻结字节创建不可变版本。实际创建必须消费有效 token 并复算 hash，字段/模板变化、过期或重放均拒绝。SMA 与 RSI 都先变成严格 template intent 再生成 StrategySpec；RSI intent 额外要求逐字原文证据匹配每个数值参数。token 绑定并保存确认过的 spec，创建时不重新生成 intent。任意条件遗漏、多轮澄清与模型遗漏矩阵仍未通过验收 | `agent/src/api/runs_routes.py`、`agent/src/execution/strategy_design.py`、`agent/src/bt_runtime/strategies/sma.py`、`agent/backtest/runner.py`、`frontend/src/pages/Execution.tsx`；B32/F7/B79/B95/B96 |
| D-046 | 管理撤单与策略 native-order cancel、pause、stop 分离；管理控制只以本地 intent ID 定位 owned PAPER 订单，由可信 runtime 从共享 SQLite 的 durable binding 解析券商 order ID。需可归因 principal 的独立 `cancel` grant；事务先持久 `CANCEL_PENDING`/唯一 cancel attempt，再发送 IPC。传输结果不确定时冻结 `UNKNOWN`，只允许 reconciliation/callback 收敛，不盲重发；只有券商取消回报确认才记 `CANCELLED`。stop 不隐式撤单；当前仅 manager/runtime 原语实现，尚无 authenticated API 或 app wiring | `agent/src/execution/paper_execution_manager.py`、`ibkr_managed_runtime.py`、`ibkr_callback_pump.py`、`order_ledger.py`；B52/B86 |

## 需实验后冻结

| ID | 事项 | 当前状态 | 影响 |
|---|---|---|---|
| D-101 | 上传 Python 的 OS 级网络/文件/资源隔离 | macOS 旧 runner FAIL；真实 Docker probe 已证无网、只读 rootfs、非 root、无 host mount。B129–B131 增加 cgroup memory/CPU/PID 运行时读数、PID 超额拒绝、32 MiB 内存压力清理和半核两秒 busy-loop quota 样本。仍缺跨 host/runtime 的完整资源压力/监督崩溃矩阵、可信风险 Broker 与生产 composition | 本轮资源证据不开放 PAPER；Python 策略仍只允许隔离回测，直至 T37/T40/P3a 全门槛通过 |
| D-102 | Backtrader BrokerBase 跨进程代理与空 bar 回报泵 | B8 产品 `PortBroker` 在真实 Cerebro/no-bar 中映射 native order、fill/cancel 与 fail-stop；仍是进程内 fake trusted port，跨进程/生产接口未验证 | 决定可信服务与策略 worker 的事件接口 |
| D-103 | event_result + checkpoint + 意图原子提交及 crash 恢复 | 本地 SQLite 崩溃/重交 probe PASS；多 writer/发送未知未验证 | 决定 ledger/schema 和策略事件协议 |
| D-104 | 用户 Python 状态与指标重启后可确定性重建 | 20 个事件的合成策略/指标 probe PASS；账户状态和跨进程 persistence 未验证 | 恢复验收门槛；不一致须暂停 |
| D-105 | 逐事件 Feed 防未来数据 + 第二个真实 BackBroker 报告复核 | P2/Broker replay 最小探针 PASS；B26/B27 已把 Python-upload report replay 接入返回门控；同进程 Feed 暴露未来数据 FAIL，跨重启恢复仍未验收 | 决定 Python 回测报告可部署状态 |
| D-106 | 美股交易日历与半日市/夏令时来源和版本 | 离线回测已固定 XNYS `exchange-calendars==4.13.2` 并验 DST/半日市/节假日/hash；官方生产日历来源、供应商交易时点和 next-session expiry 仍未冻结 | 决定下一有效交易时段和过期窗口 |
| D-107 | 目标比例数量、费用预留、价格缓冲、跳空门槛 | Decimal 纯函数已实现；SMA 合成手算样例通过，费用缓冲用于离线 sizing；真实费用/滑点模型和跳空门槛未验收 | 影响具体订单数量与风险，不等于用户额度授权 |

## P0 发现带来的当前门槛

- 既有 macOS runner 不具备已验证的 OS 网络/文件隔离；系统自带 Seatbelt 探针虽能拦截，但标记为 deprecated。新 Docker launcher 的隔离切片已通过 B4，但还没有接入执行服务/可信风险 Broker 和逐事件 IPC。上传 Python 仍不能接 paper/live Broker。
- Backtrader 的 `preload=False, runonce=False` 会逐步扩展 line buffer，但这并不能防同进程反射访问 Feed 参数或完整 `PandasData.p.dataname`。未来行情必须留在可信服务侧，通过按已授权事件发放的 IPC 给策略进程；现有探针 Feed 明确不符合这个条件。
- 真实 Cerebro 的空 bar broker event pump 可行；如策略必须及时收到订单 `notify_order`，运行参数应显式打开 `quicknotify=True` 并用目标 IPC 验证。

## 新增离线接口

- `agent/src/bt_runtime/market_data.py` 接受调用方提供的完整 session schedule 和 CSV，不负责下载行情；bar 的 Backtrader 时间点采用其完成后的 `available_at`。manifest 记录 provider/calendar/复权和公司行动声明，并标注来源声明未认证。
- `agent/src/bt_runtime/strategies/sma.py::run_verified_backtest` 通过受信任 SMA 模板执行真实 Backtrader/BackBroker，并绑定 manifest hash。此路径不认证供应商授权、不证明数据来自真实市场，也不代表上传 Python 沙箱或交易 Broker。
- `agent/src/execution/versions.py` 的版本记录同时定位模板与上传源码的 hash-addressed blob，加载时重新校验源码 hash。
- `agent/src/execution/ibkr_callback_pump.py` 以持久 `BrokerOrderIndex` 绑定券商 order ID、可信 intent 与 Backtrader ref，并将已规范化回报持久入 inbox 后应用到账本和 `PortBroker`。账户级权限/资金校验、费用、完整恢复与真实 Gateway 不在此切片范围内。

## 必须由用户提供或明确授权的账户参数

paper 账户、已确认的 paper Gateway 登录环境与连接 profile、预期账户 ID、账户行情权限、具体标的合约、策略预算、订单/总敞口/日亏损限制、授权有效期及清理动作目前均未确认。任何实现不得用示例数字、live 4001 或 Read-Only 状态代替这些配置。


## P3a 已冻结的离线实现选择

| ID | 决定 | 依据 / 实现位置 |
|---|---|---|
| D-019 | V1 IBKR transport 只由可信宿主提供经审核且不超过 30 天的 SMART/USD/STK→conId 合约映射；官方 SDK factory 据冻结 PAPER intent 构造精确的整股 Market/DAY `Contract`/`Order`，transport 再核对 native Backtrader command 与 intent 完全一致。IBKR `nextValidId` 必须为正整数且不低于持久 cursor；每个运行 epoch 先持久记录 fresh callback 为候选值，host 完成开放订单/本地绑定 reconciliation 后显式确认，才可分配；分配前事务性递增 SQLite cursor；每个本地 DB 路径只允许一个 owner。此锁不提供跨机器/不同路径的账户全局唯一性，映射依赖人工可信来源；没有账户读取、连接或下单授权 | `ibkr-runtime/ibkr_runtime/order_factory.py`、`order_ids.py`、`paper_transport.py`；`agent/src/execution/ibkr_callback_pump.py` next-valid-ID hook；B37/B53。应用级执行服务尚未将这些注入件正式接线。 |
| D-020 | 每个 PAPER/IBKR/账户的本机 execution runtime 必须持有 `ExecutionOwnerLease` 才能经 `BacktraderDispatchPort` 派单；租约 scope 由 broker/account/environment 决定，owner ID/PID 写入稳定锁文件，POSIX `flock` 在同一共享本地目录内保证独占。相同 host/lock directory 下跨 OS process 竞争已测；目录和 owner metadata 文件权限分别限为 0700/0600；dispatch 会用冻结 intent 核对租约 scope。不同机器、不同锁目录或绕过该执行入口不受此锁保护，也不等同于 broker/client owner 配置 | `agent/src/execution/owner_lease.py`、`backtrader_dispatch.py`；B39–B42。app execution service 启停与 lock directory 部署配置尚未接线。 |
| D-021 | `SQLiteOrderLedger` 按 broker/account/environment 持久保存 `PAUSED/RUNNING/STOPPING/STOPPED/RECOVERING/ERROR` 控制状态；缺失 scope 默认 `PAUSED`。dispatch 在取风险事实前要求 RUNNING，并在最终 `BEGIN IMMEDIATE` 发送事务中再次检查同一 scope，防止暂停与 send commit 竞态；阻断时释放尚未发送的 reservation，不创建 attempt。控制表是可信宿主本地闸门，不是授权/管理 API，也不撤销已经提交的 `SEND_STARTED` 或取消在途订单 | `agent/src/execution/order_ledger.py`、`dispatch_service.py`；B43。app 生命周期、认证控制入口、在途撤单/恢复及 paper 验收仍未实现。 |
| D-022 | Python 策略 worker 的一事件 request/response 使用版本 1 的 JSON-over-stdio 帧，host 为每次调用生成随机 request_id；响应必须匹配协议版本、request_id 和当前 event_id，并严格拒绝账户、actor、grant、deployment 或管理命令字段。worker 没有服务端 socket、账本/凭据挂载或管理通道；该随机 ID 是关联/重放防护，不是授权凭证。真实 daemon 运行经宿主进程强杀后验证容器内 deadline 与自动删除 | `agent/src/execution/event_worker.py`、`strategy_worker_image/worker.py`、`python_sandbox.py`；B45/B46。持久双向策略服务、管理 API 鉴权、可信 execution service 和完整 T40/T43 仍未完成。 |
| D-023 | Python paper event bridge 只接受与 durable event 完全相同的当前 completed Bar；启动/每事件要求 service RUNNING 且持有匹配 owner lease、run 为 PAPER 且账户一致、该 scope 无未结 ledger 工作、run 无旧 dispatchable outbox。每个事件至多一个 BUY proposal；proposal 先经 handoff 固化为 intent，host 再核验 event seq/bar/instrument/order fields，并用 one-shot resolver 建立一次 native BT order。失败时暂停 run 与 service。该组合不恢复 Cerebro/native orders、IBKR callbacks、持仓或账户现金，故重启的真实 recovery check 必须覆盖这些事实后才可激活 | `agent/src/execution/paper_event_bridge.py`、`backtrader_dispatch.py`、`event_worker.py`、`event_journal.py`、`order_ledger.py`；B50。离线 fake transport/Cerebro 组合不是 app wiring 或 paper 验收。 |
| D-024 | 撤单只允许通过持久 broker-order binding 解析出的已拥有 PAPER 活动订单执行；事务先写唯一 cancel attempt 并设 `CANCEL_PENDING`，再调用 transport 的精确 broker order ID。UNKNOWN 不重试，必须靠后续 callback/recovery 解决；`CANCELLED`/`CANCEL_REJECTED` callback 更新撤单事实。此规则不提供账户全局撤单、外部订单管理或 stop/flatten 权限，也没有真实 Gateway 确认 | `agent/src/execution/backtrader_dispatch.py`、`order_ledger.py`、`ibkr-runtime/ibkr_runtime/cancel_transport.py`；B52。 |
| D-025 | PAPER lifecycle 由单一可信 manager 持有，V1 只接 IBKR/PAPER 且最多一个本机 runtime slot；runtime factory 必须是宿主配置，不能从请求体推导账户或连接参数。Ledger、callback index/spool 与 order-id allocator 必须指向同一规范 SQLite 文件。恢复必须回到 PAUSED，授权动作要求可归因 Principal + action grant；recovery/quiescence 未成功时保留 slot 和 lease。该 manager seam 目前未注册到 FastAPI，且不实现 SDK 进程/账户 reconciliation | `agent/src/execution/paper_execution_manager.py`、`execution_service.py`；B55。 |
| D-026 | Official SDK `EClient` 生命周期只能由显式 host 命令开启；client owner 构造不连接，SDK run loop 独占专用线程；disconnect 后必须在限时内 join，超时维持 STOPPING，disconnect 异常维持 FAILED，均不得视为 owner 可释放。Callback 先同步 durable spool 再进入有界内存队列；队列溢出仍需 host pause/reconcile。此类当前仅在独立 ibkr-runtime 环境测试，未由主 app subprocess 接线 | `ibkr-runtime/ibkr_runtime/lifecycle.py`、`callbacks.py`；B57。 |
| D-027 | Official SDK 与主应用运行时由锁定的独立 Python 进程隔离；主应用父进程仅通过私有 stdin/stdout V1 NDJSON 发送有限的 status/start/stop/shutdown/place_order/cancel_order/confirm_reconciled_stop 命令。帧有 16 KiB 上限、精确 schema、有限 JSON、串行请求与 request_id 响应关联；未知字段/命令一律拒绝。订单 IPC 只接受受限 PAPER SMART/USD/STK 整股 BUY/SELL MKT/DAY 请求，撤单只接受精确正 broker ID，宿主再次校验合约映射与冻结标的。子进程仅在显式 start 时连接，构造和启动进程本身保持离线。callback 先持久写共享 SQLite spool；`IbkrCallbackPumpWorker` 在可信宿主独立线程消费 spool，通过 ledger/inbox 完成持久交接后 ACK，不修改 Backtrader 对象；`managedAccounts` 必须包含配置账户；pump fatal 可请求 durable pause。connect 在 SDK 专用线程执行；API handshake、RUNNING 状态与派单 readiness 分离；run loop 异常锁存 FAILED，只有 host reconciliation 后才可确认停止。协议错帧、命令超时或管道中断都视为 lifecycle 不确定。当前 worker/process/pump 尚未由同一个 manager runtime 创建或驱动，亦未接入 FastAPI；账户余额、持仓和 open-order 恢复仍缺 | `agent/src/execution/ibkr_runtime_process.py`、`ibkr_callback_pump.py`、`ibkr-runtime/ibkr_runtime/ipc.py`、`worker.py`、`callbacks.py`、`commands.py`；B58–B60。 |
| D-028 | SDK 子进程健康只能从可信 host status 监控；非 RUNNING、socket 断开、callback overflow/未知值、SDK handshake 缺失、run_error 或 status IPC 故障必须调用 durable pause hook。SDK API handshake 与 broker/account/order reconciliation 分离；`ready_for_dispatch=false` 当前为预期，不能以 RUNNING 或 SDK readiness 开启发送。monitor 必须与 callback pump 同时存活，使 pause 后仍能处理和持久化券商回报。当前 monitor 只有可测试 primitive，尚未由 app runtime 创建 | `agent/src/execution/ibkr_runtime_process.py::IbkrRuntimeHealthMonitor`；B61。 |
| D-029 | Lifecycle recovery 分为 `begin_recovery()` 和 `complete_recovery()`：前者必须先持本机账户 owner lease 并 durable 写 RECOVERING，使 SDK child/connect/callback replay 可在发送闸门关闭时发生；后者仅在受信任 broker/account/order reconciliation 明确返回 true 后转 PAUSED。老 `start()` 为兼容入口，组合两步。app runtime 未接线前不能把 PAUSED 或 RUNNING 作为账户恢复证据 | `agent/src/execution/execution_service.py`；B62。 |
| D-030 | V1 PAPER 快照必须由 SDK 的 account summary、positions、all-open-orders 三组请求产生，并以 snapshot ID 和三个独立 end marker 关联；Host 先持久化采集意图，再接受幂等、严格 schema 的 callback facts。只有账户 tag/currency、完整 end markers、broker open-order 与本地活动 binding/intents 完全相等、positions 与持久 fills 一致，且 fresh `nextValidId` 高于本地及 broker order-ID 高水位时，才先记录 reconciliation proof，再确认同一 candidate。任何未知订单、仓位或历史成交差异均阻止恢复；不得用余额/当前仓位猜测漏掉的 executions。该 primitive 仍需 app runtime 编排；它不连接 Gateway，也不等于授权或激活 | `ibkr-runtime/ibkr_runtime/callbacks.py`、`lifecycle.py`、`ibkr_reconciliation.py`、`ibkr_order_ids.py`；B64。 |
| D-031 | Concrete `IbkrManagedPaperRuntime` 构造保持离线；仅 manager 显式调用 `start_paused()` 后，按 acquire lease→RECOVERING→SDK child→回调泵→账户握手→snapshot→账本对账→PAUSED 顺序操作。account、Gateway host/port/client ID、超时、风险/静默检查均由可信 factory 注入，同库路径构造期核对。callback/health failure durable pause；安全 stop 的 quiescence callback 必须先证明当前账户订单状态，再关闭 SDK/drain spool/inbox，只有全完成才让 service 释放 lease。该类尚未由 app lifespan/manager factory 接入，亦不验证 Gateway 实际是 PAPER profile | `agent/src/execution/ibkr_managed_runtime.py`、`execution_service.py`；B65。 |
| D-032 | live 日线策略输入由 trusted host 经 `PaperCompletedBarFeed` 推送不可变 completed `Bar`；feed 只做标的/币种/时区/可用时间/严格单调与有界队列检查。provider 授权、真实合约、XNYS regular session、缺口/修订/公司行动由上游 host 必须验证，策略进程不能推动未来/部分 bar。Feed 空轮询应返回 `None` 而非结束，配合 Cerebro `quicknotify=True` 让 broker callback loop 在没有新行情时继续消费回报；队列溢出锁存并由 host pause。它不 fetch 数据、不初始化账户现金/持仓，也尚未接到 manager/策略 runtime | `agent/src/bt_runtime/feeds/paper_queue.py`、`agent/src/bt_runtime/brokers/proxy.py`；B66。 |
| D-033 | B67 的 `PythonPaperSession` 是严格离线 composition：Cerebro 中只运行可信 shell；冻结上传源码仍经逐事件 `PythonEventCoordinator`/隔离 worker 执行，输入只含 host 当前 completed Bar 和 checkpoint。订单经 native PortBroker 与既有 event/dispatch/ledger seam；transport 必须显式标记 `offline=True`，构造器不接 SDK process/API。此 primitive 不能作为生产 paper runtime 或账户恢复证据 | `agent/src/execution/python_paper_session.py`、`paper_event_bridge.py`；B67。 |
| D-034 | Backtrader 账户 hydration 只接受可信 reconciliation store 输出的完整 PAPER snapshot，cash 只取唯一 USD `TotalCashValue`；`NetLiquidation` 和 `AvailableFunds` 不作 cash。每个持仓须整股、USD、scope 匹配，并由唯一 conId→Data 映射覆盖；全部验证后才原子更新 broker，且只允许 start 前一次执行。首 bar 到来前持仓 market value 不可声称可得，broker `getvalue()` 必须 fail-closed | `agent/src/bt_runtime/brokers/proxy.py::PortBroker.hydrate_reconciled_account_snapshot`；B67。 |
| D-035 | `MarketDataProvider`、`TradingCalendar`、`MarketRules`、`CostModel` 均须逐项声明 history/realtime/backtest/paper/live capability，默认全部 false；声明不等于实现，基类未覆盖的方法仍抛出 `UnsupportedMarketCapability`。CalendarSchedule 的 hash 使用与 completed-bar importer 相同的 UTC session payload canonicalization，并校验实际内容。无版本化 quote schema 时不提供 quote subscription。contracts 仅定义边界，不认证供应商、日历或费率 | `agent/src/execution/market_data_contracts.py`、`agent/src/bt_runtime/market_data.py`；B68。 |
| D-036 | `PythonPaperSession` 可在非 daemon Cerebro thread 长驻，并允许 trusted host 运行期间 push completed bars；输入结束、join、运行异常与队列错误显式暴露，异常尽力 pause。其 transport 始终要求 `offline=True`，故它目前只用于持续 feed/fake transport 验收，不可被解释为 IBKR app runtime 或生产策略 deployment | `agent/src/execution/python_paper_session.py`；B68。 |
| D-037 | `IbkrPaperRuntimeFactory` 只接受完整显式的 IBKR/PAPER scope、SDK Python/cwd、Gateway endpoint、client ID、account 与统一 SQLite/owner-lock 路径；recovery、activation、quiescence、callback、intent 和 contract handlers 不得默认成功或省略。构造只建立 host 组件/本地 SQLite，不 fork、不 acquire lease、不连接；只有 `IbkrManagedPaperRuntime.start_paused()` 才是连接边界，恢复必须落在 PAUSED。已知 LIVE Gateway 4001 被拒绝。此 factory 尚未接 app manager、可归因授权、事实 provider 或 Python/Cerebro/feed runtime，不能作为真实 PAPER readiness 证据 | `agent/src/execution/ibkr_paper_runtime_factory.py`；B69。 |
| D-038 | FastAPI 只暴露受认证只读 PAPER status；默认惰性 manager 的 `runtime_factory=None`、控制 grant deny-all，lifespan 不启动 runtime。status 仅输出 configured/phase/control gate/owner lease/稳定错误字段，不暴露账户 ref、Gateway endpoint、余额、持仓或委托；Execution 页面读此状态，读取失败显示 unknown。没有 start/activate/pause/stop route | `agent/src/api/state.py`、`paper_execution_routes.py`、`frontend/src/pages/Execution.tsx`；B70。 |
| D-039 | Deferred callback handoff 使用 durable `callback_inbox` 作为重启恢复真相；Pump 进程内按 source/event ID 暂存“已入有界 Broker queue、待 ACK” claim，ACK 后先提交 SQLite delivered，再释放 claim。新 Pump 不继承内存 claim，因此未 ACK inbox 会重放；队列 sink 失败时撤销本次 claim并暂停。构造 `IbkrCallbackPumpWorker` 即保留唯一 background-consumer role，Cerebro 侧检测此配置后不再从 `next()` 消费；无 worker 的离线测试继续用 Cerebro consumer。Broker 对完全相同的重复 event ID 再次 ACK，内容冲突仍停机。该 pump 重放不恢复 Backtrader native order/ref；未知 ref 回调仍 fail-stop，需 native order recovery slice | `agent/src/execution/ibkr_callback_pump.py`、`backtrader_dispatch.py`、`agent/src/bt_runtime/brokers/proxy.py`；B71。 |
| D-040 | Managed runtime 只向可信 Backtrader hydration consumer 暴露当前恢复轮次的 complete/reconciled snapshot：必须仍持有匹配 lease、状态为 PAUSED、SDK 健康、snapshot ID/account/PAPER scope 与持久 reconciliation proof 一致，host recovery proof 复验通过，且 snapshot 完成时间在配置 freshness window 内（默认 10 秒、允许 1–300 秒；拒绝未来超过 5 秒的时间戳）。`activate()` 一旦成功就使该 hydration handoff 失效；后续 pause 不能重用旧快照，必须重新 RECOVERING 并获取新快照。此 accessor 不做 conId/feed 映射，也不启动 Cerebro；TTL 不能防止外部交易刚好发生在快照与 hydration 间 | `agent/src/execution/ibkr_managed_runtime.py::latest_reconciled_snapshot`、`ibkr_paper_runtime_factory.py`；B72。 |
| D-041 | Managed Python PAPER session 单独作为可信 composition primitive，不能复用离线 `PythonPaperSession` 的任意 fake transport/synthetic cash。只接受当前 fresh reconciled PAPER snapshot、frozen Python version、严格 verified conId→feed 和 runtime 自有 SDK transports；构造时覆写 pump sink 为有限 callback staging router 并启用 deferred ACK。启动 runtime 后必须 PAUSED、有 lease、无活动/未知 ledger work、pending inbox/callback、router callback 或 dispatchable outbox，才创建并 hydration PortBroker/Cerebro。显式 manager activation 后才接受 host 完整 bar；停止先暂停并 drain，任何未恢复 native order/ref 或待确认回调均保留 lease。当前只由离线 fake-child integration 验证，尚未注入 app manager 或真实 provider | `agent/src/execution/managed_python_paper_session.py`、`agent/tests/execution/test_managed_python_paper_session.py`；B73。 |
| D-042 | Python upload 回测同样必须先审阅再执行。Preview 只做源码语法/入口、数据 importer、spec/manifest 和摘要校验，不建 run/version；10 分钟一次性 token 绑定整个请求与派生 hash、引擎/worker 版本及精确 digest-pinned worker image，最终 POST 重新校验后才消费 token 和创建 run。worker image 必须是 `@sha256:<digest>` 或不可变 `sha256:<image-id>`；确认后身份进入 run config/report，恢复也必须使用同一镜像。没有 worker image 时只返回不能运行的 preview，不发确认 token。此确认仅授权离线回测；输入 provider、日历、调整声明和镜像内容/来源仍不因此获得认证 | `agent/src/api/runs_routes.py`、`agent/src/execution/python_sandbox.py`、`agent/src/bt_runtime/strategies/python_upload_service.py`、`frontend/src/pages/Execution.tsx`、`agent/tests/test_backtrader_backtest_api.py`；B75/B111。 |
| D-043 | 原始输入文件哈希与解析后策略输入哈希分开。`file_sha256` 保持原始 CSV 字节语义；`quality_report.strategy_input_schema` 与 `strategy_input_sha256` 对规范化可用时刻和 Backtrader 消费的 OHLCV 浮点值进行绑定，运行器在启动 Cerebro 前重算核验。Python offline event coordinator 必须持有可信 importer/artifact service 构造的固定 Bar 序列，事件调用提供当前 Backtrader feed timestamp、不接受 Bar，并要求 timestamp、序号和 durable journal cursor 三者一致；允许 Backtrader float-date 反解带来的最多 50 微秒表示误差，其他错位先 pause，不执行 worker。此证明输入和 manifest 一致，不认证供应商、授权或数据本身真实性 | `agent/src/bt_runtime/market_data.py`、`strategies/sma.py`、`strategies/python_upload.py`、`agent/src/execution/event_worker.py`；B76。 |
| D-044 | 两种离线回测入口采用同一个可配置 BackBroker 百分比滑点模型：`set_slippage_perc(perc=fraction, slip_open=True, slip_limit=True, slip_match=True, slip_out=False)`，按下一 bar 市价开盘价计算并受 bar 高低范围撮合约束。默认 `0.0` 保持旧结果；Python upload 允许 0–0.25，SMA route 固定默认值纳入 preview/hash。报告必须声明模型和 fraction，Python 报告由第二个 Cerebro 独立重放相同参数。该参数是模拟假设，不是目标券商或实际 provider 成本认证 | `agent/src/bt_runtime/strategies/sma.py`、`python_upload.py`、`report_replay.py`、`runs_routes.py`、`Execution.tsx`；B77。 |
| D-045 | 运行中的 BacktestJournal 遇到 `seq != next_seq`（包括调用方带有合法未来 available_at 的跳号请求）必须在事务内持久 `PAUSED` 后拒绝；不能只 rollback 并保持 RUNNING，否则同一损坏 host cursor 之后可能继续。拒绝时不得插入事件或推进 next_seq；之后的正确 cursor 也不能自动恢复。普通 worker 暂时失败且事件已按当前 cursor durable-release 仍保留可精确重试语义 | `agent/src/bt_runtime/backtest_event_journal.py`、`test_backtest_event_journal.py`、`test_strategy_worker_docker_integration.py`；B78。 |
| D-047 | Execution SQLite 备份使用 online backup API 捕获 WAL 一致视图，完成后输出无 sidecar 的单文件数据库和最后写入 manifest；恢复只安装到不存在的新路径并核验哈希/schema/integrity/FK，任何现存 `execution_control` scope 都强制改为 `RECOVERING`，须重新取得 lease 并 reconciliation。SQLite-only migration 先做可验证备份，再用单个 `BEGIN IMMEDIATE` 事务；运行时必须停止，migration callback 不得自行 commit/close。该原语不提供远端调度、加密/保留方案或 broker state 恢复 | `agent/src/execution/sqlite_backup.py`、`agent/tests/execution/test_sqlite_backup.py`；B87。 |
| D-048 | Online `SQLiteEventJournal` 的策略 checkpoint 只有在严格/规范 JSON、正整数 cursor、上一 event 与 result 均提交且 event ID 一致、result SHA 有效、`WorkerEventResult` 合法及 checkpoint 等于上一 result.state 时才能交给 worker。验证必须与读取在同一 `BEGIN IMMEDIATE` 内；失败先持久 PAUSED 再报错。该一致性校验不重建 Backtrader native indicator/order/ref，也不证明 broker dispatch 原子性 | `agent/src/execution/event_journal.py::get_checkpoint`、`test_event_journal.py`、`test_event_worker.py`；B88。 |
| D-049 | PAPER emergency pause 可由当前 API 的认证主体调用，即使 shared-key/loopback 不能归因到具体人；这是单向关闭新增派单的风险降低操作。接口不接受请求体，scope 必须由可信 manager active entry 解析；持久 PAUSED 确认前不回报成功。pause 不取消现存订单、不释放 owner lease、不停止 callback/reconciliation，不授予任何其他管理动作 | `agent/src/api/paper_execution_routes.py`、`frontend/src/pages/Execution.tsx`；B89。 |
| D-050 | CANCELLED/REJECTED 确认后若收到新的有效 FILL，保留原 intent 终态并累计券商成交事实，但在同一 SQLite 事务将精确账户 scope 置为 RECOVERING；未完成 broker reconciliation 前不允许新派单。迟到 fill 不恢复已释放 reservation，也不猜测性调整账户快照 | `agent/src/execution/order_ledger.py::record_broker_event`；B90。 |
| D-051 | 恢复快照只有在逐行 payload 的规范 JSON/SHA-256 有效时才可供 reconciliation 使用；reconciled proof 绑定按 kind/fact_key 排序后的完整事实摘要，二次读取时必须一致。此摘要用于发现持久事实不一致，不是带密钥的抗恶意篡改签名 | `agent/src/execution/ibkr_reconciliation.py::complete_snapshot`、`reconcile`；B91。 |
| D-052 | SMA 的交叉边界沿用已冻结的严格规则：上一差值 `<= 0` 且当前 `> 0` 才买入；上一差值 `>= 0` 且当前 `< 0` 才退出；差值恰等于 0 本身不触发。样本首个有效差值已 bullish 时不追买；若已有 long 持仓，只在后续 bearish cross 退出实际整股仓位。B92 仅补真实 Backtrader/BackBroker 证据，没有改变交易语义 | `CONTRACTS.md §2.1`、`agent/src/bt_runtime/strategies/sma.py`、`test_sma_backtest.py`；B92。 |
| D-053 | 未受信任 Python worker 的失败反馈只允许稳定类别、白名单内置异常类型、bar 序号与上传源码行号；host/API message 由固定模板生成。原始异常消息、traceback、stderr 与策略 stdout 不进入客户端响应或持久 run state，避免错误反馈成为策略输出/数据泄露通道 | `agent/src/execution/strategy_worker_image/worker.py`、`python_sandbox.py`、`event_worker.py`、`runs_routes.py`；B94。 |
| D-054 | `rsi_threshold/1.0` 固定用 SMA seed 的 Wilder RSI：首个 RSI 在 period 个 close-to-close 变化后可用；两均值均为 0 时取 50，单边涨/跌分别取 100/0。需要 period+2 根 bar 才能判断一次穿越；首个有效值只建立前值、不追逐样本开始前状态。空仓仅在前值 `>= entry_below` 且当前 `< entry_below` 买入；持仓仅在前值 `<= exit_above` 且当前 `> exit_above` 卖出；阈值相等不触发，RSI 与阈值差值在 `1e-12` 内按相等处理以消除浮点误差。参数只允许 period 2–250 与显式 `0 <= entry_below < exit_above <= 100`，无隐式默认。preview/create 用同一 compiler identity 和冻结 spec。此决定只定义受控离线模板，不证明 provider 或 PAPER 结果 | `CONTRACTS.md §2.2`、`agent/src/bt_runtime/strategies/sma.py`、`strategy_design.py`、`runs_routes.py`、`Execution.tsx`；B95。 |
| D-055 | RSI NL2 模型输出只能符合严格 `RsiThresholdIntent` schema，不生成代码或 StrategySpec；period/entry/exit 各自需有 exact evidence quote。Host 要求规范化 quote 是原请求子串、quote 内数值等于 intent 字段，并出现对应的周期/entry-lower/exit-upper 语义词；无法证明原文明确给出数值时拒绝，不允许模型填默认参数。StrategySpec 经静态校验后才进入 preview token；create 消费同一 token 内的冻结 spec，不重跑模型。该机制只校验参数出处与 schema，不证明条件抽取完整；UI 仍必须展示原文和规则供用户确认 | `agent/src/execution/strategy_design.py`、`runs_routes.py`、`test_strategy_design.py`、`test_backtrader_backtest_api.py`；B96。 |
| D-056 | Manager-owned Python PAPER deployment 由显式 `runtime_factory(scope)` 和 `session_builder(runtime, scope)` 两阶段构建。工厂只允许同 IBKR/PAPER scope、同 runtime/service、同 canonical durable SQLite 路径的未启动且未持 lease 组合；对象构造不能启动 SDK、恢复或获取 owner lease。`PaperExecutionManager.start()` 是连接/恢复边界，session 的管理撤单只转发本地 intent ID 至 runtime，由 manager grant 与 runtime durable binding/scope 检查共同守护；恢复失败仍保留 manager slot 与 lease，获授权 stop 通过 runtime quiescence 收尾 | `agent/src/execution/managed_python_paper_deployment.py`、`managed_python_paper_session.py`、`paper_execution_manager.py`；B98。该 factory 尚未注册到 app root，也不代表账户、行情、身份或风险 provider 已就绪。 |
| D-057 | FastAPI shutdown 是可信进程生命周期动作，不伪造用户 principal，不隐式撤单/清仓。调用 manager 当前 runtime 的 stop/quiescence；只有 runtime 返回成功、durable gate 为 STOPPED 且 owner lease 已释放时才清除唯一 manager slot。若停机未确认或抛错，保留 slot、lease 可见状态并记录稳定异常类型；不把 shutdown 视为 process crash 恢复证明 | `agent/src/execution/paper_execution_manager.py::shutdown_active_runtime`、`agent/api_server.py::_lifespan`；B99。 |
| D-058 | `PaperExecutionManager.start()` 必须先检查可信 app composition 声明的 `PaperExecutionReadiness`，缺任一策略 session builder、attributable authorizer、账户/对账 facts、行情/verified contract 或 risk/cost facts 时，在 runtime factory 调用前拒绝。遗漏 readiness 按全缺项处理；`state._set_paper_execution_composition()` 将同一不可变对象绑定 status 与 manager，active slot 期间禁止替换。完整 readiness 仅允许既有 recovery/start_paused 到 PAUSED，不授权激活或订单；静态 flag 也不认证 provider 的真实性 | `agent/src/execution/paper_execution_manager.py`、`agent/src/api/state.py`；B102。 |
| D-059 | Trusted NL2 provider 报告在发布前必须经独立进程中的第二个真实 Backtrader/BackBroker replay。decision log 以 verified bar 的 1-based 序号绑定日期/方向/股数/理由；复算核对执行订单核心事实、fills、equity、closed trades、现金/价值、费用、持仓与指标，并验证 manifest、strategy version、instrument 与 cost 声明。策略 sizing 元数据允许保留为首跑附加字段。Replay 证明捕获的决策所产生的执行/会计结果一致，不证明信号算法独立正确、数据来源真实或账户事实准确；失败必须发生在 `execution_report.json` 发布前 | `agent/src/bt_runtime/report_replay.py`、`agent/backtest/runner.py`；B103。 |
| D-060 | 当前单用户 PAPER runtime 只接受显式 `IB_GATEWAY_PAPER/4002` 或 `TWS_PAPER/7497` profile、loopback host 与一个 `DU[0-9]+` account；known LIVE defaults 4001/7496、custom/unknown port、remote host、profile mismatch 及不符合 ID 形式在本地账本/SDK process 构建前拒绝。底层 managed runtime 同样拒绝 loopback 外 host 和非 4002/7497。`managedAccounts` 必须精确仅含配置账户，不接受混合/重复/空列表；否则 durable callback 不 ACK、handshake 不完成、不取 snapshot。IBKR 可重配端口，因此本版本不支持 custom PAPER ports；静态 profile/DU pattern 和 native callback 只能降低误连，不是 broker-side mode attestation，真实连接继续需要用户明确 PAPER config 与可信 app composition | `agent/src/execution/ibkr_paper_runtime_factory.py`、`ibkr_managed_runtime.py`、`ibkr_callback_pump.py`；B104。 |
| D-061 | Python upload policy 放在 public/application trust boundaries：preview/create 的 shared preparation、resume 重新装载 frozen version 前将其状态切为 running、online coordinator release/stage event 前、managed PAPER session composition 时；不在通用 `invoke_strategy_worker` / offline journal 执行，避免把安全 probe/test source 从真实 Docker+Cerebro 运行链移走。Policy 限制 imports 与顶层 `on_event(event,state)` 形状，拒绝 AST 直接出现的动态执行/反射和 dunder；别名/字符串形式不保证。它是 API/兼容面筛查而非 Python sandbox，实际隔离依旧由 Docker boundary 提供，上传 Python 仍仅离线运行 | `agent/src/execution/strategy_source_policy.py`、`event_worker.py`、`managed_python_paper_session.py`、`runs_routes.py`；B105。 |
| D-062 | PAPER manager 与 readiness 归属单个 FastAPI `app.state`：status/pause 从 `Request.app` 取值，lifespan shutdown 从同一 app 取 manager；可信 composition root 应调用 `_set_paper_execution_composition(manager, readiness, app=app)`，确保同一 readiness 绑定 manager 启动 gate 与 status。旧无 app 全局 state helper 仅为兼容，不能用于真实 app route/lifespan composition。未注册 composition 的默认 app 仍是 inert | `agent/src/api/state.py`、`paper_execution_routes.py`、`agent/api_server.py`；B107。|
| D-063 | Trusted NL2 回测 preview 必须将实际 provider bars 规范化为完整 session CSV，使用 `import_completed_daily_csv` 验证，再把 raw CSV、metadata、expected session schedule 及 data/manifest SHA-256 与短期一次性确认 token 一起持久化。Review hash 包含两项摘要；create 读取 token 中同一快照，runner 再导入并核验 hash 后回测，禁止确认时重新抓取。报告 artifacts 留存同一 CSV/manifest。快照可复现但 provider 来源、许可、point-in-time、adjustment、公司行动与 broker contract mapping 仍未认证 | `agent/backtest/runner.py`、`agent/src/api/runs_routes.py`、`frontend/src/pages/Execution.tsx`；B110。 |
| D-064 | App-scoped `_set_paper_execution_composition()` 对相同 manager/readiness pair 可幂等；若当前 app manager 有 active status/slot，拒绝替换并保留旧对象，避免正在运行的 runtime/lease 脱离 app lifespan shutdown。只有无活动 slot 才可换 manager；此规则不填充 readiness、不建立 production composition，也不启动 broker runtime | `agent/src/api/state.py`、`agent/tests/test_paper_execution_routes.py`；B109。 |
| D-065 | Python upload preview token 必须绑定 confirmation identity、digest-pinned worker image 和市场快照摘要；短期 SQLite authorization row 保存 schema-versioned CSV/metadata/session schedule 及快照摘要。Create 接受不含市场数据的轻量确认请求；兼容的重传字段不会参与执行，必须从 token 读取快照、验证格式/hash、重新运行 importer，并在创建 run/version 前由完整 review hash 原子消费 token。消费成功立即清空 snapshot/spec 大 payload，新 preview 清理已消费/过期行。意外损坏可被摘要检测；本地可写数据库的攻击者可能同时改写快照和记录，因而这不是签名/防篡改存储。数据 source/provenance 仍不认证 | `agent/src/api/runs_routes.py`、`frontend/src/lib/api.ts`、`frontend/src/pages/Execution.tsx`、`agent/tests/test_backtrader_backtest_api.py`；B112。 |
| D-066 | Preview authorization 每条授权序列化 payload 上限 80 MiB、所有未消费且未过期授权 payload 合计上限 256 MiB；按 SQLite TEXT UTF-8 BLOB 长度，在 `BEGIN IMMEDIATE` 中先回收 expired/consumed rows 后再检查并插入，以免并发请求绕开容量边界。单项超限返回 413，活动总额超限返回 429；失败不创建 token/run/version。消费后清空数据让 SQLite 可重用页面，但不承诺数据库物理文件缩小，也不替代 artifacts 磁盘配额或保留策略 | `agent/src/api/runs_routes.py`、`agent/tests/test_backtrader_backtest_api.py`；B113。 |
| D-067 | 模型生成的 SMA fast/slow period 都必须有 request 原文短引文，值精确匹配且数字与 fast/slow 角色就近绑定；缺失、无效或角色歧义 fail-closed，不能由模型补默认值。 deterministic exact SMA grammar 保持独立。已知 unsupported 预检覆盖有限的中英文指标、风险/仓位、价格/日历/周期词族；该词典和 evidence 不能证明任意描述的意图完整性 | `agent/src/execution/strategy_design.py`、`agent/tests/test_strategy_design.py`、`agent/tests/test_backtrader_backtest_api.py`；B114。 |
| D-068 | PAPER status 的 readiness 以 `PaperExecutionManager.startup_readiness`（实际 `start()` gate）为权威。若 app state 与 manager bound readiness 不同，status 显示 `composition_ready=false` 和 `readiness_binding_mismatch`，并列出 manager 侧缺项；无显式 app readiness alias 时可从 manager bound object 补齐。此举保证诊断与启动结果一致，不认证 readiness collaborator 的真实性或生产可用性 | `agent/src/api/paper_execution_routes.py`、`state.py`、`agent/tests/test_paper_execution_routes.py`；B115。 |
| D-069 | IBKR commission callback 作为 scoped immutable fee fact 持久化，按原始规范 payload hash 精确去重；同一 scope/execId 内容变化追加事实并派生 `PENDING_REVIEW`，绝不原地覆盖。fee 可早于 execution 保存，匹配 fill 后由 scope + execId 派生关联；所有金额保持 `REPORTED_UNCONFIRMED`，不改变 Backtrader 累计佣金或声称已完成 broker reconciliation | `agent/src/execution/ibkr_callback_pump.py`、`order_ledger.py`、`agent/tests/execution/test_ibkr_callback_pump.py`；B116。 |
| D-070 | `PythonBacktestEventCoordinator` 遇到非 int/非正/越界 host seq 时，先持久暂停当前 backtest journal，再抛 `EventWorkerError`；bool 不视为 int。失败前不调用 worker，不推进 cursor，后续合法 seq 不能恢复该次 run。它只约束 host cursor 类型和范围，不证明所有 forged in-range cursor、内省或容器逃逸面 | `agent/src/execution/event_worker.py`、`agent/tests/bt_runtime/test_backtest_event_journal.py`；B117。 |
| D-071 | Atomic SQLite migration 必须先生成并验证 pre-migration backup，再于单一 rollback-capable transaction 执行。成功迁移后该备份仍能验证并恢复旧 schema/user_version；restore gate 必须进入 `RECOVERING` 以要求之后进行 broker reconciliation | `agent/src/execution/sqlite_backup.py`、`agent/tests/execution/test_sqlite_backup.py`；B118。 |
| D-072 | 发布后的 Backtrader execution report 以固定 schema 的同目录 sidecar 记录相对路径、字节数与 SHA-256；新格式 run 在 API 读取时必须重新验证，失败返回 409。无 sidecar 的合法旧报告显式标记 `unverified_legacy`；sidecar 与报告同机可写，不能作为签名或防篡改存储 | `agent/src/bt_runtime/report_artifact_integrity.py`、runner、Python upload service、`runs_routes.py`；B119。 |
| D-073 | Host feed timestamp 只有在严格为 timezone-aware `datetime` 且可规范化为 UTC 后才可用于对齐 verified bar；错误类型、naive datetime、tzinfo 异常均在 worker 前持久暂停 journal，避免原生 Python 异常留下可继续运行的 cursor | `agent/src/execution/event_worker.py`、`agent/tests/bt_runtime/test_backtest_event_journal.py`；B121。 |
| D-074 | PAPER start/activate/cancel/stop HTTP 管理操作必须使用 require_auth 返回的 Principal，再由 manager 检查可归因身份和逐项 control grant。启动 scope 只允许 trusted app composition 安装；其他管理操作解析 manager 的 active scope。共享 key/loopback 不是用户身份，默认 app 没有 scope/factory 时不得启动连接 | `paper_execution_routes.py`、`paper_execution_manager.py`、`api/state.py`；B121。 |
| D-075 | run detail 读取时，integrity sidecar 的存在本身即为新格式证据，config marker 丢失不能将报告降级为 legacy；出现未知 config schema marker 必须 fail-closed。缺少 config marker 且没有 sidecar 的合法旧报告保持 `unverified_legacy`。sidecar/config/report 同机可写，仍不防同主体 co-modification | `agent/src/api/runs_routes.py`、`agent/src/bt_runtime/report_artifact_integrity.py`、`agent/tests/test_backtrader_backtest_api.py`；B122。 |
| D-076 | PAPER app composition 通过类型化 `PaperExecutionComposition` 显式交给 FastAPI app root；lifespan 启动只把 manager/readiness/scope 安装到该 app state，未注入时建立 inert manager/readiness。构造与注册不启动 runtime；默认身份/grant/readiness gate 仍有效。此 hook 是可部署接线点，不是 production composition 或 provider 认证 | `agent/src/execution/paper_execution_composition.py`、`agent/api_server.py`、`agent/tests/test_state_migration_wiring.py`；B123。 |
| D-077 | execution report integrity API 必须返回与 sidecar 校验相同的单次 bytes snapshot；禁止校验后再次打开路径以构造响应。SHA-256 sidecar 与配置仍可由同一本机主体修改，不构成签名或独立存储 | `agent/src/bt_runtime/report_artifact_integrity.py`、`agent/src/api/runs_routes.py`、`agent/tests/test_backtrader_backtest_api.py`；B123。 |
| D-078 | Feed timestamp 接受精确标准库 `datetime` 类型，不接受可覆写时区虚方法的子类；UTC 归一化结果须仍为精确 aware datetime，bar 时间获取与比较失败必须先持久暂停 backtest journal，再返回稳定 worker 错误 | `agent/src/execution/event_worker.py`、`agent/tests/bt_runtime/test_backtest_event_journal.py`；B123。 |
| D-079 | `PaperExecutionService.start()` 始终是 recovery operation 而非“确保仍在 RUNNING”的幂等调用；即使同一 service 持 lease 且 durable state 为 RUNNING，也先持久转 RECOVERING 并运行 fresh recovery check，成功后只到 PAUSED，后续必须显式 activate。runtime `begin_recovery()` 可复用现有 lease，但不能绕过关闸 | `agent/src/execution/execution_service.py`、`agent/tests/execution/test_execution_service.py`；B124。 |
| D-080 | Uncaught `ManagedPythonPaperSession` thread failure 尽力先持久暂停对应 execution journal，再暂停仍为 RUNNING 的 scope；管理状态仅展示固定 `managed_python_session_failed`，不暴露异常消息。持久层写入失败时不可声称 durable alert 已成功。此诊断/关闸不恢复 native Backtrader order/ref，也不证明 callback/cancel 在异常后的持续服务 | `agent/src/execution/managed_python_paper_session.py`、`paper_execution_manager.py`；B125。|
| D-081 | `PortBroker.restore_reconciled_order()` 是离线 native-order 重建 primitive：仅在完整 PAPER reconciled snapshot hydration 后、broker start 前，用 strict BUY facts 重建 durable BT ref 与累积执行 bits；不发送订单、不重复改账户快照中的 cash/position。broker-local ref 从恢复 high-water mark 后继续分配。BUY-only 限制源于缺少 SELL 逐笔 close/open/PnL 成本事实；primitive 未接 `ManagedPythonPaperSession`，不等同 cold-start recovery 完成 | `agent/src/bt_runtime/brokers/proxy.py`、`agent/tests/bt_runtime/test_broker_proxy.py`；B126。|
| D-082 | Managed Python PAPER session 只接入 exact linked-outbox 的 BUY 活动单冷启动恢复：订单必须属于当前 RUNNING journal run，ledger intent 与当前 strategy version/deployment、run event/result/proposal/prepared intent/link、broker binding/feed 与 reconciled account snapshot 完全匹配。callback worker 在恢复阶段 quiesce，核验后 native refs/Cerebro ready 才恢复消费；不确定 state、pending durable delivery/费用、SELL 等继续 fail-closed。此为离线窄切片，不升级 T41/T42，也不证明真实 Gateway crash recovery；无 bar 且持仓时 Backtrader clean stop 的估值边界未修复 | `agent/src/execution/managed_python_paper_session.py`、`ibkr_callback_pump.py`、`ibkr_reconciliation.py`、`event_journal.py` 与 execution tests；B127。|
| D-083 | Managed Python session 组合时必须链式包装而非替换 runtime callback failure sink。致命 callback consumer 故障会尽力持久暂停对应 strategy execution run、记录进程内失败诊断，再委托既有 runtime handler 暂停账户 scope；API 只暴露稳定错误码。不能在 sink 中同步 join/stop callback worker。SQLite 不可用时不得声称 journal pause 已 durable；该机制不证明阻塞策略期间 callback/cancel 服务连续性 | `agent/src/execution/managed_python_paper_session.py`、`ibkr_managed_runtime.py`、`ibkr_callback_pump.py`；B132。|
## D-118 trusted-template 与 Python upload 共用 manifest evaluation window

B173 将 NL2 trusted SMA 的评价窗口明确绑定到数据 manifest。warmup bar 继续用于指标预热和完整资产轨迹，但年化收益按 `evaluation_start..evaluation_end` 内的 bars 计算，与 Python upload 一致；报告持有窗口日期与 bar count，独立 BackBroker replay 接收同一 evaluation start，验证报告窗口/count 与 manifest/replay 相符。此决定统一评价范围，不裁剪全 feed equity curve，也不认证行情来源、point-in-time 可用性或 PAPER 执行。

## D-119 Importer fetched_at 不得早于已导入 bar 可用时间

B174 将每根 bar 的 `available_at` 与快照 `fetched_at` 建立最基本的时间先后约束：`fetched_at < max(available_at)` 时拒绝，时间相等可接受。该规则使 manifest 内部时间自洽，但这些时间仍由调用方声明，不认证供应商、下载记录或 point-in-time 历史。

## D-120 首期 IBKR 合约映射只允许 USD 普通股

B175 在可信 dispatch 边界要求解析合约为与冻结 instrument 一致的 `STK`，币种必须精确为 `USD`。不符合的 contract 在分配 broker order ID、建立 durable binding、派单或调用 transport 前拒绝。其他币种/市场需单独设计 capability 和风控，当前不尝试自动转换或放宽币种。

## D-121 NL2 preview 的冻结产物必须通过真实 API runner 走到可验证报告

B176 增加 FastAPI preview→confirm→report detail 的集成验收，不替换 trusted runner；confirm 消费已审阅的冻结行情快照而不再次取数，运行真实 Cerebro/BackBroker 和 independent replay，保存冻结版本与报告完整性 sidecar，再由 detail API 验证读取。该测试使用确定性 fixture，仅证明产品/API/引擎连接，不代表真实 provider 或 PAPER 证据。

## D-122 Python 上传正式 API 的验收必须跨越真实 Docker worker 与真实 BackBroker

B177 让 Python upload 的 preview→confirm→persisted detail 路径使用 digest-pinned Docker worker，每个事件只收到当前已完成 bar；可信 host 把 proposal 交给真实 Cerebro/BackBroker，订单在下一 bar 按明确股数执行，再由独立真实 BackBroker 子进程复算并验证完整性 sidecar。测试使用 fixture 数据与本机 Docker，不能据此声称真实 market data 或 paper order 已通过。
