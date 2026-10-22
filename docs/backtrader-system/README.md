2026-10-22 B195 已把 digest-pinned Python worker 配置到本机用户环境，并重启开发后端；后端进程确认读取 `VIBE_PYTHON_WORKER_IMAGE` 与 `DOCKER_CONTEXT=colima-vibe-execution`，Docker 预检和隔离 worker round-trip 通过。B194 的 PAPER IB Gateway 只读验收通过：4002 握手、AAPL 23 根日线、NL2 与 Python upload 两条真实数据 Backtrader 回测均 integrity verified。Gateway/TWS 端口已可在 UI 选择。定向后端 **46 passed**，Execution/Portfolio UI **23 passed**，production build 成功。未读取持仓、未做 what-if 或下单；行情许可、point-in-time 和复权语义仍未核验，真实 PAPER 订单回报/对账闭环仍待验收。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[BLOCKERS.md](BLOCKERS.md)。

2026-10-22 B192 修正 recovery teardown 验收：已确认策略失败时 runtime 按设计保留未 ACK callback、STOPPING gate、owner lease 和 callback worker，直到显式 recovery；测试在确认原始 callback spool 清空且 inbox 持久后清理 fake worker。execution/Backtrader/API/PAPER 合并组 **715 passed、19 subtests**。B191 的双入口报告展示与 RSI 文案修正有 **14 UI tests passed**、build 成功；B190 独立重算 NL2 sizing claims 的核心组 **54 passed**。T44 仍存在 signal/coordinated intent 及共因算法边界；真实 PAPER 和账户证据尚未完成。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B188 修复 T44 报告投影会忽略额外订单字段的问题：Python upload 正式验证现在拒绝 verified execution schema 之外的订单字段；真实 Backtrader 报告注入伪造 `broker_reported_realized_pnl` 后被拒绝。回测与独立 replay 定向 **10 passed**；加入 B187/B188 后 execution/Backtrader/API/PAPER 合并组 **709 passed、19 subtests passed**。这仍不覆盖任意 Broker/Analyzer 攻击或共因算法错误。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B187 补足 event_result/checkpoint/outbox 事务中途失败证据：真实文件 SQLite 在事件 commit marker 更新处注入 abort，重开后确认无半提交，移除 trigger 后同一结果只提交一次。`test_event_journal.py` **24 passed**。官方 `ibapi 10.50.2` 独立 runtime **46 passed**。这不证明任意 OS/磁盘崩溃或 broker exactly-once。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B184–B186 按方案继续补强离线边界：PAPER composition/start 必须固定有效策略 SHA-256 版本；Python worker 启动前核对 Docker daemon seccomp 并显式请求 builtin profile；daily importer 拒绝 session date 与声明时区开/收盘跨日错配；策略线程失败后 callback pump 仍可持久入账并处理授权撤单，未 ACK 回报留待显式恢复。最新 execution/Backtrader/API/PAPER 合并回归 **708 passed、2251 warnings、19 subtests passed**，Docker 配置目标 1 passed；编译及 16 个目标文件 whitespace/conflict scan 通过。市场真实性、完整容器逃逸、生产 app root/providers 和真实 IBKR PAPER 仍未验收。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B182–B183 增加指定 YFinance 的精确日线获取路径和 Python 上传 PAPER 离线组合：YFinance 不再经宽泛 loader registry 静默回退，并在冻结 manifest 记录宿主观察到的请求/缓存状态/返回表与快照哈希；Python 上传固定源码、版本、policy、sandbox 和 shared SQLite 并接入 managed session。市场/API 定向 **62 passed**，组合/deployment/API 定向 **42 passed**，execution/Backtrader/API/PAPER 相关组 **687 passed、19 subtests passed**。供应商认证、生产 facts provider、主 app 安装和真实 IBKR PAPER 仍未验收；T07/T08/T14/T40/T41/T42 继续 PARTIAL。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B179–B181 修复双入口版本复用、provider snapshot manifest 自洽和恢复 gate：NL2 回测版本改存 `backtrader/{version}` 并由 PAPER validator 接受；DataManifest v1.1 固定币种，snapshot 检查范围/available_at/source/adjustment/completion；恢复态无法仅凭 journal event hash 续跑，要求 shared execution gate 已 RUNNING。定向组合 **105 passed**；execution/Backtrader/API/PAPER 相关组 **673 passed、19 subtests passed**。T07/T08/T12/T41/T42 仍 PARTIAL；无 Gateway/account/order 操作。

2026-10-22 B178 修复 SQLite 备份恢复后账户 gate 与策略 journal 状态分裂：同一事务将 gate 置为 RECOVERING 并暂停 RUNNING journal；恢复后普通 identity/stage/release 失败，仅 exact released-event 显式恢复可续跑。备份/事件日志 **30 passed**。T34/T42 仍 PARTIAL，异机恢复演练和真实 broker reconciliation 未完成。

2026-10-22 B177 把 Python 上传完整 API 路径接到真实隔离 worker 验收：Docker 构建 digest-pinned worker，经 preview→confirm 执行当前 bar only 策略、BackBroker native BUY/next-bar fill 与独立 replay，随后 run detail 读取报告 sidecar 成功。新增集成用例 **1 passed**；Execution UI **13 passed**，production build 成功。数据仍是 fixture，无真实 IBKR/PAPER。详见 `PROGRESS.md`、`ACCEPTANCE.md` 与 `BLOCKERS.md`。

2026-10-22 B174–B176 收紧三处闭环：importer 拒绝 manifest `fetched_at` 早于任意导入 bar 的 `available_at`（相等允许）；PAPER dispatch 拒绝非 USD STK 合约，且在订单 ID 分配/binding/send 前失败；NL2 preview→confirm→run detail 新增真实 API+runner+Cerebro/BackBroker 用例，确认确认阶段不重取行情且产物/sidecar 可验。市场数据+dispatch 定向 **24 passed**，再加入 Backtest API 全文件的组合 **55 passed**；真实 provider、生产可信 provider wiring 与 IBKR PAPER 仍未验收。详见本目录各证据文档。

2026-10-22 B173 对齐 NL2 trusted-template 与 Python upload 的回测窗口：trusted SMA 报告和独立 BackBroker replay 现在都按 manifest evaluation bars 计算年化收益，报告显式绑定 evaluation window/count，并拒绝 replay 窗口或 bar count 被篡改。定向回归 **30 passed**；含 B171/B172、API/backend routing 与 Docker worker 的合并组 **92 passed**。数据真实性、跨入口完整业务一致性与真实 PAPER 仍未验收。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B172 验证 Python `explicit_quantity` 在 BackBroker 资金不足时不被 host 静默缩量：200 股提案原样进入真实 Backtrader，订单为 `Margin`、零成交，独立 BackBroker replay 验证通过。新增负向回归；本轮 importer/upload/API/Docker 组合 **75 passed**。这证明离线 BackBroker 语义，不是账户风控或 broker 拒单证据。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B171 加固通用 completed-bar importer：manifest warmup 起点和 evaluation 终点必须分别锚定导入首末交易日，warmup_end/evaluation_start 必须确实存在于导入日历。4 种边界伪造均拒绝；旧 API/upload/真实 Docker worker fixture 组合通过。此项不认证行情供应商、许可或 point-in-time。T07/T09 仍 PARTIAL。

2026-10-22 B170 修复 managed Python PAPER 启动期间异常没有统一持久暂停的问题：reconcile 后的 hydration、active-order restore、CREATED staging、callback barrier 与线程启动任一环节失败，都会 best-effort 持久 PAUSE run；若 execution scope 已 RUNNING 则重新关闸，并保留 owner lease。注入 hydration 后晚期故障验证 PAUSED run/scope、无发送、不可直接 activate。最新 execution/Backtrader/API/PAPER 合并组 **658 passed**。T41/T42 仍 PARTIAL，无真实 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B169 修复 active BUY 恢复时把多笔 SQLite fills 压成一次加权均价成交的问题：host 现在校验 execution IDs、逐笔数量/价格/UTC 时间、aggregate 数量/加权价/最新时间与 position cost basis，再逐笔重建 Backtrader exbits；损坏或顺序不一致继续拒绝，且恢复不重发。两笔不同价格/时间的 failed-session 恢复及重复 execution 通过。execution/Backtrader/API/PAPER 合并组 **657 passed**。真实 IBKR/Gateway 对账仍未验收，T41/T42 PARTIAL。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B168 修复 Python upload 忽略 manifest evaluation window 的问题：warmup 数据仍逐根输入 worker 以恢复策略状态，但 host 丢弃 warmup 期间的订单提案；报告增加 evaluation 范围/交易日和忽略提案计数，年化统计只按 evaluation bars 归一。独立真实 BackBroker replay 绑定同一窗口。相关 Backtrader/API 组 **123 passed**，边界定向 **2 passed**。equity curve 保留全量 feed 轨迹；真实 provider provenance 与真实 PAPER 仍未验收。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B167 在 Python event coordinator 的信任边界重复核对 frozen StrategySpec、USD、纽约时区、pinned XNYS 完整交易时段和可信当前时间；新 bar 若错 instrument/currency/timezone/session/open-close 或来自未来，先持久 PAUSE，零 stage/worker；durable released event 与 shadow replay 同样重验。Python/managed session 绑定一致 clock。execution + bt_runtime **597 passed**，真实 Docker 两用例 **2 passed**。测试数据修正了 DST 后的开收盘时间和策略日历身份。此证据不认证行情来源，T07/T09/T39 仍 PARTIAL；无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B166 为 NL2 澄清加入服务端 append-only transcript：每轮保存原始策略描述、答复和 parent turn，校验同 ID 重试、描述链与回测上下文；冲突/stale turn 返回 409。后端策略/API 76 passed，Execution UI 13 passed，production build 通过。T12 仍 PARTIAL：actor/tenant 隔离、留存/删除、任意自然语言约束遗漏与真实 provider 准确性尚未验收。未连接 Gateway/account/未发送订单。完整证据见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

# Vibe-Trading × Backtrader 系统设计与开发交接
2026-10-22 B165 收紧 B164 的失败 bar 恢复终态：只有唯一 exact-linked CREATED intent 确认 FILLED 后才继续策略事件；CANCELLED/REJECTED 触发持久 PAUSE 并要求 fresh reconciliation，防止本地取消/拒绝后出现迟到成交时仍推进策略。定向 CREATED/retry 场景 6 passed；managed-session + `PortBroker` + dispatch 回归 58 passed，runner 退出 `LIVE_THREADS []`。活动订单与 CREATED intent 同场恢复仍 fail-closed；无 Gateway/account/真实订单操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B164 打通 failed-session recovery 中“前序 committed event 留下唯一 exact-linked CREATED BUY，同时下一根 bar 已 released 未提交”的受限组合。恢复仍先完成 reconciliation、shadow replay 与 link 校验并保持 PAUSED；显式 activate 后旧 intent 先过二次风控，retry bar 只在该单收到 FILLED/CANCELLED/REJECTED 终态回报后才重新释放。风险拒绝保持 CREATED 并暂停；SEND_STARTED/UNKNOWN 或活动单不推进 retry event。定向组合 11 passed；managed session + Broker proxy + dispatch 54 passed；execution/Backtrader/API/PAPER 合并组 634 passed、1665 warnings、19 subtests。无 Gateway/account/真实订单操作；T41/T42 仍 PARTIAL。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B163 让唯一、精确链接且尚未发送的 CREATED BUY 可安全停止：先关闭 PAPER send gate 并持久暂停策略 journal；只有该 CREATED intent 仍是唯一可恢复项时才结束 Cerebro/runtime，保留账本记录且零发送。它没有券商单可撤，后续恢复仍须 fresh reconciliation 与独立显式 activate。此前新增的 8 个状态组合已通过；本轮未重跑测试。真实 Gateway/account/订单未触碰，T41/T42 仍 PARTIAL。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B162 补上 B159 committed outbox handoff 后的受限恢复：对账与 committed-prefix shadow replay 后，仅暂存唯一、同 run/version/account/deployment 且精确链接 proposal 的 CREATED BUY；启动保持 PAUSED，显式 activate 后才由 Cerebro thread 生成临时 native order ref、执行现有二次风控并调用 fake PAPER transport。PAUSED 零发送、成功恰好一次；二次风控拒绝保持 CREATED、零发送并暂停。定向 48 passed；execution/Backtrader/API/PAPER 相关组 628 passed、4597 warnings、19 subtests。首轮一个既有 sandbox helper timeout，单测复跑通过后整组复跑通过。多 CREATED、SELL、UNKNOWN/READY_TO_SEND、failed-bar 混合与 native ref 跨重启仍 fail-closed；T41/T42 仍 PARTIAL。未连接 Gateway、未读取账户或发送真实订单。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`、`BASELINE.md`。

2026-10-22 B161 新增 SQLite-backed PAPER 管理 grant evaluator，精确绑定可归因 subject/auth method/tenant、IBKR PAPER account、单一控制动作和有效期，支持撤销与重开持久性。管理 API/生产签发 root/identity provider 仍未接入，默认 app 继续 inert；T13 仍 PARTIAL。无 Gateway/account/order 操作。详见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`。

2026-10-22 B160 为 NL2 不完整策略加入结构化多轮澄清：缺字段以版本化表单展示，用户补充作为原始文本追加后重新运行受限解析；不支持策略仍 fail-closed，只有完整规则和冻结行情预览才能签发确认 token。UI 处理 stale preview 与双入口状态隔离。后端相关合并组 632 passed，Execution UI 13 passed，production build 通过。T12 仍 PARTIAL：多轮 transcript 已由 B166 持久化，但不能证明模型穷尽任意自然语言约束。未连接 Gateway/account/未发送订单。完整证据见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`。

2026-10-22 B159 修复 committed-but-unlinked Python outbox 恢复窗口：fresh reconcile 和 committed-prefix shadow replay 通过后，managed session 可幂等补完当前 run 的 outbox→ledger intent link；prepared intent/decision 带哈希并在恢复时重新核对身份；过程不 reserve/send。缺失 native Backtrader order ref 的 CREATED intent 仍使 session 保持 PAUSED。定向 64 passed，execution/Backtrader/API 合并 615 passed；Docker 重放 1 passed。T41/T42 仍 PARTIAL。完整验收数见 `PROGRESS.md` 与 `ACCEPTANCE.md`。

2026-10-22 B158 为 managed Python PAPER 启动和失败恢复增加只读 committed-prefix shadow replay：journal 校验连续事件/result hashes、逐项重算 frozen worker 输出、outbox proposal 与最终 checkpoint；seed 新增独立持久字段。失败持久 PAUSE，影子路径不 commit/dispatch。execution + Backtrader runtime/API 组合 610 passed、1615 warnings、19 subtests。T41 仍 PARTIAL，尚不覆盖完整 native Backtrader 状态或真实 worker/Gateway crash recovery。见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`。

2026-10-22 B157 将 Python upload 的客户端 session schedule 改为 pinned XNYS 权威校验：固定 `XNYS@4.13.2`/纽约时区，从 warmup_start 到 evaluation_end 逐项核对日期、UTC 开收盘、半日市和可选 hash，导入及 manifest 使用宿主生成的日历。假日/时段/版本/时区/hash 负例不建 run/version。API 文件 25 passed；市场数据和执行路由回归 20 passed。T07/T09 仍 PARTIAL，日历包不认证真实行情来源或官方更正。见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`。

2026-10-22 B156 增加 `build_frozen_nl2_paper_composition()`，显式验证 trusted NL2 frozen version、IBKR/PAPER scope/config、provider/feed/contract/authorizer 后组装版本固定的 ready manager；构造不启动、不 lease、不连接 Gateway，默认 API 仍 inert。离线 composition + 默认 inert 测试 10 passed。仍无生产 provider/config/app 注册或真实 PAPER 证据。见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`。

2026-10-22 B155 将 PAPER 活动改为 SQLite 同事务追加的 scope-local event sequence，并增加带 Last-Event-ID 补传的认证 SSE；Execution 基于 REST snapshot cursor 订阅并刷新活动面板，保留轮询回退。后端定向 74 passed；execution/BT/API 合并回归 607 passed、1563 warnings、19 subtests；Execution UI 11 passed、production build 成功。T29 部分推进，T30 仍 PARTIAL；默认 app inert，无 Gateway/account/order 操作。见 `PROGRESS.md`、`ACCEPTANCE.md`、`BLOCKERS.md`、`DECISIONS.md`。

2026-10-22 B154 为 T39 future-only canary 集成增加本次 run 的 `market_data.csv`、`verified_bars.json` 和 SQLite event journal 精确路径读取攻击；宿主确认文件在 worker 启动前存在且包含 canary，真实 Docker worker 均无法读取。launcher 契约也拒绝 host PID/IPC/network 与 device 选项。T39 仍 PARTIAL，内省与逃逸矩阵未完成；定向 Docker 用例 1 passed。无 Gateway/account/order 操作。见 [PROGRESS.md](PROGRESS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B153 新增认证 PAPER activity 只读 API 和 Execution 持久活动面板：可信活动 scope 下读取最近委托（含终态）、成交、未确认费用、恢复队列与本地 reservation；页面只在可验证 reconciliation snapshot 存在时展示券商持仓，否则标 unknown，risk limits 标 unavailable。执行/Backtrader/API 合并组 589 passed；UI 10 passed、production build 成功。当前默认 app 无生产 runtime composition；无 Gateway/account/order 操作。T29/T30 SSE 仍未实现。见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[BLOCKERS.md](BLOCKERS.md)。

2026-10-22 B152 新增 `TrustedTemplatePaperSessionBuilder`，可信 app root 可注入冻结 NL2 version、run/deployment ID、proposal/dispatch risk providers、完成 bar feed 与合约 resolver，构造同一 SQLite 上的 managed session；冻结 source/compiler/Backtrader、IBKR PAPER scope、策略政策、feed/contract mapping、run identity 与 provider 事实均校验，构造不启动 SDK、不持 lease、不调用风险 provider。拒绝错误 scope/policy/version/account/database 和非法 worker 参数。builder 专项 16 passed；包含早期 builder tests 的 execution + Backtrader + API 组合 576 passed、1562 warnings、19 subtests。生产 app root/授权与事实 providers 仍未接，真实 IBKR PAPER 未验收；没有 Gateway/真实账户操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[BLOCKERS.md](BLOCKERS.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B151 为 frozen `rsi_threshold@1.0` 补齐 managed PAPER 离线原生 BUY/SELL：RSI 下穿 entry 阈值后经 host sizing 买入 19 股，fake IBKR callback 确认成交后，上穿 exit 阈值卖出 19 股；Backtrader 持仓归零，账本与 callback inbox 排空。另为 managed deployment factory 增加可信冻结版本 pin，start API 只接受回显该 ID，拒绝请求切换策略版本。RSI 定向 1 passed、SMA+RSI 组合 2 passed；部署/API 组 27 passed；完整相关组 565 passed、1560 warnings、19 subtests。尚无生产 app composition root、grant/version-store provider 接线、权威账户/行情/风险 facts 或真实 IBKR PAPER 证据；没有连接 Gateway/真实账户。见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[BLOCKERS.md](BLOCKERS.md)。

2026-10-22 B150 接入 frozen `sma_cross@1.0` / `rsi_threshold@1.0` 的可信事件协调器与 `target_fraction` host sizing。SMA 已经由同一 durable outbox/ledger/Cerebro/PortBroker 链路完成真实 Backtrader native BUY 与 fake IBKR callback；RSI 已验证 Wilder checkpoint、entry/exit 阈值状态机，但未完成 native order 组合。过期/错身份风险事实 fail-closed。定向 9 passed，完整相关回归 542 passed；app-level deployment composition/provider 与真实 IBKR PAPER 仍缺；没有连接 Gateway 或真实订单操作。见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B149 修复 Python PAPER 事件桥只准 BUY 的退出缺口：host outbox/risk handoff 授权并核对精确 intent 后，真实 Cerebro 可提交 native SELL；host 风险事实仅有 1 股却申请卖 2 股的负例未建 reservation/未发送并持久暂停。SELL/native callback 定向组 7 passed；完整 execution + Backtrader runtime + API 回归 534 passed、19 subtests。fake transport 不是真实账户证据。B150 已给 NL2 SMA 增加 managed PAPER offline slice，完整 production wiring 和真实账户测试仍缺。无 Gateway/account/真实订单操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B148 续审时确认 managed PAPER session 仅接受 frozen `PYTHON_UPLOAD` + `explicit_quantity`；该缺口已由 B150 部分推进：SMA NL2 template 与 target_fraction 已进入离线 managed Cerebro/PortBroker/host risk 路径，RSI coordinator 则补了 checkpoint/state-machine 回归。尚不能声称 RSI native order、app deployment composition 或真实 IBKR PAPER 已通过。无 Gateway/account/order 操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[DECISIONS.md](DECISIONS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B147 为真实 Backtrader 报告补了独立金额 oracle，并让 synthetic runner 可选设置 BackBroker percentage slippage（默认保持 0）。12-bar 手算 fixture 覆盖两次 BUY、一笔 SELL 平仓和期末持仓；Decimal 手算 SMA(2/3) 交叉为第 4 bar BUY、第 7 bar SELL、第 10 bar BUY。49 股买入以 $10.05 high cap 成交；$6.90 开盘的 49 股卖出经 10% adverse slip 后由 $6.30 low cap，随后 44 股买入以 $13.20 high cap 成交；含两侧费用的已实现净亏损为 $191.7615，末现金 $221.6305，44 股按末价估值后权益 $749.6305，报告最大回撤 $291.995 亦经独立手算核对。独立 BackBroker subprocess replay 一致，篡改 total_fees fail-closed。新回归 1 passed；相关 SMA、数据导入、replay subprocess 与 Python upload 合并组 30 passed、437 warnings、4.03 秒；整个 Backtrader runtime + 回测 API 子树 116 passed、1147 warnings、9.04 秒。仍是合成数据，真实许可行情和完整报告口径不因此通过。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B146 再补 dispatch 的已受理/ACK 丢失窗口：假 Broker 先将确定性 attempt/client order ID `fsync` 到独立接受日志，再挂起并被 SIGKILL；ledger 重开后不重调 sender，假 Broker 只留一条接受记录。dispatch 专项 27 passed、0.49 秒。离线假 Broker 不证明 Gateway 受理或真实 ACK 丢失，T42 仍 PARTIAL。无外部账户/真实订单操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B145 补上 dispatch 的进程崩溃窗口：真实 SQLite `SEND_STARTED` 事务提交后、sender 尚未进入时硬杀子进程；重开账本后 dispatch 只进入 `RECONCILE`，risk provider/sender 调用均为 0。`test_dispatch_service.py` 26 passed、0.49 秒。仅证明本地 durable no-retry 边界，不证明真实 Broker/网络原子性或 reconciliation；T42 仍 PARTIAL。无 Gateway/account/order 操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B144 加强 T37 worker deadline 故障证据：恶意策略 fork 出忽略 SIGTERM 的子进程并先写入 ready marker，测试确认后杀死宿主 Python supervisor；容器内 2 秒硬时限仍使容器退出并由 `--rm` 自动移除。真实 Colima Docker 专项 1 passed（3.62 秒），launcher 单元 15 passed；无 Gateway/account/order 操作。此证据只覆盖当前 Docker 配置下的单次后代进程清理，不等于完整容器逃逸/daemon 故障矩阵、跨平台隔离或 T37 完成。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B143 补齐一次 failed-session recovery 内 exact-linked partial BUY、未 ACK cumulative callback 与原事件重试的组合验收。真实 Cerebro/SQLite + fake IBKR：恢复保留 ref=77 与 position=1；重复 execution 只记一次，durable inbox 在 PAUSED 下重放并 ACK；第二笔成交使旧单完成，之后独立 activate 才按原 event ID/seq 重试并提交一次，send=0。执行/API 合并组 181 passed、93 warnings、19 subtests；IBKR SDK 子进程 suite 46 passed。该用例在 PAUSED bridge 边界注入失败，不等于 Docker worker timeout 与活动订单同场景；真实 Gateway/account/进程崩溃仍未验收，T41/T43 继续 PARTIAL。恢复时重建 feed 必须先注册进新 Cerebro 再绑定 native orders，已修正顺序。无 Gateway/account/order 操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B142 开始打通 failed managed-session recovery：新增独立 `recover` 授权和 `/api/execution/paper/recover`，manager 保留原 runtime/lease；只针对策略/Cerebro 线程失败、旧 callback worker 仍健康的情况，先请求新的 IBKR `nextValidId`、fresh snapshot/reconciliation，再安全换接 durable callback sink、重建 Cerebro，并返回 PAUSED。恢复事件仍是原 released event，必须另行 activate 才重试。为同连接二次 reconciliation 增加隔离子进程 `reqIds(-1)` 命令，并在恢复失败时把 RECOVERING 收敛回 PAUSED、保持 allocator 关闭。定向 execution/API 组合 180 passed、81 warnings、19 subtests；IBKR SDK 子进程 suite 46 passed。成功端到端用 fake runtime + Cerebro/SQLite，仅覆盖无活动单的失败事件重试；部分成交 BUY + pending inbox 的失败后联合恢复、callback worker fatal、Gateway/进程崩溃、生产身份与真实 PAPER 均未验收。T41/T43 仍 PARTIAL。无 Gateway/account/order 操作。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。



2026-10-22 B141 为失败会话恢复补上 callback sink 安全换接原语：只在 callback worker 存活、健康、暂停且退出当前 `process_one()` 后换接；进程内 delivery claim 只有逐个对应未 ACK durable inbox 才能清除，随后由新 sink 幂等重放。session/journal/callback 定向组合 111 passed、71 warnings、6.41 秒。此原语尚未接入 session/manager，snapshot→quiesce、旧 broker notification/native state 迁移仍缺，T41/T43 仍 PARTIAL。无 Gateway/account。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B140 为失败会话恢复补上 journal 原语：检查并恢复 PAUSED 状态下同一个未提交 released event，事务要求 event ID/seq/payload hash、checkpoint 前缀一致，且无 result/outbox 或后续 staged event；只翻转 run gate，不改事件或 cursor。journal 定向 23 passed；没有接 manager/API/策略替换，也没有解决 callback→broker→native ACK 的交付屏障竞态，因此 T41/T43 仍 PARTIAL。无 Gateway/account。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B139 保证因活动订单而未完成的安全 stop 不会阻断风险降低操作：manager 保留 STOPPING 槽时仍接受独立授权的 exact-intent cancel；runtime 继续要求 scope 内 durable binding、PAPER 状态、存活 lease 与健康 SDK。B137 Docker timeout 场景先触发未完成 stop，再持久处理部分成交、拒绝共享 key 撤单、只发送 broker ID=55 的授权撤单并落账取消回报。定向 1 passed、3.86 秒；managed-session + manager 组 35 passed、4.88 秒；无 Gateway/account。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B138 修复超时后可错误重新 activate 的风险：若 `ManagedPythonPaperSession` 已捕获策略线程异常，即使 principal 有 activate grant，session 也拒绝重开 durable RUNNING 闸门，service 保持 PAUSED、order send=0。B137 Docker timeout 集成加入该断言，managed-session 文件 17 passed、5.36 秒；相关源码/测试 py_compile 与 diff check 通过。这个 fail-closed 修复不解决同一进程中替换失败 session、消化未 ACK callback 后安全恢复的问题；lease 保留、T41/T43 仍 PARTIAL。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B137 将真实 Docker 策略超时推进到 managed PAPER session：上传策略在容器内死循环后，真实 SandboxTimeout 沿异常链到达 Cerebro/session，execution service 与 SQLite run 均进入 PAUSED，released event 未提交且 result/outbox=0、cursor 不前进、没有策略订单发送；独立 callback worker 仍处理 `nextValidId` 候选和既有订单的部分成交。可归因 principal 在 PAUSED 下按 exact intent 发起撤单，非归因主体被拒，取消回报落账。真实 Docker 专项和 managed-session 文件验收结果见 [ACCEPTANCE.md](ACCEPTANCE.md)。使用 fake IBKR runtime；不涉及 Gateway/account，不证明真实 broker 行为、宿主/daemon 崩溃或生产管理接线；T43 仍 PARTIAL。详见 [PROGRESS.md](PROGRESS.md)。

2026-10-22 B136 增加真实 Docker worker deadline + SQLite event coordinator 回归：上传策略死循环被容器内 2 秒 deadline 归类为 SandboxTimeout，释放事件保持 authoritative、未提交结果/outbox/cursor 不前进；用不同 bar 重试仍不能替换已释放事件，两个超时容器均已删除。Docker 定向 1 passed、5.50 秒；event worker 回归 11 passed、0.20 秒。只证明 coordinator/journal 边界，不证明 ManagedPythonPaperSession 在真实容器超时后的 service pause/callback continuity。无 Gateway/account 操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B135 将 B127 的部分成交 BUY/native ref 冷启动恢复与 B134 的非空 Python checkpoint 续跑放入同一条 managed-session 重开 SQLite 回归。恢复启动未调用 sandbox、未推进 cursor、未重发；后台回报补齐成交后，恢复的 ref=77 在 Backtrader 完成，随后下一 bar 使用 durable checkpoint 仅提交一次。定向 1 passed，session/journal/callback 合并组 **96 passed**。fake IBKR/runtime + 真实 Cerebro/SQLite；没有真实 Gateway/account。T41 仍 PARTIAL：没有 Docker worker crash，也未覆盖 SELL/其它状态。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B134 增加 managed PAPER 有状态续跑对照：不中断参考 run 与重开 SQLite/new runtime 后都消费相同 completed bar；非空 Python event checkpoint、下一事件 state/proposals 与 durable cursor 一致，恢复启动不调用 worker，下一事件仅执行一次且无 broker send。checkpoint worker 使用确定性 fake sandbox；定向用例 1 passed，session/journal/callback 合并组 95 passed。此证据不代表真实容器 worker、活动订单与非空策略状态联合崩溃恢复或 T41 完成。无账户/Gateway 操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B133 补齐 T43 的离线并发切片：在 managed session 的策略事件卡在 sandbox 时，独立 callback worker 仍将部分成交写入账本；未归属的撤单被拒，可归因 principal 通过 manager 对 exact intent 发出撤单，取消回报落账。阻塞中的未提交 proposal 不生成 outbox、不派单；释放后才提交并派发。session/callback 组 **73 passed**，runtime/manager 组 **27 passed**。使用 fake IBKR 和测试构造的已绑定活动单，无 Gateway/account 操作。T43 仍 PARTIAL，真实 Gateway、进程崩溃恢复与生产管理通道仍未验收。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B132 收紧 T43 callback-worker 故障联动：managed session 现在链式保留 runtime failure sink；致命回调会尽力持久暂停对应 execution journal、设置稳定 session health code，并让既有 runtime 闸暂停 PAPER scope。fake IBKR worker 实际注入错账户回报，session/callback 定向组 **72 passed**。这证明故障被发现后的 fail-closed 联动，不证明策略死循环期间仍可持续接收正常回报或完成授权撤单；T43 仍 PARTIAL。无 Gateway、账户或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B131 在真实 Docker worker 内以 0.5 CPU 配额执行 2 秒 busy loop，读取 cgroup `cpu.stat`，断言墙钟至少 1.9 秒时 CPU 用量不超过 1.75 秒；定向用例 1 passed，相关 execution/Backtrader/API/PAPER 组合 **580 passed、1395 warnings、15 subtests**。这是单配置压力样本，T37 仍 PARTIAL。B130 同时补了 32 MiB 内存限额压力与容器回收负例；B128 补官方 SDK 安装器源码锁回归。本轮无 Gateway、账户或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B130 真实 Docker worker 在 32 MiB 内存上限下触碰最多 128 MiB 页面；只接受分配被拒或 OOM 退出 137，并验证容器已移除。定向 Docker 用例 1 passed，相关 execution/Backtrader/API/PAPER 后端组 **580 passed、1397 warnings、15 subtests**。这补上一个内存压力负例，不代表所有 OOM 或 CPU 饱和路径已验收；T37 仍 PARTIAL。无 Gateway、账户或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B129 在真实 Docker worker 中核对 memory/CPU/PID cgroup 生效值，并在 PID=4 下尝试派生 12 个子进程，内核拒绝超额创建；inspect 配置与运行时读数匹配。真实 Docker 负例 1 passed，相关后端/API/PAPER 合并回归 **580 passed、1393 warnings、15 subtests**。没有验证 OOM/CPU 饱和后的完整强杀回收矩阵，因此 T37 仍 PARTIAL。B128 另为官方 IBKR 安装器增加源码树锁/依赖锁/`uv` 调用契约回归并让 `install()` 自行复核，专项 7 passed；这不是官方发行包干净重装。本轮无 Gateway、账户、行情请求或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B127 将 B126 BUY native-order 重建原语接入受限的 managed Python PAPER session 冷启动路径：仅恢复 ledger 为 ACKNOWLEDGED/PARTIALLY_FILLED、scope/合约/feed/策略版本匹配、当前 RUNNING journal run 有 exact 已提交 outbox link，且订单/成交/费用/账户快照可核验的 BUY 单。恢复时暂停 callback worker 并等待当前处理完成，重验 reconciled snapshot 与 ledger；原生 ref 注册、Cerebro 启动后才恢复回调消费。缺 link、另一 run 的 link、活动手续费事实、pending callback/inbox/outbox、SELL/其它订单状态或任一事实不一致都会保持 PAUSED 并保留 lease；测试证明不重发旧单。完整 execution/Backtrader/API/PAPER 回归 **573 passed、1397 warnings、15 subtests**，session/callback 定向组 **71 passed**；compileall 与 diff check 通过。**T41/T42 仍 PARTIAL**：仅覆盖特定 BUY 活动单，未恢复取消/拒绝/SELL/不确定发送，未做真实 Gateway/进程崩溃矩阵；策略异常后的 callback 连续性和授权撤单也未验收。无 bar 且已有持仓时 Backtrader 收尾取市值会失败，需有可用行情 bar 才能证明 session clean stop。B126 的 broker 原语和 B125 的稳定健康码/UI 轮询仍保留；生产身份/provider 仍缺。本轮没有 Gateway、账户或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B114 收紧 NL2/SMA 意图：模型给出的 fast/slow 周期必须各自附原文引文、数值与角色就近绑定；补充中英文指标、止损/仓位/杠杆、价格/日历/周期过滤预检。缺证据的模型默认 10/30 在 preview 前拒绝、不签 token。B115 让 PAPER status 以 manager 实际启动闸 readiness 为准；声明漂移时显示 `readiness_binding_mismatch` 并 fail-closed。B116 将 IBKR commission 回报作为 append-only fee facts 持久化，支持先于成交到达、exact replay 去重和变化版本待复核，金额始终标为未确认。B117 让非法 host bar cursor 在策略 worker 前持久暂停。B118 补成功 SQLite migration→预迁移备份校验→恢复旧 schema 的证据。B119 为发布后的 execution report 写入同目录摘要并在 run detail 读取时重验；损坏报告返回 409，合法旧报告标记为未验证。B120 将非法 feed timestamp/tzinfo 失配升级为持久暂停；B121 接上需可信 scope 与可归因 grant 的 PAPER 管理 API；B122 防止完整性 sidecar 仍在时因 config 标记丢失而降级验证。T12/T22/T34/T39/T40/T44 仍 PARTIAL；共享 key/loopback 无法授权真实管理动作，主 app 无生产 runtime composition，sidecar 与报告仍可由同一主体改写。本轮未连接 Gateway、账户或发订单。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B113 为 preview authorization SQLite 增加容量闸：单个快照最多 80 MiB，所有未消费授权 payload 总计最多 256 MiB；达到总额时拒绝新 preview（429），不创建 run/version。API/service 16 passed，`compileall` 与 `git diff --check` 通过。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B112 将 Python upload 行情与 session schedule 快照写入短期 preview authorization；确认请求不再需要上传这些数据，旧客户端重传的内容会被忽略。create 从 token 快照重建、重跑 importer 并核验摘要；修改 SQLite 快照则在 run/version 创建前拒绝，消费 token 时释放快照 payload。后端/API/service 16 passed，完整相关后端组 478 passed、15 subtests；UI 7 passed，production build 成功。来源真实性与 PAPER 仍未完成。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B111 将 Python upload 的 digest-pinned worker image 纳入预览 review hash；创建时镜像变化会在 run/version 创建前返回 409，无效/未固定镜像不发 token；运行 config、报告和 `/execution` 审阅卡显示同一镜像身份，恢复也拒绝切换镜像。后端 API/service 16 passed；Execution UI 7 passed、production build 成功。T02 仍 PARTIAL：整体语义版本/运行身份矩阵未覆盖。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B110 让 trusted NL2 的策略预览同步冻结经过现有 XNYS completed-bar importer 校验的行情快照；review hash 绑定 data/manifest hash，确认后 runner 消费同一 CSV、metadata 和 session schedule，不重新抓取。run artifacts 保留同一 CSV 与 manifest；Execution 审阅卡展示两个 hash。真实 Backtrader 冻结快照/独立报告复算测试通过，完整 execution/Backtrader/API/PAPER 组 478 passed、15 subtests；UI 7 passed、production build 成功。行情 source、授权、point-in-time、复权与 instrument mapping 仍未认证。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B109 防止 app composition setter 在旧 PAPER manager 仍有 active slot 时替换它，以免 lifespan 只能关闭新 manager、遗留旧 runtime/lease；同 manager/readiness 重复注册幂等，空 slot 允许替换。定向 routes/state 组 18 passed；完整相关后端组包含此变更共 478 passed。没有解除默认 app readiness 缺项或连接 Gateway。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。


2026-10-22 B108 完成 IBKR 201 拒单的离线账本映射：只接受正整数 `request_id` 命中当前 scope 的持久 order binding、且零成交和非终态订单；事务性记录 REJECTED、释放剩余预留并回传 native Backtrader rejected 通知。相同回调幂等；未绑定、错 scope、部分成交或后续不同拒单内容均保持暂停/对账。定向回归 71 passed，完整后端组 475 passed、15 subtests，官方 SDK suite 44 passed。T23 仍 PARTIAL：没有真实 Gateway callback/reconciliation。无账户或订单操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B107 将 PAPER status/pause routes 与 FastAPI lifespan shutdown 改为读取各自 `app.state` 中的 manager/readiness，避免两个 FastAPI 实例共享进程级 manager 或 route 与 shutdown 管理不同对象。双 app status/readiness 隔离及各自 shutdown 回归通过；定向组 32 passed，完整 execution/Backtrader/API/PAPER 组 471 passed、15 subtests，`compileall` 与 `git diff --check` 通过。manager composition 仍需由可信 app root 显式注册，app 默认惰性、没有 IBKR runtime；无 Gateway/账户连接。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)、[DECISIONS.md](DECISIONS.md)。

2026-10-22 B106 开始补 IBKR 官方 SDK `error()` 回调分类：官方 SDK 的 5 参数回调 payload 由 adapter 保留并先写 durable spool；pump 仅忽略官方文档列明的 2104/2106/2107/2108/2158 farm 状态通知，1100/1101/1102/1300 触发暂停并要求重连/对账，订单拒绝及未知码继续 fail-closed。宿主 callback pump 47 passed，SDK callback 模块 14 passed。T23 仍 PARTIAL：尚未把绑定订单的 201 拒单事实写入 ledger，也未验证真实 Gateway 回调/对账。无 Gateway/账户操作。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B105 收紧 Python upload 的静态 API 面：preview/create 和 resume 路由、online Python PAPER session composition 使用同一 AST validator，仅允许明确的标准库 import，要求唯一的 `on_event(event, state)` 同步入口，并拒绝 AST 可见的直接动态执行/反射调用与直接 dunder 访问。通用隔离 worker 和离线 event journal 仍能承接 adversarial source，供容器/逐事件探针直接验证；所以该 validator 只作应用 API/兼容限制，不作为沙箱，也不保证挡住别名/字符串形式。定向组 41 passed；完整 execution/backtest/API/PAPER 回归 453 passed、15 subtests；真实 Docker 文件/网络隔离用例 1 passed。Python 上传仍仅用于离线回测；PAPER composition 仍 inert。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-22 B104 收紧 IBKR PAPER pre-connect 配置：要求显式 Gateway/TWS PAPER profile、loopback host、对应默认 PAPER 端口（4002/7497）及单个 DU 数字账户；拒绝 live 默认端口、未知端口和账户/managedAccounts 不精确匹配。runtime handshake 失败时不请求 snapshot/reconciliation。相关离线组合 77 passed。此配置检查不能认证 Gateway 实际登录模式；app 仍 inert，未连接 Gateway/账户。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B102 将 PAPER composition readiness 从 status/UI 诊断变成 manager 启动前强制闸：任何能力缺项都在 runtime factory 前拒绝；app 默认 readiness 缺项，完整声明只可恢复到 PAUSED。manager 与 API status 绑定同一 readiness 对象。管理 readiness 不代表账户/数据真实或激活授权。PAPER app composition、生产 provider 和真实 PAPER 仍未完成；无 Gateway/账户连接。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B103 为 NL2 trusted SMA/RSI provider 报告接入独立 BackBroker 子进程复算：decision log 与 verified bars、manifest、冻结策略版本及成本输入绑定，订单/成交/权益/费用/持仓/指标不一致时不发布 execution report。SMA/RSI、Python upload 兼容与 mismatch 失败路径定向组 26 passed。该证据验证首跑执行会计的一致性，不验证策略信号算法或供应商数据真实性；无行情/Gateway/账户连接。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B101 完成 PAPER SELL 股数预留的离线账本切片：两次风险检查会合并可信 facts 中的外部挂单股数与 SQLite 本地 reservation；最终事务校验份额余量并原子提交唯一发送边界。状态机与相邻 Cerebro/callback 组合回归 95 passed。部分成交、拒单/确认取消、UNKNOWN 重启、旧活动 SELL 冻结和迟到成交均有覆盖。T16 仍为 PARTIAL：生产级可信持仓归属、券商 open-order facts/provider 与跨 ledger/主机容量保证未接入，未连接 Gateway 或账户。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B99 将 active PAPER runtime 的安全停止接入 FastAPI lifespan 关闭：只在 runtime 确认静默、持久状态 STOPPED 且 lease 已释放时清除 manager slot；失败保留状态并记录稳定错误类别。manager/lifespan/live runtime 回归 55 passed。没有启动或连接 Gateway/账户。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B98 将离线构建的 IBKR PAPER runtime 与 managed Python/Cerebro session 接到 `PaperExecutionManager` 可消费的 deployment factory；恢复失败时管理器保留 slot/lease，获授权的 stop 可清理 fake runtime。组合回归 76 passed，退出干净。仍未接入 app root、账户/行情/风险/身份 provider，也没有连接 Gateway 或账户。详见 [PROGRESS.md](PROGRESS.md)、[BLOCKERS.md](BLOCKERS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B97 补充两项可离线推进的验收：真实 Docker worker + Cerebro 在同一共同前缀后分别重开 SQLite journal，再处理不同未来后缀；同时 `/execution` 将 PAPER composition 缺项显示为可读说明，覆盖九种界面语言。T04 仍为 PARTIAL（其他游标/内省/逃逸组合未覆盖）；没有连接 Gateway 或账户。详见 [PROGRESS.md](PROGRESS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B96 为 RSI 增加受限 NL2 intent schema 与参数出处校验：模型抽取的 period/两个阈值必须各有可在原描述中逐字定位、语义匹配的证据，缺项不补默认值；确认仍复用 preview 冻结 spec。相关后端解析/API/真实回测回归 70 passed。模型漏掉其他条件的保证、真实 provider 响应和多轮澄清仍未完成，详见 [PROGRESS.md](PROGRESS.md)。

2026-10-21 B95 完成第二个受信任模板 RSI 的首个离线端到端切片：冻结 Wilder 算法/seed/warmup/阈值边界，加入 `rsi_threshold/1.0` 编译器、真实 Cerebro/BackBroker、provider runner、preview→确认冻结和规则卡。后端专项 58 passed，Execution UI 6 passed，生产 build 成功。RSI 目前接受完整显式阈值句式，不自动补 14/30/70，也不代表行情来源已验证或 PAPER 通过；详见 [PROGRESS.md](PROGRESS.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

2026-10-21 B94 改进 Python 上传失败反馈：隔离 worker 异常只传白名单类别、bar 序号及源码行号；API 和失败状态保存归一化信息，不返回任意异常消息或 stderr。实际 Docker worker 敏感文本探针通过，API/worker/runtime 相关组合 45 passed，Execution UI 5 passed，前端 build 成功。策略错误返回 422 并提示具体 bar/类别/行号；基础设施错误仍用通用 502。该变化不扩展受支持的策略语义。

2026-10-21 B93 增加 T04 未来后缀扰动验收：同一冻结 Python 版本、相同前三根已完成 bar、不同后两根 bar，分别经真实 Docker worker、Cerebro、BackBroker 与 durable event journal 运行；截至第三日的决策、订单和成交完全一致，完整回测终值随后缀变化。端到端用例 1 passed。T04/T39 仍为 PARTIAL：尚未覆盖运行中 journal 重开后的组合扰动矩阵、更多游标攻击及完整容器逃逸面。仅用离线合成 fixture，无 Gateway/账户操作。

2026-10-21 B92 补齐 T03 的 SMA 零差值边界及初始持仓证据：真实 Cerebro/BackBroker 测试验证 <0→0、0→>0、>0→0、0→<0 只在跨过零线时触发；初始快线已高于慢线时不追买，预置多头只在死叉时卖出原持仓数量。SMA 测试 9 passed；交接基线组合 48 passed。T03 记为受信任合成切片 PASS，仍不代表真实行情或 paper 通过。

2026-10-21 B91 收紧 IBKR 恢复快照完整性：读取事实行时重验规范 JSON 与 SHA-256，且 reconciliation proof 绑定完整快照事实摘要；采集后损坏或已对账后篡改都会 fail-closed，allocator 保持关闭。reconciliation + managed runtime 专项 15 passed。没有 Gateway/账户活动。

2026-10-21 B90 收紧终态订单迟到成交处理：若已确认 CANCELLED/REJECTED 的 intent 又收到有效 FILL，账本在同一事务累计成交并将该 broker/account/environment 派单控制闸门置为 RECOVERING，要求重新对账后才可恢复。订单仍保留原终态；账本、callback pump 与新 intent 派单闸门回归共 66 passed。未连接真实 Gateway/账户。

2026-10-21 B89 增加仅降低风险的 PAPER 紧急暂停路由与 `/execution` 控件：认证后从可信 manager 当前 active scope 解析，不接受请求体中的账户、订单、身份或 reason；服务确认 durable PAUSED 后返回不含 scope 的状态。暂停只关闭新增派单，继续券商回报/对账且不撤销既有单。后端 route 8 passed、Execution UI 4 passed、生产 build 成功。共享 key/loopback 仍不可归因，故不能据此开放 activate/cancel/stop 或算作 T32/T43 完成。

2026-10-21 B88 补齐在线 Python execution journal 的恢复一致性校验：读取 checkpoint 时验证严格/规范 JSON、cursor 整数性、前一事件提交状态与身份、result SHA-256 和 state 相等；缺失/篡改会先持久 PAUSED，再拒绝读取，worker 不会收到被篡改状态。定向组 33 passed，含真实 Cerebro/PaperEventBridge 离线闭环。T41/T42 证据增强但仍 PARTIAL；原生 Backtrader 活动 Order/ref 跨进程恢复尚未实现。

2026-10-21 B87 为共享 execution SQLite 增加在线一致性备份、SHA-256/schema/integrity 验证、拒绝覆盖的恢复到新路径和 SQLite 迁移事务包装。WAL 事务进入单文件备份；恢复时持久控制 gate 强制置为 RECOVERING，必须重新 broker reconciliation。专项测试 4 passed。T34 升为 PARTIAL：仍未验证实际部署备份计划、操作员恢复或券商对账；无 Gateway/账户活动。

2026-10-21 B86 为可信管理层新增独立的 owned-order cancel 动作：要求可归因 Principal 的 `cancel` grant，外部只传本地 intent ID；runtime 校验 PAPER scope/lease/持久 order binding，先写 `CANCEL_PENDING` 再调用 SDK IPC，超时后写 UNKNOWN 并拒绝重试，只有 callback `Cancelled` 确认取消。manager/runtime/callback/dispatch 组 54 passed。管理 API 与可归因身份源仍未接入，故不可从 UI 操作；无真实 Gateway/账户活动。

2026-10-21 B85 将 PAPER app 状态从单一 `configured` 扩展为 runtime factory 注册、composition readiness 和缺失能力码；默认及仅注册裸 factory 时仍不能报告策略组合就绪。`/execution` 不再把“factory 已注册但策略/事实源未接齐”显示为普通 STOPPED。新增显式 composition 注册 seam，不启动 runtime；定向后端组 12 passed，compileall、`git diff --check` 通过。此 readiness 是诊断声明，不是授权或真实性证明；没有连接 Gateway、账户或发送订单。

2026-10-21 B84 为 durable backtest journal 加入 checkpoint 恢复一致性校验：当前 state 必须等于上一条 hash 验证后的已提交 event result.state；格式/内容/身份不符立即持久 PAUSED，worker 调用为 0。覆盖新事件、重试和旧事件重放；定向组 11 passed，相关后端子树 353 passed、15 subtests。T41/T42 仍 PARTIAL，在线订单与真实账户恢复未覆盖。

2026-10-21 B83 以真实 Docker worker + SQLite journal 验证有状态策略重启续跑：中断于首个 completed bar 后重开 coordinator，第二事件的 rolling SMA(2) state 和 BUY proposal 与不中断运行一致；重放首事件复用已提交结果。T41 仍 PARTIAL：尚未覆盖 BT native indicator/order-ref、账户和 paper/live 恢复。相关子树 349 passed、569 warnings、15 subtests。

2026-10-21 B82 为独立回放子进程增加 1,000 根 bar/decision、每条决策理由 1,024 字符、请求/stdout 8 MiB、stderr 64 KiB 与运行时限；最小环境变量 allowlist，超限输出会终止进程。定向组 13 passed，相关 execution/Backtrader/PAPER/NL2 子树 348 passed、15 subtests。T44 仍 PARTIAL：子进程没有独立 OS 身份，artifact 也未签名。

2026-10-21 B81 将 Python upload 的第二套 Backtrader/BackBroker 报告复算移入独立本地子进程；OHLCV、Bar records、decision log、资金/费用、manifest 与 instrument 通过规范 JSON 摘要绑定，父进程校验请求/结果摘要、协议和 PID。定向组 10 passed，B79–B81 相关子树 345 passed、15 subtests。T44 仍 PARTIAL：没有 artifact 签名/篡改后落盘重验收，仍可能有共同算法缺陷。

2026-10-21 B80 在真实 Docker worker + importer + Backtrader/Cerebro 路径加入 future-only canary：随机数值只存在于第二根 CSV bar；第一根事件内的调用栈、模块/对象图及 `/proc` 可见面探针没有发现该值，整段 BackBroker 回测通过。T39 仍 PARTIAL，探针有遍历界限且不覆盖完整逃逸矩阵。

2026-10-21 B79 完成 P2 NL2 的受控 SMA 接线和离线回归：非固定句式交由配置 ChatLLM 抽取严格 SMA 意图 JSON，服务端仅编译到受控 `sma_cross` StrategySpec；entry/exit/direction 必须匹配指定枚举。一次性 preview token 绑定原始请求摘要并保存规范 spec，create 复用相同 spec。审阅卡显示原描述、规则和遗漏风险。解析器/API 19 passed；相关 execution/Backtrader/PAPER 子树 340 passed、15 subtests；Execution UI 3 passed、生产 build 成功。T12 仍 PARTIAL：多轮问答、任意意图遗漏检测和真实 provider 验收尚未完成。

2026-10-21 B78 收紧可信数据释放的游标失配处理：合法未来 Bar 时间戳配合跳过当前 seq 会让 durable backtest journal 持久进入 PAUSED；游标不推进、不落事件、worker 调用为 0，之后即使恢复正确 seq 仍必须人工处理。真实 Docker worker + Cerebro 组合攻击和 journal 单测通过。T39 仍 PARTIAL，其他内省/逃逸矩阵与真实 provider provenance 尚未完成。

2026-10-21 B77 将显式滑点比例贯穿两种离线入口：真实 Cerebro/BackBroker 按下一 bar 市价撮合应用百分比滑点，报告声明模型与比例，Python upload 再由独立 BackBroker 重放核对；API preview token、不可变 run config、恢复输入和 `/execution` 审阅卡都绑定同一参数，默认 0%，上限 25%。定向回归验证实际成交价偏移、独立 replay 和更改确认参数拒绝。数据真实性、实际 provider 费率和 paper 运行仍未因此解决。

2026-10-21 B76 收紧离线可信输入边界：Python upload 的 BacktestEventCoordinator 从 importer 验证并绑定的不可变 Bar 序列按 durable cursor 释放单根 Bar，不再接受调用方传入任意 `(seq, Bar)`，并核验当前 Backtrader feed timestamp；SMA/Python runner 把实际 OHLCV 与 manifest 中新增的规范策略输入 digest 对照。同一条真实 Docker worker + journal + Cerebro 组合攻击测试 1 passed；相关后端子树 330 passed、673 warnings、15 subtests passed。T39 仍为 PARTIAL，尚缺多个 host cursor/introspection 变体与完整供应商 provenance；本轮无 Gateway/账户操作。

2026-10-21 B75 为 Python 上传增加只校验的 preview 与明确确认步骤。短期一次性 token 绑定源码、CSV、schedule、metadata、金额/佣金、派生 StrategySpec/manifest 和引擎版本；变更请求会被拒绝且不创建 run/version。`/execution` 现展示文件/清单 hash、范围与声明来源的未认证边界。API 定向 9 passed，后端相关子树 325 passed、657 warnings、15 subtests passed；Execution UI 3 passed、生产 build 退出码 0。此项只完成离线上传审阅体验，不是 NL2，也不代表真实行情、隔离准入或 paper。

2026-10-21 B74 将固定版本 XNYS session 校验接入 PAPER completed-bar feed 发布边界：周末/节假日 bar、纽约时区不匹配及不精确对应排定开收盘的日线 bar 均 fail-closed，半日市使用交易所当日收盘；managed session 同时核对冻结策略 bar policy 与 feed instrument/calendar/version。相关执行/Backtrader/API 子树 325 passed、655 warnings、15 subtests passed。该闸门只证明时间边界符合项目日历，尚未证明供应商行情授权、数据完整性/正确性或 point-in-time 可用性。未连接真实 Gateway 或账户。

2026-10-21 B73 新增 `ManagedPythonPaperSession`，把 frozen Python upload、受信任 completed-bar feeds/conId 映射、B72 fresh PAPER snapshot hydration、真实 Cerebro/PortBroker、managed callback worker、Python event bridge、逐单风险/账本和 SDK subprocess PAPER transport 组成单一离线可测试 session。session 实现 `PaperExecutionManager` runtime protocol，离线验证由可归因 principal 经 manager 激活/停止；启动严格落在 PAUSED，只有显式 activate 才接收 bars；未恢复 native BT order/ref、活动单或 pending callback/outbox 时拒绝 attach；停止先暂停、确认无队列/在途工作，再结束 feed/worker/runtime。新增完整假 IBKR 子进程 BUY→SEND_STARTED→execution callback→native Backtrader fill 闭环。相关 execution/Backtrader/API 子树 323 passed、599 warnings、15 subtests passed。仍未由真实 app manager/FastAPI runtime factory 构造，也没有可信生产行情/风险事实配置；未连接真实 Gateway 或账户。

2026-10-21 B72 在 managed runtime 中增加 latest reconciled snapshot handoff：必须仍处于该轮恢复后的 PAUSED、租约持有、SDK 健康且 durable reconciliation proof/account/PAPER scope 完整匹配；默认仅在完成后 10 秒内（可配 1–300 秒）可供可信 PortBroker hydration 消费。成功 activate 后旧 snapshot 立即失效，单纯 pause 不会让旧账户快照重新变为新鲜。该 TTL 不解决外部账户变更竞态。未连接真实 Gateway 或账户。

2026-10-21 B71 修复 managed callback worker 与 Cerebro 回调队列之间的 deferred-ACK 重投：一个进程内已交付但未 ACK 的 callback 不会再次入队，consumer ACK 会持久提交；进程重启后仍重放未确认 inbox，内容一致的重复 event ID 会补做 ACK。worker 构造时注册唯一 consumer role，dispatch 自动禁止 Cerebro 再消费。专项回归 50 passed、8 warnings。该 replay 只证明 pump→sink，没有恢复 Backtrader native order/ref；manager 尚未把 runtime worker、hydrated Cerebro、策略/feed 和逐单授权/事实源组装成同一 PAPER session。未连接 Gateway、未读取账户或发送订单。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md) 与 [DECISIONS.md](DECISIONS.md)。

2026-10-21 B70 新增受认证只读 `GET /api/execution/paper/status`，并让 `/execution` 页面显示真实 manager status；不暴露账户/余额/持仓/委托，不连接或启动券商。Execution UI 定向测试 3 passed，生产 build 成功；后端 execution/Backtrader/API 子树 315 passed、589 warnings、15 subtests passed。默认 manager 没有 runtime factory，状态为 NOT_CONFIGURED。B69 显式 PAPER factory 可零连接构造同库 recovery runtime，但尚未绑定 app manager；缺经验证的 PAPER profile/account、risk/account/market facts provider、可归因控制身份，也没有把 Python strategy/Cerebro/feed 接入该 runtime。没有启动进程或连接 Gateway。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md) 与 [BLOCKERS.md](BLOCKERS.md)。

2026-10-21 新增 Python paper event bridge 离线组合切片：可信 host 在 Cerebro 当前 `Strategy.next()` 内校验 completed Bar、paper lifecycle/owner lease、run scope、旧 outbox 和 scope ledger 恢复队列；一个事件最多 handoff 一个 proposal，并通过 one-shot resolver 把匹配 intent 交给 native BT order。B51 将真实 Cerebro bridge 接到 `BacktraderDispatchPort`、两阶段 PAPER 风控、SQLite ledger、durable callback pump 和本地 fake transport，覆盖从 proposal 到 native fill 的完整离线组合。B52 增加仅针对已绑定 PAPER 活动订单的持久化撤单尝试；先提交 `CANCEL_PENDING`，再以精确 broker order ID 调用官方 SDK，回报确认撤单，结果不确定时冻结且禁止重试。官方 execDetails adapter 也补齐 `secType`。B53 修正 order ID readiness：nextValidId 只提供候选值，必须由可信宿主在 open-order 与本地 binding reconciliation 完成后显式确认才能分配。B54 将 hostile future-series/index probe 放入真实 Docker worker→Cerebro 上传回测，验证 event 只有当前 completed bar；T39 现为 PARTIAL。B55 新增单槽 PAPER lifecycle manager，校验单一规范 SQLite、恢复失败保留 lease、归因授权和 quiescent stop；B57 在独立 SDK runtime 增加显式连接/专用线程/有限停止 primitive。B58 将父进程 supervisor 放入主应用 execution 模块，父进程使用独立 SDK venv 启动断连子进程，并经版本化、有界私有 stdio 协议管理；spool 有序读取/ACK 接口也已落地，但业务 callback pump 尚未由 app runtime 驱动。B52 相关后端子树 243 passed、563 warnings，B57 runtime 28 tests、B58 SDK runtime 30 tests + host supervisor 2 tests，host callback pump 35 tests，B54a Docker 端到端 1 passed，B55 manager/lifecycle 组 18 passed，B56 后端子树 249 passed、563 warnings。SDK process seam 尚未接到应用 manager，也没有账户 facts provider、callback recovery；没有连接 Gateway、读取账户或发送订单。

2026-10-21 P3a 已新增离线 Backtrader dispatch composition：真实 Cerebro native order 经过冻结 intent 字段核对、两阶段 PAPER 风控与 SQLite 账本；`SEND_STARTED`、attempt 和完整 broker-ID→intent→native-ref 映射在同一事务提交后，transport 才能发送。execution 回报经 durable inbox 更新 native order，Backtrader 成功消费后才 ACK。另持久化并校验合约 conId 与身份字段，旧的不完整 mapping fail-closed。`PaperExecutionService.close()` 在停止未静默时保留 STOPPING 和 owner lease。完整 bridge→dispatch→fake transport→callback→native fill 组合已由 B51 验收。续做新增官方 `ibapi 10.50.2` 的 SMART USD STK Contract、Market/DAY Order 构造器、原生命令与 intent 一致性 transport、由 `nextValidId` 驱动的持久 order-ID 分配器与本地进程 owner 锁。B43 持久控制闸门定向组 23 passed；B44 相关后端子树 216 passed。B45 验证版本化单事件 stdio 协议及伪造响应拒绝（27 passed）；B46 在真实 Docker 中验证强杀宿主 supervisor 后 worker 按容器内 deadline 退出并自动清理（17 passed）；B47 相关后端子树 224 passed。新增 paper-only `PaperExecutionService` 生命周期原语：恢复后默认 PAUSED，显式 readiness 检查通过才激活；B48 定向组 33 passed，B49 相关后端子树 230 passed、563 warnings；原交接基线 46 passed、148 warnings。B51 定向组 43 passed，相关后端子树 241 passed、563 warnings。整体切片仍不连接 Gateway、不读取账户或发送 paper/live 订单。应用级 runtime、管理 API、管理通道认证、跨机器账户 owner、真实账户/报价事实和完整恢复服务仍未完成。详见 [PROGRESS.md](PROGRESS.md)、[ACCEPTANCE.md](ACCEPTANCE.md) 与 [BLOCKERS.md](BLOCKERS.md)。

版本：设计稿 1.2；日期：2026-10-09。实施状态：P1 受信任 SMA 与 Python upload 隔离事件离线回测、严格 CSV/session schedule 导入、独立 Backtrader backend/API 报告分流、不可变版本/事件日志原语、risk 纯函数、Docker worker、可创建并保存用户回测的 `/execution` 页面，以及 P3a 的离线 SQLite 订单账本、`BrokerBase` 代理、官方 IB API 回调/发送闸口和账本回报泵切片已落地。2026-10-20 Python upload 首次通过真实 Docker worker→Cerebro/native Order/BackBroker 报告闭环及 importer→artifact service seam，新增独立认证 `POST /runs/backtests/python-upload` API 和离线上传表单；合成 future-bar perturbation 检查通过。随后增加并通过 B25 proposal→risk→journal 冻结→ledger intent handoff、B26/B27 报告复算与 B28 离线 journal/resume。B29 新增单笔 PAPER BUY dispatch 状态机，在 reservation 前后复查风险、持久化 `SEND_STARTED` 后才调用注入的 sender，并对不确定发送冻结 `UNKNOWN`；负费用/负价格缓冲输入被拒绝。B30 固定 `exchange-calendars==4.13.2` 的 XNYS 会话表，并使 SMA provider 路径经过严格 completed-bar CSV importer；按含首尾日期检查完整请求区间，拒绝首尾/中间缺失交易日和非午夜日线标签。B32 增加只读策略预览、规则卡和显式确认；hash 将确认输入绑定到实际回测请求，预览本身不创建 run 或版本。共享 SQLite ledger 会在同一写事务中汇总同账户/策略的本地 reservation 并复查买单额度；SELL 在股数 reservation 未实现前被 fail-closed 拒绝。这些仍是离线切片：不证明行情供应商/交易日历的生产授权，不是 Gateway 集成、跨数据库 reservation 保证或账户 owner 管理。有限的固定句式预览不等于 AI NL2 缺项澄清；grant store、权威账户/报价 provider、deployment owner、完整 IBKR Broker composition、自动策略生成与完整 paper 回报/恢复链路仍未完成。验收明细和边界见 [ACCEPTANCE.md](ACCEPTANCE.md)、[PROGRESS.md](PROGRESS.md) 与 [BLOCKERS.md](BLOCKERS.md)。

## 结论与仓库选择

主开发仓库为 `vibe-trading`。复用其 React/FastAPI、AI 对话、研究与报告能力，新增独立运行的 Backtrader 后端。Backtrader 是固定版本依赖；其行情 Feed 和实盘 Broker 扩展先写在 Vibe 仓库的运行包里，通过公开扩展接口接入。只有证明引擎本身存在无法通过扩展解决的问题，才另建最小 Backtrader 补丁。

必须真实使用 Backtrader：历史回测经 `Cerebro + Strategy + BackBroker`；IBKR 模拟盘和实盘经 `Cerebro + 同一策略 + 自定义 BrokerBase + 官方 ibapi`。不允许回测走 Vibe 原引擎却标注 Backtrader，也不允许用 Backtrader 算信号后绕过其订单对象和回报链直接下单。

本方案包含本总览及以下六份文档：

| 文件 | 读者与用途 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 用户、技术负责人：产品边界、架构决策、自动交易与扩展方式 |
| [CONTRACTS.md](CONTRACTS.md) | 实现 agent：数据、策略、Broker、事件、API、状态与恢复契约 |
| [DELIVERY_PLAN.md](DELIVERY_PLAN.md) | 执行/评审 agent：阶段、依赖、文件归属、验收和异常场景 |
| [EVIDENCE.md](EVIDENCE.md) | 审阅者：本地实际代码、环境、未提交改动和外部资料 |
| [AGENT_START.md](AGENT_START.md) | 首个执行 agent：可直接粘贴的开工任务书 |
| [REVIEW.md](REVIEW.md) | 双入口复审：七项问题、修正与开工前的五项验证 |

实施按 DELIVERY_PLAN 的阶段推进；公共接口先由集成负责人冻结，再按工作包拆分。当前 P0 离线探针及基线详见 [PROGRESS.md](PROGRESS.md)。

## 用户已明确的要求

1. Vibe 负责自然语言交互与策略生成，用户无需写代码。
2. 用户也可上传基于我们 Python Backtrader 模板编写的策略。
3. 使用 Backtrader 的回测引擎、模拟盘和实盘策略运行能力。
4. 首期美股 + IBKR；为 IBKR 其他国家/资产、A 股 QMT、加密货币预留扩展接口。
5. 授权范围内自动交易，订单、持仓及异常及时推送；预设审批、风控、停止机制。
6. 从简单策略和模拟盘开始；首期单用户，不做多租户平台。

## 设计默认值：建议而非用户已确认的交易参数

- 首期只支持普通美国股票/非杠杆 ETF、USD、整股、只做多、常规交易时段、日线策略。
- 先做 SMA 双均线交叉；第二个模板为 RSI 阈值策略。每个策略只交易一个明确合约。
- 同一个 `broker + account + environment` 首期只允许一个主动运行的策略，避免跨策略资金分配和持仓归属未定义。
- 风控金额、亏损额度、通知渠道没有用户给定值，不能替用户授权。离线示例用明显标记的合成参数；真实模拟账户也必须配置具体授权。
- 日线信号在完整 bar 可用后决定，下一有效常规交易时段派单；不承诺成交于官方开盘价。详情见 CONTRACTS 的时间和数量语义。
- 自动交易运行固定策略版本；AI 生成修改建议不自动替换正在交易的版本。
- 两种策略入口：自然语言生成受控模板，或上传符合 Backtrader 模板的 Python。两者共用同一 BT 回测/交易运行、版本、风控、账本和报告。用户 Python 只有在操作系统隔离、依赖限制和订单授权测试通过后才准许 paper 部署。
- 单机模块化应用 + 独立 worker + SQLite；无需先引入 Redis、Kafka、Kubernetes。

以上默认均可在实施前变更，但影响策略或执行语义的变更必须形成新版本和重新验收。

## 本次评审纠正的关键事实

- 本地 Backtrader 已有基于官方 `ibapi` 的只读历史/实时行情 Feed。已安装 `ibapi 10.50.2`，旧 IbPy 未安装。不能再把本地情况概括成“只有旧 IbPy 可用”。
- 官方 Python API 安装采用独立 `ibkr-runtime/` 锁文件，避免 IBKR 固定的 `protobuf==5.29.5` 与主项目 `channels` extra 的 protobuf 要求冲突。安装器仅接受匹配官方 release 1050.02 的 `setup.py + ibapi/**` 树哈希；它不声明上游下载压缩包有已验证的 archive hash。流程见 [IBKR runtime 安装说明](../../ibkr-runtime/README.md) 与 [源码树锁](ibapi-source.lock.json)。
- 这些新增文件未提交，不能假设新 worktree 自带。现有 Feed 与 what-if 工具不等于已实现可交易 Broker。
- `IBKR_SETUP.md` 记载的环境是 **live Gateway 4001、Read-Only**，不是待测 paper 环境。本次未连接账户，也未确认该记载仍是实时状态。执行 agent 不得把这个端口直接复用为模拟盘。
- Vibe 已有实盘授权/停止/对账模块，但既有 LiveRunner 是调度 AI agent 继续对话/工具工作的运行器，不是按行情事件执行固定策略的 Backtrader worker；交易 worker 需要独立实现。
- Vibe 会话 SSE 使用内存缓冲，满队列可能丢消息。它可以作为 UI 推送通道，不能成为交易账本。

## 首期完成的含义

用户能在 UI 中描述一个受支持策略，审阅具体规则，取得真实 Backtrader 回测报告，授权该不可变版本运行于明确的 IBKR paper 账户，并看到券商回报驱动的订单/持仓、风控阻断和恢复状态。当前 `/execution` 支持 SMA 的固定/受控模型抽取句式，以及 RSI 的受限 JSON intent 抽取；RSI period/阈值需由原文数值证据支持，确认卡展示原描述和实际模板参数。RSI 不会猜测默认值。另有独立 Python upload 表单，要求源文件、completed-bar CSV、独立 session schedule 和调用方声明的 metadata，使用隔离 worker 离线回测，单次最多 1,000 根 bar。B160/B166 已实现受控多轮 NL2 澄清和本地 durable transcript；任意自然语言缺项检测、服务端 actor/tenant 隔离、真实数据来源核验、app-managed 在线风险 Broker、恢复及 paper 链路仍未完成。

Python 上传入口也须有独立的回测、状态恢复及 paper 证据。隔离或恢复等门槛尚未通过时可以先交付只读研究能力，但整体双入口目标标为部分完成。策略进程只提出受限订单，可信执行服务独占账户权限、风控与账本；二者职责不得混用。

离线用例通过、券商模拟盘验收通过、真实账户准入，是三个分别记录的状态。模拟盘或实盘填报不存在、权限缺失、市场休市导致验收未完成时，报告实际阻塞，不能以 mock 或历史测试替代。

实施按 P0→P5 推进。当前契约实现、实验结果与未完成事项见 [PROGRESS.md](PROGRESS.md)、[DECISIONS.md](DECISIONS.md)、[BASELINE.md](BASELINE.md) 和 [BLOCKERS.md](BLOCKERS.md)。SMA 与 RSI 的 provider 回测创建/保存/读取都使用受信任模板 compiler 和真实 Cerebro/BackBroker；Python upload 已贯通离线 UI、独立认证回测 API、artifact service 与 durable offline replay/resume。B160 为缺项策略提供受控多轮补充，B166 增加服务端 append-only transcript、turn UUID 幂等重放、父 revision 与回测上下文绑定；T12 仍 PARTIAL，因为 actor/tenant、留存/删除、任意自然语言遗漏和真实 provider 准确性尚未验收。B115 让 PAPER status 与 manager 实际启动 readiness 一致，但未接生产组合；B116 记录未确认费用事实；B117 错误游标持久暂停但仍不足以通过完整 T39 攻击矩阵；B118 补成功迁移后的备份恢复正向测试，但不构成部署级恢复验收。账户/报价事实源、管理 API 和真实 paper 验收仍未完成。本轮未连接 Gateway、未发送订单，也未更改 Gateway 设置。
