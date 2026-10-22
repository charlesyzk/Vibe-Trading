# 2026-10-22 持续目标续做基线（B192 后）

## B187–B192 当前追加证据

| 项目 | 当前证据 |
|---|---|
| 改动 | B187 真实 SQLite 中途 abort 覆盖事务回滚/安全重交；B188–B189 双入口 replay 严格约束订单 schema；B190 独立重算 trusted NL2 sizing metadata；B191 修正双入口报告身份展示；B192 将恢复测试对齐到 durable unACKed inbox/STOPPING 语义。 |
| 验收 | `test_event_journal.py` **24 passed**；B187/B188 合并组 **709 passed、2239 warnings、19 subtests**；B190 report/replay 定向 **34 passed**、SMA/RSI/accounting/upload/replay/routing 核心组 **54 passed**、最新 missing-field route **19 passed**；B191 Execution UI **14 passed**、production build 成功；B192 execution/Backtrader/API/PAPER 合并组 **715 passed、2525 warnings、19 subtests、98.57 秒**，recovery matrix **6 passed**。官方 `ibkr-runtime/.venv` 为 ibapi 10.50.2，`unittest discover` **46 tests OK**。 |
| 边界 | SQLite trigger 不等于 arbitrary process/OS/storage crash；信号计算与 coordinated order/decision tampering、共同算法错误、同机 artifact 同主体篡改风险仍在；无真实 Gateway/account/order 操作。 |

## B184–B186 本轮执行证据

| 项目 | 当前证据 |
|---|---|
| 改动 | PAPER app composition 与 start route 强制冻结 strategy SHA-256 pin；Docker sandbox preflight 检查 builtin/default seccomp，并在容器显式设 `seccomp=builtin`；daily importer 将 session 开/收盘换算到 metadata timezone 后绑定 session_date；策略线程失败时 callback worker 改投 durable no-op sink，继续 ledger 入账、保留未 ACK 回调供显式恢复。 |
| 验收 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：**708 passed、2251 warnings、19 subtests、67.36 秒**。Docker seccomp 目标 1 passed；market importer 14 passed；market contracts+routing 21 passed；策略 timeout/callback/cancel 1 passed，邻接故障组合 2 passed；版本 pin 定向 59 passed。修改文件 py_compile 与 tracked `git diff --check` 通过。 |
| 边界 | warnings 未逐项消除。`git status --short` 保持 53 条路径；所有变更仍为用户未提交工作，不做提交/清理。没有真实行情访问、Gateway/account/订单操作。seccomp 证据限当前 Docker daemon，calendar 使用声明/fixture，callback 的 Backtrader native notification 要显式恢复；供应商真实性、生产 root/providers 和真实 PAPER 仍未完成。 |

## 本次 goal 续做起点复核（2026-10-22）

| 项目 | 当前证据 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；未提交。 |
| dirty state | `git status --short` 有 53 个已修改/新增路径；含 provider/chat/settings/Electron 等既存工作及 Backtrader/execution/API/frontend/SDK/docs 实现。仅在明确目标文件内续做，不 reset/stash/清理/整体暂存。 |
| 依赖 | Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、pandas 2.3.3、yfinance 1.7.0、exchange-calendars 4.13.2、FastAPI 0.142.2、Pydantic 2.13.5、uv 0.11.21；`uv lock --check` 退出码 0。官方 ibapi 10.50.2 在独立 `ibkr-runtime/.venv`，模块位于该 venv `site-packages/ibapi`；主 `.venv` 未装 SDK。 |
| 起始测试 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**48 passed、210 warnings、0.54 秒**。早期 46 passed/148 warnings 为历史值；保留后续测试与 warning 差异。 |
| 其他环境 | `.venv/bin/ruff` 不存在；本轮没有 Gateway、账户、行情外网或真实订单操作。 |

## B182–B183 精确 YFinance 来源路径与 Python-upload PAPER 离线 composition

| 项目 | 当前证据 |
|---|---|
| 改动 | YFinance trusted snapshot 直接调用 concrete loader、拒绝 registry fallback，冻结 host-observed provenance 与分层 hash；Python upload 新增 version-pinned session builder/composition factory，校验 sandbox、策略 policy、USD STK/feed、共享 runtime DB 并复用 managed session。 |
| 验收 | adapter/market/API 定向 62 passed、1349 warnings；composition/deployment/PAPER route 定向 42 passed、4 warnings；execution/Backtrader/API/PAPER 完整相关组 687 passed、2242 warnings、19 subtests、64.78 秒；`py_compile` 与 `git diff --check` 退出码 0。 |
| 边界 | provider authenticity、license、raw HTTP response、PIT、adjustment 和 contract mapping 仍 UNVERIFIED；Python upload factory 还未安装到 app root。只验证离线 construction，未启动 SDK、获取 lease、连接 Gateway 或操作账户。T07/T08/T14/T40/T41/T42 仍 PARTIAL。 |

## B179–B181 NL2 frozen version identity、market snapshot evidence 与恢复 gate

| 项目 | 当前证据 |
|---|---|
| 改动 | NL2 strategy version 使用 `backtrader/{version}`；DataManifest v1.1 显式记录 currency 且保留 v1.0 读取；snapshot 复核日期范围/窗口、available_at、currency/source/adjustment/final；journal resume 在共享 DB 检查 execution gate 为 RUNNING。 |
| 验收 | routing + contracts/market snapshot + importer + SMA/Python replay + backup/event journal 合并 **105 passed、548 warnings、7.42 秒**；完整 execution/Backtrader/API/PAPER 子树 **673 passed、2240 warnings、19 subtests、65.19 秒**；同一 NL2 report version 通过 `_validate_frozen_nl2_version()`。 |
| 边界 | provider 声明仍非认证事实；execution gate 不替代 lease/grant/reconciliation；真实数据、真实 paper、SELL/UNKNOWN 崩溃恢复尚未完成。 |

## B178 备份恢复暂停 journal run 并要求 exact event 恢复

| 项目 | 当前证据 |
|---|---|
| 改动 | Restore 的同一 SQLite 事务把 execution gate 设为 RECOVERING，并把 journal 中 RUNNING run 全部设为 PAUSED；不支持的 execution_run schema 会回滚失败。 |
| 验收 | SQLite backup + event journal 专项 30 passed。恢复后普通 identity/stage/release 被拒；显式 exact released-event recovery 才允许续跑。 |
| 边界 | 异机备份、运维恢复演练和真实 broker reconciliation 仍缺，T34/T42 PARTIAL。 |

## B177 Python upload preview/confirm 经真实 Docker worker 的 API 闭环

| 项目 | 当前证据 |
|---|---|
| 改动 | 新增 Docker integration API 用例，固定 worker 镜像 ID 后经正式 preview/confirm endpoints 执行真实当前 bar worker、真实 Cerebro/BackBroker 和独立 replay，最终由 run detail 验证 sidecar。 |
| 验收 | 单项 Docker API 1 passed、17 warnings、4.53 秒；与 market data、dispatch、Backtest API 合并 56 passed、1258 warnings、8.39 秒；Execution UI 13 passed、Vite production build 退出码 0，py_compile/diff check 退出码 0。 |
| 边界 | 使用确定性 fixture bars/calendar，不是真实供应商、账户或 broker；UI test 使用 API mock，真实 API/runner 证据来自独立 Docker integration。 |


## B174–B176 数据时间、USD contract 与 NL2 API/runner 接合

| 项目 | 当前证据 |
|---|---|
| 改动 | Importer 要求 fetched_at 不早于所有 bar 的 available_at；native dispatch 要求解析出的 IBKR contract 为 exact USD STK；NL2 API preview/confirm/detail 增加不 mock runner 的真实 Cerebro/BackBroker 闭环。 |
| 验收 | importer+dispatch 首轮 24 passed；真实 API→runner 新用例 1 passed；最终含完整 Backtest API 文件 55 passed、1242 warnings、6.40 秒，py_compile/diff check 退出码 0。最终合并命令见 `ACCEPTANCE.md` B174–B176。 |
| 边界 | 获取时间仅 self-consistency，不是可信 provider attestation；API 使用确定性 fixture。USD exact gate 不代表已验证合约来源/IBKR account。 |


## B173 trusted-template report/replay evaluation window 对齐

| 项目 | 当前证据 |
|---|---|
| 改动 | Trusted SMA 的 evaluation window/count 写入报告；年化收益只统计 manifest evaluation bars；runner 将同一 evaluation_start 传给独立 BackBroker replay，verifier 检查窗口和 bar count。 |
| 验收 | `test_sma_backtest.py`、accounting、replay 和 backend routing 定向组 30 passed；与 importer/upload/API/Docker worker 组合 92 passed（命令重复列了一次 routing 文件，包含重复用例）。 |
| 边界 | 保留 warmup 全轨迹 equity curve；没有 provider authenticity、point-in-time 或真实 PAPER/Gateway 证据。T07/T09/T38 仍 PARTIAL。 |


## B172 explicit quantity 的 BackBroker 现金不足负向验收

| 项目 | 当前证据 |
|---|---|
| 改动 | 只增加验收，不改数量实现：uploaded strategy 原始请求 200 股面对 $1,000 现金，BackBroker 保留数量并返回 Margin，零成交。 |
| 验收 | Python upload/SMA/position sizing 相关文件组 41 passed；与 B171、service/API/backend routing 和真实 Docker worker 共 **75 passed、1115 warnings、39.14 秒**。独立 BackBroker report replay=`verified`。 |
| 边界 | BackBroker 离线 margin 不证明 IBKR account buying power、订单拒绝 callback 或 PAPER dispatch 风控。 |

## B171 通用 importer 校验 manifest 窗口边界

| 项目 | 当前证据 |
|---|---|
| 改动 | 导入前将 warmup_start/evaluation_end 与实际 bars 首末交易日精确绑定，并要求两个内部边界也是存在的交易日。 |
| 验收 | 专项 `test_market_data.py` 11 passed、77 warnings、1.68 秒；与 importer/upload/API/真实 Docker worker 整组 75 passed。 |
| 边界 | 仅验证日历与窗口完整性；不认证行情 provider、许可、历史 point-in-time、复权或 corporate action。 |

## B170 managed session startup fail-closed

| 项目 | 当前证据 |
|---|---|
| 改动 | `start_paused` 启动全过程统一异常边界；持久暂停 run，execution gate 在持有 owner lease 时维持/回到 PAUSED。 |
| 验收 | 晚期注入测试 1 passed；execution + bt_runtime + backtest API + PAPER routes 658 passed、1692 warnings、19 subtests、62.36 秒。 |
| 边界 | 持久 PAUSE best-effort；本地 journal/ledger 写坏不保证状态可落盘。失败 session 不能直接 activate，须重新构造并 fresh reconcile。真实 Gateway recovery 与 SELL 仍未验收，T41/T42 PARTIAL。 |

## B169 per-fill Backtrader native order recovery

| 项目 | 当前证据 |
|---|---|
| 改动 | ReconciledOrderState 携带逐笔 fills；严格校验 execution ID/时间顺序/累计数量/加权价/最后时间与 position cost basis；逐笔恢复 native executed bits。 |
| 验收 | execution + bt_runtime + backtest API + PAPER routes：657 passed、1696 warnings、19 subtests、61.71 秒。管理会话恢复用两笔不同价格/时间 fill，重复旧 exec 不重复记账，后续 exec 完成原 ref 且不 resend。 |
| 边界 | BUY 限定恢复，未确认费用不伪造 per-fill 分布；没有真实 Gateway/账户故障演练。T41/T42 PARTIAL。 |

## B168 Python upload warmup/evaluation separation

| 项目 | 当前证据 |
|---|---|
| 改动 | 校验 manifest 日期范围与 bars 覆盖；warmup 仍输入 worker 以建状态，但 proposals 被 host 忽略；只有 evaluation 区间提交真实 BackBroker 订单。报告记录 window/bar/proposal counts，年化 daily periods 使用 evaluation bar count；独立 replay 绑定同一 evaluation_start。 |
| 验收 | Backtrader runtime + backtest API 123 passed、1209 warnings、9.69 秒；evaluation/warmup 两个关键用例 2 passed；`py_compile`、`git diff --check` 通过。 |
| 边界 | equity curve 保留全 feed 轨迹（warmup 无交易）；无真实 provider provenance/许可或 PAPER 证据。NL2 template evaluation 语义未纳入此 slice。 |

## B167 coordinator bar contract defense-in-depth

| 项目 | 当前证据 |
|---|---|
| 改动 | coordinator 在新 bar stage 前、durable released-event 执行前和 shadow replay 中按 frozen StrategySpec 重验 instrument、USD、时区、XNYS 精确交易日与 UTC 开收盘、可信 clock；失败持久 PAUSE 且 worker=0。两类 PAPER session 复用 feed clock。 |
| 验收 | `agent/tests/execution agent/tests/bt_runtime`：597 passed、1016 warnings、19 subtests、57.99 秒；两项真实 Docker worker 集成：2 passed、17 warnings、8.01 秒。 |
| 范围边界 | pinned calendar/合成数据不证明 provider 来源、许可、point-in-time、修订或公司行动；T07/T09/T39 仍 PARTIAL。没有 Gateway/account/order 操作。 |

## B166 NL2 transcript provenance

| 项目 | 当前证据 |
|---|---|
| 改动 | Trusted-template preview 写入 append-only SQLite turn 链；父 turn、精确描述追加、上下文 hash 与 stable turn ID 都由服务端校验。UI 在失败重试时复用相同 turn UUID。 |
| 定向验收 | Transcript + backtest API 36 passed、656 warnings、5.40 秒；Execution UI 13 passed；`npm run build`、`git diff --check` 通过。 |
| 隐私/语义边界 | DB 文件 0600；未绑定 actor/tenant、无 retention/删除、多用户隔离未测；任意 NL 遗漏和真实 provider 仍未验收，T12 PARTIAL。 |
| 外部活动 | 仅离线 API 与本地 SQLite 测试；未连接 Gateway、未读账户、未发真实订单。 |

## B165 收紧 CREATED retry 的可推进终态

| 项目 | 当前证据 |
|---|---|
| 改动 | 只有 durable FILLED 可释放原 retry bar；CANCELLED/REJECTED 要求 fresh reconciliation。活动订单与 CREATED intent 同场恢复仍拒绝。 |
| 定向验收 | fill/cancel/reject × 风险通过/拒绝 6 passed、43 warnings；runner `LIVE_THREADS []`。 |
| 外部活动 | 本地 SQLite/Cerebro 与 fake transport；未连接 Gateway/account/真实订单。T41/T42 PARTIAL。 |

## B164 CREATED intent 与 released retry bar 联合恢复

| 项目 | 当前证据 |
|---|---|
| 改动 | 只允许一个 exact-linked CREATED BUY 与同 run 的一条 released retry bar 并存；fresh reconciliation/shadow replay 后仍 PAUSED。显式 activate 先重风控旧 intent，只有 FILLED broker callback 到达后才恢复原 event；CANCELLED/REJECTED 持久暂停并要求 fresh reconciliation。 |
| 定向验收 | 风险允许/拒绝 2 passed；与 B143/B162 关键用例 11 passed；session + broker proxy + dispatch 54 passed；execution/Backtrader/API/PAPER 合并组 634 passed、1665 warnings、19 subtests。pytest runner `LIVE_THREADS []`；py_compile/diff check 通过。 |
| 外部活动 | 本地 SQLite、真实 Cerebro 与 fake PAPER transport；未连接 Gateway/account/真实行情服务，未发真实订单。T41/T42 PARTIAL。 |
| 仍缺 | 多 CREATED、SELL、多事件队列、未知/未决 send、其他 active order 共存、Docker/host crash 与真实 Gateway 账户/order 对账。 |

## B163 唯一已链接 CREATED intent 的安全停止

| 项目 | 当前证据 |
|---|---|
| 改动 | stop 先关 send gate、持久 pause run，再允许唯一 B162 精确链接的 CREATED BUY 保持原样并结束 session/runtime。未创建 broker order；没有 cancel、删除或自动重发。其他活动/未决状态仍阻止 stop。 |
| 定向验收 | 此前在进程内 pytest runner 中覆盖初始 journal PAUSED/RUNNING、二次风险允许/拒绝、停止/激活时序共 8 passed，并检查指定 callback/Cerebro 线程已退出。本轮未重跑；之后独立 pytest 子进程未正常退出，不计为新验收。 |
| 外部活动 | 本地 SQLite 与 fake PAPER transport；未连接 Gateway、未读取账户、未发送真实订单。T41/T42 继续 PARTIAL。 |
| 仍缺 | B164 之后仍有其他 CREATED/retry 组合、其他订单状态/native ref 跨重启，以及真实 Gateway reconciliation。 |

## B162 精确 CREATED intent 恢复

| 项目 | 当前证据 |
|---|---|
| 改动 | 在 B159 outbox handoff 后，managed session 对唯一 committed-proposal-linked CREATED BUY 暂存一个临时 PortBroker 队列。PAUSED 不建 order；显式 activate 后 Cerebro thread 以新 native ref 经既有 resolver、二次风险检查与 trusted transport。风险拒绝使 session/scope 暂停且不发送。 |
| 定向验收 | managed session + PortBroker + BacktraderDispatch：48 passed、327 warnings、10.26 秒；覆盖 RUNNING/PAUSED journal、成功与二次风险拒绝。 |
| 组合验收 | execution + Backtrader runtime + backtest API + PAPER routes：628 passed、4597 warnings、19 subtests、95.41 秒；首次仅一个 sandbox timeout，孤立复跑通过后整组复跑通过。py_compile/diff check 通过。 |
| 外部活动 | 本机 SQLite、fake managed child 与 fake PAPER transport；未连 Gateway、未读账户或发送真实订单。T41/T42 保持 PARTIAL。 |
| 仍缺 | Docker worker crash 后完整 managed recovery 演练、多 CREATED、SELL、UNKNOWN/READY_TO_SEND、临时 native ref 跨重启恢复，以及真实 Gateway broker/account reconciliation。 |

## B159 committed outbox→ledger handoff 恢复

| 项目 | 当前证据 |
|---|---|
| 改动 | managed session 在 fresh reconciliation 与 shadow replay 后，可对 RUNNING/PAUSED 同 run 补完 PENDING/RESERVED handoff。prepared intent/decision 保存 hash，恢复时对比冻结 event proposal/run identity；失败持久 pause。B159 原阶段仅 create/link；B162 已补唯一精确链接 CREATED BUY 的受限暂存与显式激活派单。 |
| 定向验收 | managed-session/journal/outbox：64 passed、197 warnings；包含两种 journal 起始状态、hash 与 run identity 负例。 |
| 组合验收 | `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：615 passed、1619 warnings、19 subtests、58.73 秒。 |
| Docker 验收 | 真实 Docker worker committed-prefix shadow replay：1 passed、2.69 秒；输出一致，journal/cursor/outbox 不写，dispatch=0。 |
| 外部活动 | managed-session 使用 fake runtime/authorizer 和本地 SQLite；未连接 Gateway/account/真实行情服务，未发送订单。T41/T42 保持 PARTIAL。 |

## B158 committed-prefix shadow replay

| 项目 | 当前证据 |
|---|---|
| 改动 | SQLiteEventJournal 持久化 initial checkpoint 并读取/校验完整 committed prefix；coordinator 对冻结 Python worker 输出只读重算；managed session 在 fresh reconciliation 后、Cerebro 启动及 failed-session 重建前调用。 |
| 定向验收 | event journal / event worker / managed session：58 passed、205 warnings、6.68 秒。 |
| 组合验收 | `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：610 passed、1615 warnings、19 subtests、56.06 秒。 |
| 应用迁移验收 | `./.venv/bin/python -m pytest -q agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：8 passed、9 warnings、2.65 秒。 |
| 负例 | 结果 JSON 与重哈希内容不符时 shadow mismatch + PAUSED；历史有 committed prefix 但缺 seed 时拒绝；无额外 cursor/result/outbox/send。 |
| 外部活动 | fake sandbox/runtime 与本地 SQLite/Cerebro；未连接 Gateway/account/真实行情服务，未发送订单。 |

## B157 本轮增量

| 项目 | 当前证据 |
|---|---|
| 改动 | Python upload API 强制 pinned `XNYS@4.13.2`/纽约时区，验证完整客户端 schedule 与宿主生成 schedule 一致；导入和 manifest 使用宿主 schedule。 |
| API 验收 | `./.venv/bin/python -m pytest -q agent/tests/test_backtrader_backtest_api.py --disable-warnings --tb=short`：25 passed、656 warnings、4.94 秒。 |
| 日历/路由验收 | `./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_market_data.py agent/tests/execution/test_execution_backend_routing.py --disable-warnings --tb=short`：20 passed、184 warnings、4.10 秒。 |
| 负例 | 拒绝假日/日期、错误开盘时刻、半日市标志、calendar version、时区、声明 hash；preview 不创建 run/version。 |
| 外部活动 | 仅本地 pytest、固定日历包和测试 fixture；无 Gateway/account/真实订单操作。 |

| 项目 | 当前证据 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；没有提交。 |
| dirty state | 开始时 `git status --short` 列出 53 个已修改/新增路径，包含此前 provider/chat/settings/Electron 变更及 Backtrader/execution/API/UI/SDK/docs 未跟踪实现。本轮仅在 Backtrader 目标目录内续做；未 reset、stash、清理或批量暂存。 |
| 依赖 | Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、pandas 2.3.3、exchange-calendars 4.13.2、uv 0.11.21。主 `.venv` 未安装 `ibapi`；IB API 10.50.2 使用独立 `ibkr-runtime` 和官方源码锁定安装脚本。 |
| Docker | 当前默认 context 为 `default`；`colima-vibe-execution` context 存在且 endpoint 配置到隔离 profile。本轮没有用默认 context 证明 Docker T39 集成。 |
| 本轮起点测试 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**48 passed、210 warnings、0.53 秒**。历史交接的 46 passed/148 warnings 与当前命令结果不同；不回退当前边界用例来匹配历史数值。 |
| 最近目标相关回归 | B155/B156 后执行 `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：退出码 0，**607 passed、1563 warnings、19 subtests、55.91 秒**。Execution UI 11 passed，frontend production build 成功。 |
| 外部活动 | 本轮基础测试与离线回归未连接 Gateway、账户或行情服务，未发送 paper/live 订单；没有产生账户持仓或委托变化。 |

# 实施基线

## 2026-10-22 B149 Python SELL 路由与恢复验收竞态修正

| 项目 | 当前读数 |
|---|---|
| 本轮改动 | `paper_event_bridge.py` 允许已经过 durable host authorization 的 SELL 提案映射到 Backtrader native SELL；bridge/deployment/session 测试覆盖授权卖出、卖超零 reservation/零 send，以及 recovery 的稳定 PAUSED/delivery/thread 前置。 |
| 验收 | `agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py` 534 passed、1474 warnings、19 subtests、53.32 秒，退出码 0；定向 bridge/session/deployment 7 passed。 |
| 外部活动 | 仅 fake IBKR transport 与本地 SQLite/Cerebro；无 Gateway/account/真实订单操作。 |

## 2026-10-22 B147 报告会计 oracle 增量

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；无提交，继承 dirty baseline。 |
| 本轮改动 | `agent/src/bt_runtime/strategies/sma.py` 只为 synthetic runner 增加可选滑点参数（默认不变）；新增 `agent/tests/bt_runtime/test_p1_report_accounting.py`，并更新本交接证据。 |
| 验收 | P1 手算会计回归 1 passed；相关 SMA、market-data、replay-subprocess、Python-upload 合并组 30 passed、437 warnings、4.03 秒；整个 Backtrader runtime + backtest API 子树 116 passed、1147 warnings、9.04 秒。独立核对 SMA 交叉时点、买卖滑点 cap、费用、已平仓 net P&L、cash、期末估值、最大回撤和 subprocess BackBroker replay。 |
| 外部活动 | 仅本地合成 OHLCV、Cerebro/BackBroker、pytest；没有 provider 请求、Gateway/account 连接或订单。 |

## 2026-10-22 B146/B145 dispatch crash-window 增量

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；无提交，继承 B144 dirty baseline。 |
| 本轮改动 | 扩展 `agent/tests/execution/test_dispatch_service.py`，覆盖 SEND_STARTED commit 后、假 Broker 接受后 ACK 前两种子进程 SIGKILL 窗口；更新交接证据，不清理先前工作树内容。 |
| 验收 | `test_dispatch_service.py` 27 passed、0.49 秒；该定向组不等于完整 execution/API 基线。 |
| 外部活动 | 本地子进程、SQLite、append-only 假 Broker 文件和 pytest；未连接 Gateway/account/行情，也未发送订单。 |

## 2026-10-22 B145 dispatch crash-window 增量

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；无提交，继承 B144 dirty baseline。 |
| 本轮改动 | 只新增 `agent/tests/execution/test_dispatch_service.py` 的 durable `SEND_STARTED` 后进程硬杀用例，并更新 Backtrader 交接文档；未清理或改写之前的工作树内容。 |
| 验收 | `test_dispatch_service.py` 26 passed、0.49 秒；仅该定向组，不是完整 execution/API 基线重跑。 |
| 外部活动 | 本地子进程、SQLite 和 pytest；未连接 Gateway/account/行情，也未发送订单。 |

## 2026-10-22 B144 续做基线与隔离增量

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；无提交。 |
| dirty 边界 | 保留先前 provider/chat/settings/Electron 等 tracked edits 及 Backtrader execution/API/BT/frontend/SDK/docs untracked 实现；本轮只扩展 `agent/tests/execution/test_strategy_worker_docker_integration.py` 与本交接文档，没有 reset/stash/清理。 |
| 依赖 | Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、pandas 2.3.3、exchange-calendars 4.13.2；主 venv 不含 ibapi（预期依赖隔离），IB API 10.50.2 位于独立 `ibkr-runtime` venv。 |
| 交接基础组 | 本轮复跑 48 passed、210 warnings；与早期 46/148 记录不同，现有差异已由 B136 续做基线记录为后续 T03 边界覆盖和警告增加，不为匹配旧数字回退代码。 |
| 新验收 | Colima 真实 Docker 后代进程 deadline 专项 1 passed、3.62 秒；Python sandbox launcher 单元 15 passed、1.39 秒；测试 py_compile 与 diff check 通过。 |
| 外部活动 | 仅本地代码/pytest 与隔离 Docker worker；没有真实行情请求、Gateway/account 连接或任何订单操作。 |


## 2026-10-22 B139 cancel remains available in retained STOPPING manager slot

| 项目 | 结果 |
|---|---|
| 变更 | 已有活动订单阻止 failed session 安全 stop 完成时，manager 留在 STOPPING，但仍允许通过独立 cancel grant 调用 exact-intent cancel；服务还须保持 PAUSED、lease held |
| 验收 | Docker 专项 1 passed、3.86 秒；managed-session + manager 35 passed、4.88 秒；py_compile、diff check 通过 |
| 限制 | 只在 runtime service RUNNING/PAUSED 可撤单；不支持已进入服务 STOPPING 后启动新的取消，也不实现 session replacement/recovery |

## 2026-10-22 B138 failed-session activation guard

| 项目 | 结果 |
|---|---|
| 变更 | session 有未处理策略异常时拒绝 activation；已在 B137 真实 Docker timeout integration 覆盖 PAUSED 保持与无策略 order send |
| 验收 | managed-session 文件 17 passed、5.36 秒；相关 Python `py_compile`、`git diff --check` 通过 |
| 限制 | failed session replacement、未 ACK callback 与 native Backtrader 状态恢复仍未实现；T41/T43 PARTIAL |

## 2026-10-22 B137 session-level Docker timeout 续做复核

| 项目 | 结果 |
|---|---|
| 新增证据 | managed session 中真实容器超时后 service 与 SQLite run PAUSED；released event 未提交、cursor 不动、result/outbox=0；callback worker 在 PAUSED 期间持久记录 `nextValidId` candidate 与部分成交，并处理有授权的 exact-intent cancel/cancelled callback |
| 验收 | 扩展 Docker 专项 1 passed、3.68 秒；managed-session 文件最终复跑 17 passed、5.10 秒；py_compile 与 `git diff --check` 通过 |
| 本轮 API/策略/路由回归 | `uv run pytest -q --tb=short -W ignore agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/execution/test_execution_backend_routing.py`：78 passed、6.94 秒、退出码 0。 |
| 边界 | Fake IBKR runtime/transport；未覆盖真实 Gateway/订单行为、daemon/host crash、生产 identity/app wiring。T43 仍 PARTIAL |

## 2026-10-22 续做基线复核（B136）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | 7f6908b7e1b95d971f4151bfaf2382e2ce9485cf / main；未提交 |
| dirty 边界 | 保留既有 provider/chat/settings/Electron 等 tracked edits 与 Backtrader 相关 execution/API/Backtrader/frontend/SDK/doc untracked work；本轮只新增 B135/B136 测试与 backtrader-system 记录。没有 reset/stash/清理，也未触碰 Kelly/ref 数据。 |
| 主环境 | Python 3.12.13、Backtrader 1.9.78.123；官方 ibapi 不在主 venv，独立 ibkr-runtime venv 保持 10.50.2。主 venv import ibapi 失败符合依赖隔离约定。 |
| 交接基础组当前结果 | uv run pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings：48 passed、210 warnings、退出码 0；旧交接记录的 46/148 是历史结果，不回退实现迎合该数值。 |
| Execution UI 复核 | frontend Execution 页面测试：9 passed；npm run build 成功。保留既有 Vite 配置与 chunk-size warnings。 |

## 2026-10-22 B136 真实 worker deadline 与 durable event retry

| 项目 | 结果 |
|---|---|
| 验收 | Docker integration 1 passed、5.50 秒；event worker 11 passed、0.20 秒；py_compile 退出码 0 |
| 证据 | 两次真实容器超时均分类为 SandboxTimeout 并被移除；SQLite released event identity 不变，result/outbox 为空、cursor 未推进，改 Bar 重试不能替换事件 |
| 边界 | 未经过 ManagedPythonPaperSession 异常关闸，不证明 callback continuity 或真实 Gateway 行为。T37/T42/T43 仍 PARTIAL |

## 2026-10-22 B135 联合恢复 BUY native ref 与策略 checkpoint

| 项目 | 结果 |
|---|---|
| 验收 | 联合场景 1 passed；session/journal/callback 合并组 96 passed |
| 证据 | 新 runtime/session 重开 SQLite 后不调用 sandbox/不重发；ref=77 partial BUY 恢复并由后台回报完成，随后从非空 checkpoint 处理下一 bar，cursor 仅推进一次，fake SDK send=0 |
| 边界 | fake IBKR/runtime/sandbox；真实 Cerebro、SQLite 与 Backtrader native order；不覆盖 Docker worker crash、真实 Gateway 或 BUY 以外恢复。T41 仍 PARTIAL |

## 2026-10-22 B134 managed session 非空 checkpoint reopen

| 项目 | 结果 |
|---|---|
| 验收 | checkpoint 专项 1 passed；session/journal/callback 合并组 95 passed |
| 证据 | 不间断 Cerebro/PortBroker reference 与 SQLite reopen/new runtime 的下一事件 state/proposals/cursor 一致；恢复时 worker 调用 0，随后事件仅处理一次且 broker send 0 |
| 边界 | 有状态 fake sandbox；首个已提交事件由 reference result seed，不是 Docker worker crash；没有与活动 native order 联合恢复。T41 仍 PARTIAL |
| 回归调整 | 合并组发现 B132 failure assertion 的异步状态观察竞态；测试等待 service 和 journal 都持久 PAUSED 后保留原断言 |

## 2026-10-22 B133 策略阻塞时 callback 与授权撤单

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | managed Python 策略线程在首事件 sandbox 阻塞期间，fake callback worker 持久化既有 exact-bound 订单的一股部分成交；manager 拒绝 SHARED_KEY 撤单，并让可归因 principal 撤销该 intent 的未成交余量。事件结果未提交时无策略派单。 |
| 验收 | 新并发用例 1 passed；session/callback 组 73 passed；runtime/manager 组 27 passed；用例文件 `git diff --check` 通过。 |
| 限制 | 只测当前进程与合成/假 transport，且活动单由测试预置；不代表生产时序、Gateway callback、进程恢复或 T43 完成。 |

## 2026-10-22 B132 callback failure 联动

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | Managed Python session 链式包装 runtime callback failure sink；fake IBKR callback worker 收到账户不匹配回报后，session run journal 与 PAPER execution scope 都被暂停，并暴露稳定 health code。 |
| 验收 | session/callback 定向组合 72 passed、33 warnings、2.48 秒；runtime/manager 相邻回归 27 passed、0.49 秒；新故障用例 1 passed、3 warnings、0.73 秒。 |
| 限制 | 未验证策略阻塞期间正常回报连续性、授权撤单或 Gateway/账户行为；fatal 后 runtime 按设计保留 lease 等待恢复。T43 仍 PARTIAL。 |

## 2026-10-22 B131 CPU quota 实际节流样本

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | 真实 Docker worker 在 `cpu.max=50000 100000` 的半核容器中忙等两秒，检查 cgroup 前后 `usage_usec`。 |
| 验收 | 墙钟至少 1.9 秒且 CPU 用量不超过 1.75 秒；定向 Docker 用例 1 passed、7.18 秒；完整相关后端组 580 passed、1395 warnings、15 subtests、44.85 秒。 |
| 限制 | 单一配额与短样本，不验证其他 host/runtime 上的 fairness、长时间压力或 quota 变更。T37 仍 PARTIAL。 |

## 2026-10-22 B130 Docker 内存压力与回收负例

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | 测试探针在 32 MiB cgroup 内存上限下逐 MiB 触碰最多 128 MiB 页面；成功分配到阈值外不算通过，结果必须是分配拒绝或 OOM exit 137，并核验容器已清理。 |
| 验收 | 真实 Docker 定向用例 1 passed、5.31 秒；execution/Backtrader/API/PAPER 合并组 580 passed、1397 warnings、15 subtests、42.92 秒；`compileall`/`git diff --check` 通过。 |
| 限制 | 只覆盖一次 memory pressure；不代表 CPU 饱和、其他 OOM/容器 daemon 故障矩阵已通过。T37 保持 PARTIAL。 |

## 2026-10-22 B129 真实容器资源限制回归

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | 在既有文件/网络 Docker 隔离用例中加入 worker 内 cgroup v2 资源读数和 PID 配额超额探针；使用 128 MiB、0.5 CPU、4 PID 配额，并核对 docker inspect 与 cgroup runtime 读数。 |
| 验收 | 定向真实 Docker 用例 1 passed、4.63 秒；execution/Backtrader/API/PAPER 合并组 580 passed、1393 warnings、15 subtests、42.75 秒；`compileall`/`git diff --check` 通过。 |
| 外部活动/限制 | CPU/内存限额被设置并读取，PID 超额创建被内核拒绝；未测 OOM/CPU saturation 后完整清理矩阵。仍无 Gateway/account/订单操作，T37 保持 PARTIAL。 |

## 2026-10-22 B128 官方 IBKR 安装器保护与合并回归

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | 官方源码安装器的 `install()` 在执行前自校验源码文件树锁与 API 版本，防止直接函数调用绕过 CLI 校验；新加 7 项合成源码/调用契约回归。 |
| 验收 | 安装器专项 7 passed；execution/Backtrader/API/PAPER 合并组 **580 passed、1393 warnings、15 subtests、42.41 秒**；`compileall` 与 `git diff --check` 通过。 |
| 外部活动/限制 | 没有 IBKR 官方源码 archive 重装、SDK child、Gateway/account/行情请求/订单活动。测试用合成小源码树和 mocked `uv`，不改变 T37/PAPER 验收状态。既有 52 项 dirty 工作区均保留。 |

## 2026-10-22 B127 当前最新合并回归

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；本轮保留原有未提交工作区修改。 |
| 本轮验证 | execution/Backtrader/API/PAPER 后端合并组 **573 passed、1397 warnings、15 subtests、44.49 秒**；managed-session/callback 定向组 **71 passed、35 warnings**；`compileall` 与 `git diff --check` 通过。Execution UI 9 passed 与 production build 是 B125/B126 期间验证，本轮未改 UI。 |
| 本轮增量 | 受限 managed session 可在 reopen SQLite 后重建当前 RUNNING run 的 exact-linked BUY ACKNOWLEDGED/PARTIALLY_FILLED native order；重建前 quiesce callback worker、重验 snapshot/ledger，Cerebro/native ref ready 后才恢复 callback 消费。strategy/outbox provenance、订单/合约/feed 和账户事实需全部吻合；不调用 sender。T41/T42 仍 PARTIAL。 |
| 外部活动 | 本地 SQLite、Docker worker、fake IBKR runtime 与测试客户端；没有连接 Gateway、读取真实账户、发送订单或访问真实市场数据。无 bar+持仓的 Backtrader clean-stop 市值边界已暴露，未修复。 |

## 2026-10-22 B126 当前最新合并回归

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；现有未提交修改均保留。 |
| 本轮验证 | execution/Backtrader/API/PAPER 后端合并组 **566 passed、1385 warnings、15 subtests、43.16 秒**；broker/hydration 定向组 **23 passed、14 warnings**；Execution UI **9 passed**，`npm run build` 成功。 |
| 本轮增量 | 在 reconciled snapshot 已 hydration 的前提下，离线重建 BUY native Backtrader Order/ref/累计执行状态且不发送；broker-local ref allocator 避免恢复 ref 冲突。尚未接入 managed-session cold start。保留 B125 error health code 和 10 秒只读 UI 状态刷新。 |
| 外部活动 | 本地 SQLite、Docker worker、fake runtime 与测试客户端；没有连接 Gateway、读取账户、发送订单或访问真实市场数据。 |

## 2026-10-22 B125 合并回归

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；现有未提交修改均保留。 |
| 本轮验证 | execution/Backtrader/API/PAPER 后端合并组 **561 passed、1377 warnings、15 subtests、42.79 秒**；session/manager 定向组 **24 passed、23 warnings**；Execution UI **9 passed**，`npm run build` 成功。`compileall` 与 `git diff --check` 通过。 |
| 本轮增量 | managed Python session uncaught thread failure 尽力暂停 durable run journal 与 RUNNING execution scope；manager/status API 和 `/execution` 状态卡显示稳定 `managed_python_session_failed`，不泄露异常消息，UI 文案覆盖 9 种语言并每 10 秒只读轮询状态。 |
| 外部活动 | 本地 SQLite、Docker worker、fake runtime 与测试客户端；没有连接 Gateway、读取账户、发送订单或访问真实市场数据。 |

## 2026-10-22 B124 当前合并回归

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；未提交修改持续保留。 |
| 本轮验证 | execution/Backtrader/API/PAPER 后端合并组 **559 passed、1379 warnings、15 subtests、44.16 秒**；lifecycle/manager/runtime/deployment 定向组 40 passed。 |
| 本轮增量 | RUNNING+lease 下重新 start 先 RECOVERING 并 fresh-check 后回 PAUSED；保留 B123 的类型化 PAPER app composition/lifespan hook（默认 inert）、timestamp datetime 子类拒绝与 fail-stop、报告 integrity 验证后使用同一内存快照响应。 |
| 外部活动 | 只用本地 synthetic fixture、Docker worker、SQLite 与 API 测试客户端；没有连接 Gateway、读取账户、发送订单或访问真实市场数据。 |

## 2026-10-22 B120/B121 当前目标续做起点与增量

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；开工时 52 个 modified/untracked 状态项，全部保留；未 reset/stash/清理/暂存。 |
| 主依赖 | Python 3.12.13；Backtrader 1.9.78.123；pytest 9.1.1；FastAPI 0.142.2；Pydantic 2.13.5；pandas 2.3.3；exchange-calendars 4.13.2；主 `.venv` 未安装 ibapi。独立官方 SDK 环境/来源树由既有记录管理。 |
| 交接基线复跑 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**48 passed、210 warnings、0.50 秒**。比早期 46 多两项后续 T03 SMA 边界测试。 |
| 本轮后端验收 | B120/B121 合并命令 551 passed、1371 warnings、15 subtests、42.43 秒；前端 Execution 专项 7 passed，production build 成功。详见 `ACCEPTANCE.md B121`。 |
| 外部活动 | 仅本地 Docker worker/synthetic fixtures 与 API fakes；无真实市场请求、Gateway/account/order 操作，未更改 Gateway 设置。 |

## 2026-10-22 B110 trusted NL2 preview data freeze

## 2026-10-22 B110 trusted NL2 preview data freeze

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | Preview 抓取并严格导入 XNYS completed daily bars，将 CSV/metadata/session schedule 与 data/manifest hash 绑定进一次性 token；确认时 runner 消费同一输入，不重新抓取。Execution 审阅卡展示 hashes，run artifacts 保存 CSV 和 manifest。 |
| 验收 | 完整 execution/Backtrader/API/PAPER 后端组 478 passed、1339 warnings、15 subtests、41.30 秒；真实 runner 快照消费用例 2 passed（包含报告 replay integrity）；Execution UI 7 passed；`npm run build` 成功；`compileall` 成功。详见 `ACCEPTANCE.md B110`。 |
| 限制/外部活动 | deterministic local XNYS fixture；真实 provider 授权/point-in-time/adjustment/corporate action 与 broker contract mapping 仍未验证。没有访问真实行情服务、Gateway、账户或发送订单。 |

## 2026-10-22 B109 PAPER app manager replacement guard

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | app composition setter 对同 manager/readiness pair 幂等；active manager slot 存在时拒绝替换，避免旧 runtime/lease 被新 app.state manager 遮蔽；空 slot 可替换。 |
| 验收 | routes/state 定向组 18 passed、9 warnings、2.82 秒；后续 B110 完整 execution/API/PAPER 组 478 passed、15 subtests。详见 `ACCEPTANCE.md B109/B110`。 |
| 限制/外部活动 | 仍无 production composition root/providers/identity grants；默认 app manager inert。仅 fake offline runtime，无 Gateway/account/订单活动。 |


## 2026-10-22 B107 FastAPI app-scoped PAPER manager

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | PAPER status/pause endpoints 使用 request app 的 manager/readiness；lifespan shutdown 复用该 app manager；显式 composition setter 可把 manager/readiness 安装到指定 app。旧无 app getter/setter 留作兼容路径。 |
| 验收 | Routes/lifespan/manager 定向组 32 passed、9 warnings；含 execution/Backtrader/API/PAPER 完整相关组 471 passed、804 warnings、15 subtests；compileall 与 `git diff --check` 通过，完整命令在 `ACCEPTANCE.md B107/B107a`。 |
| 限制 | 没有 app-specific production composition root；主 app 的默认 manager 仍 inert、无 runtime factory。没有连接 Gateway/账户。 |

## 2026-10-22 B106 IBKR SDK error/status callback classification

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | SDK `error()` callback 保存完整 primitive payload 到 durable spool；pump 仅忽略五个官方文档列为正常 farm 状态的 code，对连接变化暂停并要求重连/对账，其余（含 order reject）fail-closed。 |
| 验收 | callback/pump/runtime/reconciliation 相关回归 75 passed、15 subtests；完整 isolated SDK suite 44 passed；`compileall` 与 `git diff --check` 通过。详见 `ACCEPTANCE.md B106`。 |
| 限制 | 离线 SDK/API 对象与合成回调；未连接 Gateway/账户。Reject code 尚未有 durable order binding 与 ledger REJECTED 事务，T23 仍 PARTIAL。 |

## 2026-10-22 B105 Python upload static API policy

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | preview/create/resume 与 online PAPER coordinator/session composition 使用 AST source validator；仅允许已列出的标准库模块，拒绝非 allowlist/相对 import、AST 可见的动态执行/直接反射调用和源码中的直接 dunder 访问；契约明确这是 API 限制而非安全边界。 |
| 验收 | B105a 41 passed、B105b Docker isolation 1 passed；完整 execution/backtest/API/PAPER 回归首次 442 passed/11 failed，经修正后同命令 453 passed、793 warnings、15 subtests。原 contracts/position/SMA 基线 48 passed。`py_compile`、`git diff --check` 退出码 0。 |
| 限制 | AST policy 用于 upload API preview/create/resume 与 online Python PAPER composition；通用 worker/offline journal 不应用它，以保留有意义的 adversarial runtime probes。此项没有使 Python upload 获得 PAPER 准入。 |

## 2026-10-22 B104 PAPER endpoint/account identity guard

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | IBKR factory 需显式 Gateway/TWS PAPER profile、对应默认 PAPER port (4002/7497)、loopback 与单个 DU numeric account；managed runtime construction 层也拒绝 live/custom ports；managedAccounts 回调要求精确单账户。新增 factory/callback/runtime fail-closed 用例及 B104/T14/D060 证据更新。 |
| 验收 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_paper_runtime_factory.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_ibkr_managed_runtime.py agent/tests/execution/test_managed_python_paper_deployment.py agent/tests/execution/test_managed_python_paper_session.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：退出码 0，77 passed、23 warnings、3.47 秒。 |
| 安全/限制 | 仅 fake process、离线 SQLite；没有初始化官方 SDK child 或连接 Gateway/账户。IBKR 可更改端口，默认端口/profile/DU account format 不是 broker-side PAPER mode attestation；app composition/readiness 仍 inert。工作区原有 52 个 dirty 文件/项及其他任务改动均保留。 |

## 2026-10-21 B103 trusted report replay

| 项目 | 当前读数 |
|---|---|
| 本轮增量 | Provider runner 为 trusted SMA/RSI 添加 verified-bar 序号 decision log；启动独立 Backtrader/BackBroker 子进程复算，在报告发布前核验执行结果、成本、manifest 与 immutable strategy version；增加 mismatch 不发布报告回归及 B103 交接记录。 |
| 定向验收 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/execution/test_execution_backend_routing.py agent/tests/bt_runtime/test_report_replay_subprocess.py --disable-warnings --tb=short`：退出码 0，26 passed、232 warnings、5.84 秒；`py_compile`、`git diff --check` 通过。 |
| 兼容性修正 | 将 upload replay tests 对 `_build_synthetic_manifest` 的调用更新为当前 `evaluation_start_index` 参数；复算订单比较忽略主/回放策略对非券商 callback 的合成 `Created` 状态记录差异，其他订单核心字段继续严格核对。 |
| 限制/外部活动 | Replay 核验首跑订单日志之后的撮合/会计字段，不独立复算模板信号；replay worker 上限设为 5,000 bars，上传 importer 仍单独限制 1,000。只用本地 synthetic provider fixture；无行情请求、SDK child、Gateway、账户或订单操作。工作区其他已有修改均保留。 |

## 2026-10-21 B102 PAPER startup readiness gate

| 项目 | 当前读数 |
|---|---|
| 起点 | 同一 Vibe HEAD `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；沿用先存 dirty 工作区，无 reset/stash/清理 |
| 本轮增量 | manager factory 前强制 composition readiness、state setter 同实例绑定、manager/state/deployment/session 测试及 B102 交接记录 |
| 验收 | manager/deployment/session/PAPER routes/state wiring 退出码 0，40 passed、34 warnings、3.12 秒；此前 B101 SELL 相邻组 95 passed、B101+`py_compile`/`git diff --check` 记录在上节 |
| 外部操作 | 仅 fake manager/runtime 与本地临时 SQLite；无 SDK child/Gateway/account/市场请求/订单 |

## 2026-10-21 B101 当前状态核对

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区在本轮开始已有 52 个 modified/untracked 状态项，均保留；执行中仅在 Backtrader execution/tests/docs 范围增加本轮修改 |
| 主环境 | Python 3.12.13；Backtrader 1.9.78.123；pytest 9.1.1；Pydantic 2.13.5；FastAPI 0.142.2；主 `.venv` 未安装 ibapi |
| 独立官方 SDK 环境 | `ibkr-runtime/.venv` 中 `ibapi==10.50.2`、`protobuf==5.29.5`；未启动 SDK runtime 或连接 Gateway |
| 交接基线复跑 | 用户给定的 contracts/position-sizing/SMA 命令退出码 0：当前 48 passed、210 warnings、0.54 秒。比 2026-10-20 交接数多两项，是后续加入的 T03 SMA 零差值/初始持仓测试，不是基线回归 |
| B101 离线 SELL 验收 | dispatch/ledger/risk/Cerebro dispatch/callback/paper bridge 相邻组合退出码 0：95 passed、1.61 秒；`py_compile` 和 `git diff --check` 通过 |
| 外部活动 | 无市场数据请求；无 Gateway/账户连接；无订单写入；未更改 Read-Only |

## 2026-10-21 B99 lifespan shutdown 接线续做

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区未提交，既有修改继续保留 |
| 本轮增量 | manager app-shutdown lifecycle method、FastAPI lifespan 顺序接线、PAPER manager 与 app lifespan 回归测试、B99 交接记录；未 reset、stash、清理或提交 |
| 后端验收 | manager/lifespan/live runtime suite 退出码 0，55 passed、9 warnings、3.02 秒 |
| 外部活动 | 本地临时 SQLite/fake runtime；未启动 SDK 子进程、连接 Gateway、读取账户或发送订单 |

## 2026-10-21 B98 managed PAPER composition 续做

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区未提交，沿用本轮之前既有修改 |
| 本轮增量 | 新增 manager-owned managed Python deployment factory；补 session 在 RECOVERING 下安全 stop；新增 factory/lifecycle 组合测试；更新 B98 交接记录。未 reset、stash、清理或提交 |
| 后端组合 | manager/deployment/session/runtime/callback/dispatch/API-route 相关组合退出码 0，76 passed、27 warnings、3.54 秒；pytest 进程正常退出 |
| 源码检查 | 新增/修改 Python `py_compile` 与 `git diff --check` 通过 |
| 外部活动 | fake SDK/runtime 与本地临时 SQLite；没有启动 SDK child、连接 Gateway、读取账户、访问行情服务或发送订单 |

## 2026-10-21 B96 RSI NL2 意图抽取续做

| 项目 | 当前读数 |
|---|---|
| 起始 Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区未提交 |
| 工作区 | provider/chat/settings/Electron 等先存修改和 Backtrader/execution/docs/frontend/ibkr-runtime 未跟踪实现均保留；未 reset/stash/清理/提交 |
| B96 后端专项 | RSI strategy-design/API、Cerebro/SMA/provider runner suite 退出码 0，70 passed、366 warnings、6.15 秒 |
| 验证边界 | 使用 fake intent generator 与本地 synthetic fixture；没有请求真实 LLM/provider、Gateway 或账户 |

## 2026-10-21 B95 RSI 受信任模板接入

| 项目 | 当前读数 |
|---|---|
| 起始 Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区未提交 |
| 原有工作区 | 保留 provider/chat/settings/Electron tracked edits 与 Backtrader/execution/docs/frontend/ibkr-runtime 未跟踪工作；本轮未 reset/stash/清理或提交 |
| B95 后端专项 | compiler/Cerebro/NL2 preview/API/provider runner + SMA 回归命令退出码 0，58 passed、366 warnings、9.08 秒；细节见 `ACCEPTANCE.md` |
| B95 UI | Execution UI 6 passed；production `npm run build` 成功（既有 Vite large chunk warning） |
| 原交接基线 | contracts/position-sizing/SMA 组合退出码 0，48 passed、210 warnings、0.80 秒 |
| 外部活动 | 仅本地合成 bars 和 API fixture；未访问行情服务、IBKR Gateway 或账户，未发送订单 |

## 2026-10-21 B94 续做核对

| 项目 | 当前读数 |
|---|---|
| 代码/测试 | worker → Docker sandbox → event coordinator → Cerebro/API 加入受限、归一化策略错误通道；初次运行与 resume 共享 host 生成的失败投影；新增 sandbox、event、API、真实 Docker 和 Execution UI 测试 |
| 定向验收 | 相关后端组合命令退出码 0，45 passed、178 warnings、17.22 秒；Execution UI 5 passed；`npm run build` 成功（既有大 chunk warning）；真实 Docker worker 独立错误探针包含在后端组合中 |
| 工作区 | 本轮另触及 execution worker/sandbox、Python upload 运行器、API routes、对应测试、Execution UI 测试和 backtrader-system README/PROGRESS/ACCEPTANCE/BLOCKERS/BASELINE/DECISIONS；保留既有其他改动，未 reset/stash/清理/提交 |
| 外部活动 | 仅本地 synthetic fixtures 和 Docker worker；未访问行情服务、IBKR Gateway/账户或提交订单 |

## 2026-10-21 B93 续做核对

| 项目 | 当前读数 |
|---|---|
| 代码/测试 | 在 `agent/tests/execution/test_strategy_worker_docker_integration.py` 增加真实 worker + Cerebro 相同前缀、不同后缀完整服务运行；仅新增测试和交接记录，无生产策略语义变化 |
| 定向验收 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_cerebro_prefix_results_are_invariant_to_future_suffix --disable-warnings`：退出码 0，1 passed、24 warnings、6.93 秒 |
| 工作区 | 继续保留既有 tracked/untracked 工作；本轮另触及 Docker integration test 与 backtrader-system README/PROGRESS/ACCEPTANCE/BLOCKERS/BASELINE；未 reset/stash/清理/提交 |
| 外部活动 | 两次真实 Docker worker 运行均为本地合成输入；未连接行情服务、IBKR Gateway 或账户，未发送订单 |

## 2026-10-21 B92 续做核对

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；未提交 |
| 工作区 | 既有 provider/chat/settings/Electron tracked edits 和 Backtrader/execution/docs/frontend/ibkr-runtime 未跟踪文件均保留；本轮触及 SMA 测试与 backtrader-system README/PROGRESS/ACCEPTANCE/BASELINE；未 reset/stash/清理 |
| 主环境 | Python 3.12.13；Backtrader 1.9.78.123；pytest 9.1.1；FastAPI 0.142.2；Pydantic 2.13.5；pandas 2.3.3。官方 ibapi 仍应使用独立 `ibkr-runtime/.venv`，不从 PyPI 同名包安装 |
| 原交接基线复跑 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，48 passed、210 warnings；新增 2 条 T03 SMA 边界测试前为 46 passed、148 warnings |
| 本轮 T03 专项 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_sma_backtest.py --disable-warnings`：退出码 0，9 passed |

## 2026-10-21 active goal 续做起点

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；未提交 |
| 工作区边界 | 开始本轮前已有 provider/chat/settings/Electron/locales 等修改，以及 Backtrader/execution/API/UI/tests/docs/ibkr-runtime 未跟踪实现；逐项以 `git status --short` 为准。本轮只新建/修改 Backtrader execution 边界内文件，未 reset、stash、清理或批量暂存 |
| 主 Python 环境 | Python 3.12.13；Backtrader 1.9.78.123；pytest 9.1.1；Pydantic 2.13.5；FastAPI 0.142.2；pandas 2.3.3；exchange-calendars 4.13.2；主环境未安装 ibapi |
| 交接基线复跑 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed、148 warnings、0.87 秒 |
| 外部操作 | 未请求行情、未连接 Gateway/账户、未提交订单、未修改 Gateway 设置 |

## 2026-10-21 NL2 续做核对（当前）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；未提交 |
| 工作区边界 | 延续 provider/chat/settings/Electron/locales 等既有 tracked 改动；Backtrader、execution、API/UI、tests、docs 与 `ibkr-runtime/` 的本地工作仍有未跟踪/未提交内容。本轮限定在 B79 NL2 parser、backtest preview API、Execution UI 和交接文档；未 reset、stash、清理或提交 |
| 主 Python 环境 | Python 3.12.13；Backtrader 1.9.78.123；pytest 9.1.1；Pydantic 2.13.5；FastAPI 0.142.2；pandas 2.3.3；exchange-calendars 4.13.2；yfinance 1.7.0；主环境不安装 ibapi |
| 官方 SDK runtime | `ibkr-runtime/.venv` 有官方源码 ibapi 10.50.2、protobuf 5.29.5；仅确认本地安装，不启动 runtime、不连接 Gateway |
| 交接基线复跑 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed、148 warnings、0.76 秒 |
| 外部操作 | 未请求市场数据，未连接 Gateway/账户，未提交订单或更改 Gateway 设置 |

本轮记录对应开始工作时的状态；B79 NL2 API/UI 的测试与构建结果更新在 `ACCEPTANCE.md` 和后续 goal 续做记录，不能由交接基线替代。

更新：2026-10-20（Asia/Shanghai）

本文件是开始实施时的本地读数。仓库在执行前已有用户修改，不能把它们归入本项目变更。

## 仓库状态

| 仓库 | 路径 | HEAD | 分支 | 基线状态 |
|---|---|---|---|---|
| Vibe-Trading | `/Users/yaozekun/strategy_verification/vibe-trading` | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` | `main` | 多项既有 tracked/untracked 修改；未重置或覆盖 |
| Backtrader | `/Users/yaozekun/strategy_verification/backtrader` | `b853d7c90b6721476eb5a5ea3135224e33db1f14` | `master` | `.gitignore`、IBAPI Feed、示例、工具等既有本地修改；只读引用 |

Vibe-Trading 在本次实施开始前已有改动：`.gitignore`、agent/provider/API 文件、Electron 凭据代码、前端聊天/设置与多语言文件、`pyproject.toml`、`scripts/dev`，以及 `PROJECT_GUIDE.html`、`agent/src/providers/model_profiles.py`、`agent/tests/test_model_profiles_multimodal.py`、`uv.lock`。具体状态应以开始实施前的 `git status --short` 为准；本项目不修改这些既有文件，除非后续有明确必要并先核对差异。

## Python 环境

2026-10-20 重新读取包元数据：

| 环境 | Python | Backtrader | ibapi | pytest | Pydantic | FastAPI |
|---|---:|---:|---:|---:|---:|---:|
| Vibe `.venv`（任务开始前） | 3.12.13 | 未安装 | 未安装 | 9.1.1 | 2.13.5 | 0.142.2 |
| Backtrader `.venv` | 3.12.13 | 1.9.78.123 | 10.50.2 | 未安装 | 未安装 | 未检查 |

依赖元数据读取命令：

```sh
./.venv/bin/python - <<'PY'
import importlib.metadata as m
for n in ['pydantic', 'pytest', 'backtrader', 'ibapi', 'fastapi']:
    try: print(n, m.version(n))
    except m.PackageNotFoundError: print(n, 'NOT_INSTALLED')
PY
```

Vibe `.venv` 可运行新执行契约单测。Backtrader 运行时探针最初使用 Backtrader 仓库 `.venv`（该环境没有 pytest）。实施后添加可选 extra `backtrader-runtime`，将 `backtrader==1.9.78.123` 安装进 Vibe `.venv` 和 `uv.lock`。官方 `ibapi 10.50.2` 来源树已单独锁定并成功安装到 `ibkr-runtime/.venv`；主 Vibe `.venv` 仍不安装 ibapi，以免将 SDK 依赖与主项目其他可选依赖混合。

## 已运行命令

| 命令 | 结果 |
|---|---|
| `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py -q` | PASS，11 passed，退出码 0 |
| `python -m pytest agent/tests/execution/test_contracts.py -q` | 未运行成功：系统 Python 没有 pytest；不是代码断言失败 |

本轮没有运行完整后端测试、前端测试或构建；没有连接 Gateway、读取账户、发送订单或修改 Gateway 设置。

## 当前实施边界

- P0 实验已完成；共享 schema 已由集成负责人写入 `agent/src/execution/contracts/`。
- Backtrader 仓库仅作为只读依赖/源码引用。探针产物放在 Vibe 的 `docs/backtrader-system/experiments/`。
- 当前无已确认的 paper 账户/profile/grant 配置；这不阻止离线实验，但阻止真实 paper 写入验收。

## 2026-10-20 goal 续做核对

以下是本轮续做核对，不覆盖上方“任务开始前”历史读数。

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main` |
| 工作区 | 仍含既有 provider/chat/settings/Electron 改动，以及本项目 Backtrader/execution/docs 新增文件；未 reset/stash，逐项保留 |
| Python / 核心包 | 3.12.13；Backtrader 1.9.78.123；Pydantic 2.13.5；pytest 9.1.1；FastAPI 0.142.2；pandas 2.3.3；yfinance 1.7.0 |
| IB API / 交易日历 | 官方 `ibapi 10.50.2` 已安装在独立 `ibkr-runtime/.venv`；Vibe `.venv` 不安装 `ibapi`。交接初始环境未安装 `exchange-calendars`，B30 后主 `.venv` 固定安装 4.13.2 |
| 原交接定向测试 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed |
| 新增版本/事件测试 | `./.venv/bin/python -m pytest agent/tests/execution/test_event_journal.py agent/tests/execution/test_strategy_versions.py -q --disable-warnings`：退出码 0，21 passed |
| Python 安装器 | `.venv/bin/python -m pip` 不可用（No module named pip）；`uv` 可用，现有 pytest 入口运行正常 |
| 容器环境 | Colima 0.10.3 专用 `vibe-execution` profile 已启动：2 CPU、4 GiB、20 GiB、`--mount none`；Docker 29.5.2 / Ubuntu 24.04.4 ARM64。默认 Docker context 已恢复为 `default`，容器操作需显式 `--context colima-vibe-execution` |
| 定向测试 | B3 执行 contract/position/event/version/risk/SMA/market-data 测试退出码 0，79 passed；`ACCEPTANCE.md` 记录精确命令 |
| 容器环境 | Colima 0.10.3 专用 `vibe-execution` profile 已启动：2 CPU、4 GiB、20 GiB、`--mount none`；Docker 29.5.2 / Ubuntu 24.04.4 ARM64。默认 Docker context 已恢复为 `default`，容器操作需显式 `--context colima-vibe-execution` 或 `DOCKER_CONTEXT` |
| 隔离镜像 | 官方 `python:3.12-slim` RepoDigest 为 `python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`。使用缓存 base + `--network=none --pull=false` 构建的 worker 已通过真实隔离探针：B4 1 passed，B5 execution 子树 85 passed。它只验证 launcher/container slice，没有策略 event IPC 或 paper risk broker |

IBKR runtime 来源验证与安装结果见 [ibkr-runtime 安装说明](../../ibkr-runtime/README.md) 和 [ACCEPTANCE.md](ACCEPTANCE.md) 的 B7。安装器验证源码文件树 hash，不记录官方压缩包 hash；没有实现或连接 Broker。

本轮 P3a 离线切片复跑：execution 子树（含 Docker）99 passed；Backtrader runtime 18 passed；核心组 93 passed；独立 IBKR SDK fake callback/send gate 5 passed。`PortBroker`/ledger/SDK submitter 目前仍是分离的测试原语，尚无可信服务集成。

执行页测试 2 passed、`npm run build` 成功。没有运行完整后端测试或任何 IBKR 连接；没有读取账户、下单或更改 Gateway 设置。

## 2026-10-21 goal 续做核对（四）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main` |
| 工作区 | provider/chat/settings/Electron 等原有 tracked edits 保留；execution/Backtrader/docs/frontend/ibkr-runtime 等新增文件保持未跟踪；本轮修改仅触及 `agent/backtest/runner.py`、`agent/src/bt_runtime/{calendars.py,market_data.py}`、对应测试、执行方案文档和依赖锁，不清理/覆盖其他更改 |
| 当前依赖 | `.venv`: Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、FastAPI 0.142.2、exchange-calendars 4.13.2；官方 ibapi 不在主 venv，独立 SDK 来源与安装见前文 |
| 交接基线 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed、148 warnings |
| B30 当前定向验收 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/bt_runtime/test_market_data.py agent/tests/execution/test_execution_backend_routing.py agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，58 passed、228 warnings；details in `ACCEPTANCE.md` |
| B31 完整受影响子树 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，199 passed、563 warnings |
| 依赖锁 | `uv lock --project .` 退出码 0，仅新增 `exchange-calendars==4.13.2` 及其依赖记录；主环境用 `uv pip install --python .venv/bin/python exchange-calendars==4.13.2` 安装，没有执行全项目 sync |

本轮只运行离线合成 provider fixture 和真实 Cerebro/BackBroker；没有请求网络行情，没有启动 Gateway、读取账户或提交任何 paper/live 订单。

## 2026-10-21 B59–B62 续做核对

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`，本轮未提交 |
| 工作区 | 原有 provider/chat/settings/Electron 等 tracked edits 和本任务此前的 untracked `agent/src/execution/`、`agent/src/bt_runtime/`、`ibkr-runtime/`、`docs/`、frontend execution 等均保留；本轮未 reset/stash/删除；本轮触及 `execution_service.py`、`ibkr_callback_pump.py`、`ibkr_runtime_process.py`、相关测试和 backtrader-system 进度文档 |
| 主环境 / SDK runtime | 主 `.venv`: Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、Pydantic 2.13.5、FastAPI 0.142.2、exchange-calendars 4.13.2、pandas 2.3.3、yfinance 1.7.0；独立 `ibkr-runtime/.venv`: Python 3.12.13、官方源码 ibapi 10.50.2、protobuf 5.29.5。两个 venv 的 `pip` module 均不可用；pytest/compileall 通过对应 venv Python 执行 |
| 既有交接基线 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed、148 warnings |
| 本轮 runtime 回归 | SDK `ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -q`：38 passed；host supervisor `./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_runtime_process.py --disable-warnings`：7 passed、9 subtests；callback/dispatch/manager/service 命令见 ACCEPTANCE B62：53 passed |

本轮只使用本地 SQLite 与 fake SDK/status reader 做离线故障验证；没有请求行情网络、启动 SDK runtime、发送 subprocess `start`、连接 Gateway、读取账户、发送订单或改变 Gateway 设置。

## 2026-10-20 Python upload runner + artifact service

| 项目 | 当前读数 |
|---|---|
| 实现 | `python_upload.py` 将冻结 Python 版本通过逐 completed-bar Docker worker 接入真实 Cerebro/native `bt.Order`/BackBroker；`python_upload_service.py` 接收严格 importer 结果与原始 CSV bytes，并保存原始行情、manifest、version 和 report |
| 本地 BT runner 测试 | `./.venv/bin/python -m pytest agent/tests/bt_runtime/test_python_upload_backtest.py -q --disable-warnings`：退出码 0，3 passed，52 warnings |
| Artifact service 测试 | `./.venv/bin/python -m pytest agent/tests/bt_runtime/test_python_upload_service.py -q --disable-warnings`：退出码 0，1 passed |
| 真实 Docker 集成 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_strategy_worker_docker_integration.py -q --disable-warnings`：退出码 0，2 passed；一个隔离负向测试、一个 Python worker→Cerebro/BackBroker 离线闭环 |
| 当前受影响全组 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime -q --disable-warnings`：退出码 0，146 passed、284 warnings；无 Gateway 连接或订单 |

以上为新增离线 runner/service 证据；没有通过用户 API/UI 发起 Python upload，没有 durable event replay/restart 验收、独立 BackBroker 报告重放、完整后端回归或 paper 交易。

## 2026-10-20 Python upload API + UI

| 项目 | 当前读数 |
|---|---|
| 后端 API | 独立认证 `POST /runs/backtests/python-upload`；仅接受受限体积 Python source、严格 completed-bar CSV、单独 session schedule、元数据及固定现金/费用边界；生成 long-only explicit-quantity Market/DAY immutable version。要求配置 `VIBE_PYTHON_WORKER_IMAGE` 为固定 image ID/digest |
| 后端相关回归 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，152 passed、292 warnings |
| Execution UI | Python `.py`、CSV 与 schedule JSON 三文件上传；元信息表单、模板下载、report version hash 和 run detail；`npm run test:run -- src/pages/__tests__/Execution.test.tsx`：3 passed |
| UI production build | `npm run build`：退出码 0；有 Vite config loader 和 chunk size warnings。`` |

单次 Python 回测最多允许 1,000 bars，因为当前按每根 bar 启动一次性 worker；超限在启动 worker 前拒绝。该 API/UI 仍只支持离线回测；UI/API 测试不真实运行 Docker，真实 Docker→Cerebro/BackBroker 路径单独见 B17。没有配置生产 worker image，没有通过用户 UI 做 Gateway/paper 操作。

## 2026-10-20 goal 续做核对（二）

本次续做重新读取了上级 `AGENTS.md` 与本目录 PROGRESS/BLOCKERS/DECISIONS/BASELINE/README/REVIEW/ARCHITECTURE/CONTRACTS/DELIVERY_PLAN。当前状态如下；不覆盖前述历史读数。

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`，本轮无提交 |
| 工作区 | provider/chat/settings/Electron 等既有 tracked 修改仍在；本项目 `agent/src/execution/`、`agent/src/bt_runtime/`、`agent/tests/`、`docs/`、`ibkr-runtime/` 等仍为未跟踪新增；未 reset/stash/清理 |
| 当前 Python / 核心依赖 | `.venv`: Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、Pydantic 2.13.5、FastAPI 0.142.2、pandas 2.3.3、yfinance 1.7.0；`exchange-calendars` 与主环境 `ibapi` 未安装 |
| IBKR 独立 runtime | `ibkr-runtime/.venv`: 官方源码 `ibapi` 10.50.2、protobuf 5.29.5；不连接 Gateway |
| 交接基线命令 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：本轮退出码 0，46 passed，148 warnings |
| 本轮新增验收 | event worker/journal 13 passed；策略 worker 真实 Docker 网络/宿主文件负向集成 1 passed；execution + Backtrader runtime 全子树 141 passed，208 warnings。命令与边界见 `ACCEPTANCE.md` B13–B15 |

账户操作：没有连接 IBKR、读取账户、发单或改 Gateway 设置。Colima/Docker 仅用于离线策略 worker 隔离验收；测试源代码只访问合成当前 bar、临时 sentinel 路径和本机 loopback listener。


## 2026-10-20 Python upload API + UI

| 项目 | 当前读数 |
|---|---|
| 后端 API | 独立认证 `POST /runs/backtests/python-upload`；仅接受受限体积 Python source、严格 completed-bar CSV、单独 session schedule、元数据及固定现金/费用边界；生成 long-only explicit-quantity Market/DAY immutable version。要求配置 `VIBE_PYTHON_WORKER_IMAGE` 为固定 image ID/digest |
| 后端相关回归 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：最新退出码 0，168 passed、292 warnings（B25，2026-10-21） |
| Execution UI | Python `.py`、CSV 与 schedule JSON 三文件上传；元信息表单、模板下载、report version hash 和 run detail；`npm run test:run -- src/pages/__tests__/Execution.test.tsx`：3 passed |
| UI production build | `npm run build`：退出码 0；有 Vite config loader 和 chunk size warnings |

该 API/UI 仍只支持离线回测；UI/API 测试不真实运行 Docker，真实 Docker→Cerebro/BackBroker 路径单独见 B17。没有配置生产 worker image，没有通过用户 UI 做 Gateway/paper 操作。

## 2026-10-21 goal 续做核对（三）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main` |
| 工作区 | 原有 provider/chat/settings/Electron 等 tracked 修改保留；本目标的 `agent/src/execution/`、`agent/src/bt_runtime/`、`agent/tests/`、`docs/backtrader-system/`、`ibkr-runtime/` 等仍是新增/未跟踪文件。新增 report replay 与 durable backtest recovery 实现在 Backtrader runtime、相关测试、API resume 路由、Execution 报告展示及验收文档；未改动上述既有 provider/chat/settings/Electron 文件；没有 reset/stash/清理 |
| Python / 依赖 | `.venv`: Python 3.12.13、Backtrader 1.9.78.123、pytest 9.1.1、Pydantic 2.13.5、FastAPI 0.142.2、pandas 2.3.3；未改依赖或 lockfile |
| 当前后端相关回归 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，174 passed、516 warnings（B28） |
| 当前改动专项测试 | `./.venv/bin/python -m pytest agent/tests/bt_runtime/test_python_upload_backtest.py -q --disable-warnings`：退出码 0，6 passed、202 warnings |
| Execution 页面测试/构建 | `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：退出码 0，3 passed；`npm run build`：退出码 0，Vite 大 chunk/config-loader warnings |
| 原交接测试 | 46 passed 的 contract/position sizing/SMA 基线命令见上方“goal 续做核对（二）”；本轮未倒退或覆盖该历史记录 |

本轮报告复算使用真实 Backtrader 两次运行和合成 bars；B28 通过专用离线事件 journal 恢复被注入的 host crash，并重建真实 BackBroker 订单/fill/权益。前端本轮无修改，保留先前 F5/F6 报告驱动展示证据。没有连接 IBKR、读取账户、发单或修改 Gateway。


## 2026-10-21 goal 续做核对（四）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；本轮无提交 |
| 工作区 | 延续此前 dirty state；本轮新增 `ibkr-runtime` SDK factory/transport/order ID allocator 测试与 `IbkrCallbackPump.next_valid_id_sink` 接线点，并更新 backtrader-system 文档。未 reset/stash/清理，无 IB Gateway 配置变更 |
| IBKR runtime | 独立 `.venv` 官方 `ibapi 10.50.2`、protobuf 5.29.5；本轮 SDK/runtime 19 tests passed |
| 受影响专项 | `test_ibkr_callback_pump.py` 21 passed；compileall 与 `git diff --check` 通过 |
| 当前相关后端子树 | B38：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，209 passed、563 warnings |

这轮仅验证官方 SDK 对象的离线构造和 fake client 边界；未连接 Gateway、读取账户、发出 paper/live 订单或修改 Read-Only。`nextValidId` allocator 与 app execution service 的组合 wiring、合约映射来源审查、跨机器 owner 和完整重启 reconciliation 仍未完成。


## 2026-10-21 goal 续做核对（五）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；本轮无提交 |
| 本机 owner 实现 | 新增 `ExecutionOwnerLease` 并设为 `BacktraderDispatchPort` 必需注入；scope 为 broker/account/environment，同一 OS 主机/锁目录内使用 `flock` |
| owner/dispatch 专项 | B39：`./.venv/bin/python -m pytest -q agent/tests/execution/test_owner_lease.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py`：退出码 0，28 passed |
| 当前相关后端子树 | B40：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，212 passed、563 warnings |
| 依赖与账户边界 | 未改主项目依赖；SDK 仍只在独立 `ibkr-runtime/.venv`；没有连接 Gateway、账户读取、paper/live 发送或 Read-Only 设置变更 |

此租约不是跨机器 lease，app service 尚未统一 lock directory 或管理启停周期。后续需实现真正的 execution service/supervisor、两类受鉴权 IPC 和进程恢复，再申请 paper 配置。


## 2026-10-21 goal 续做核对（六）

| 项目 | 当前读数 |
|---|---|
| owner lease 边界 | `BacktraderDispatchPort` 必须持有 broker/account/environment 相符的 lease；锁目录 0700、metadata 0600；跨 OS 子进程争用拒绝 |
| 最新专项 | B41：`./.venv/bin/python -m pytest -q agent/tests/execution/test_owner_lease.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py`：退出码 0，30 passed |
| 最新相关后端子树 | B42：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，214 passed、563 warnings |
| 环境和操作 | `.venv` 主环境与独立官方 `ibkr-runtime/.venv` 未更换；没有 Gateway 连接、账户读取、paper/live 下单或设置变化 |

代码已建立单机租约 primitive 和 dispatch enforce；应用启动服务仍需统一持锁 lifecycle/目录，并组合认证管理通道、策略 IPC 与进程恢复。


## 2026-10-21 goal 续做核对（七）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；本轮无提交 |
| 原交接基线复验 | `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，46 passed、148 warnings |
| IPC 专项 | B45：版本化 stdio 协议、请求 ID 关联、旧版本/过期 ID/特权响应字段拒绝；27 passed、284 warnings |
| sandbox deadline 专项 | B46：真实 Docker 中强杀 host supervisor，worker 在容器 deadline 后退出并由 daemon 自动删除；17 passed、24 warnings |
| 最新相关后端子树 | B47：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，224 passed、563 warnings |
| 工作区与账户边界 | 保留既有 tracked 修改及目标新增文件；本轮未 reset/stash/清理。没有连接 Gateway、读取账户、发送 paper/live 订单或改动 Gateway 设置 |

B45 的 request ID 只用于关联请求响应，不提供管理认证或授权；B46 只证明策略容器在宿主 Python 进程被强杀后会自行到期并被清理。尚需可信 execution service 在 worker 失败时持续处理 IBKR callback、授权撤单、告警和订单冻结，并组合控制闸门、owner lease、策略 IPC、IBKR SDK lifecycle 与启动恢复。


## 2026-10-21 goal 续做核对（八）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；本轮无提交 |
| lifecycle 专项 | B48：`./.venv/bin/python -m pytest -q agent/tests/execution/test_execution_service.py agent/tests/execution/test_owner_lease.py agent/tests/execution/test_dispatch_service.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings`：退出码 0，33 passed |
| 最新相关后端子树 | B49：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，230 passed、563 warnings |
| 相关实现 | 新增 `PaperExecutionService`，仅 PAPER scope；启动恢复完成后 PAUSED，readiness 通过后显式激活，STOPPING 等待 quiescence 后释放 lease；B45/B46 IPC 与 worker deadline 证据仍有效 |
| 工作区与账户边界 | 延续既有 dirty state，本轮仅新增 execution service、其测试和 Backtrader 文档。没有 reset/stash/清理、Gateway 连接、账户读取、paper/live 发送或 Gateway 设置变更 |

该生命周期对象的恢复/readiness/quiescence 检查由 host 注入，当前测试使用 fake predicate；没有 IBKR client、账户/订单对账、可信 facts provider、应用管理 API 或真实服务启动组合。不能据此声称已完成 paper 准入。


## 2026-10-21 goal 续做核对（九）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区延续 dirty state，无提交 |
| Host allocator/pump/dispatch | B63a 定向组：46 passed；新增 host SQLite allocator 与 stale-candidate 清理；pump 握手事件只在验证回调后置位 |
| SDK runtime | B63b 独立 venv 全套：39 tests passed；修复同步 stale `nextValidId` candidate 状态 |
| 账户与操作边界 | 没有 app runtime composition、account/open-order/position reconciliation 或 Gateway 连接；未读账户、未发送 paper/live 订单、未改 Gateway 配置 |

B63 只使 order-ID 候选闸门在 host/SDK 两端行为一致，不代表 broker cursor 已与本地未决订单完成核对。继续目标应先完成 app-managed runtime composition 和信任 facts provider；在其可复现验收之前，不确认 candidate 或开启 durable RUNNING gate。


## 2026-10-21 goal 续做核对（十）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区延续 dirty state，无提交 |
| SDK snapshot callbacks | B64b SDK 全套：43 tests passed；快照请求仅支持 RUNNING 的 PAPER SDK client，包含三组 callback 结束标记和账户摘要/持仓/开放订单快照 ID |
| Host snapshot/recovery | B64a 定向组：55 passed、15 subtests passed；覆盖 SQLite capture/migration、strict callback routing、精确本地订单和成交持仓对账、候选 ID 与持久 proof 绑定、未映射 broker order 拒绝 |
| 相关后端子树 | B64c 最终复跑：280 passed、563 warnings、15 subtests passed。首次 278 passed + 一项 2 秒 subprocess timeout，单测复跑通过，最终整组通过；详见 ACCEPTANCE |
| 账户与操作边界 | 仍无 app runtime composition、授权账户/行情 facts provider 或 execution-history repair；未连接 Gateway、读取真实账户、发送 paper/live 订单、改 Gateway 设置或清理工作区 |

SDK query/capture 和本地 reconciliation primitive 已可组合，但尚无实际服务 factory 驱动它们。任何真实连接之前，必须在 app-managed RECOVERING 流程内完成接线与故障验收；不明 executions 仍须暂停等待明确历史数据，不能自动开放 allocator。


## 2026-10-21 goal 续做核对（十一）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区 dirty state 延续，无提交 |
| Managed runtime | B65a：57 passed、15 subtests passed；runtime constructor 零连接，显式启动走 recovery→PAUSED；账户错配留在 RECOVERING，失败静默检查不释放 lease |
| 相关后端子树 | B65b 最终复跑：282 passed、563 warnings、15 subtests passed |
| 实际 Gateway/账户操作 | 没有生产 runtime factory、FastAPI manager 接线或可归因控制身份；没有连接 Gateway、读取真实账户、发 paper/live 订单、改 Gateway 设置或遗留真实持仓/委托 |

B65 将 manager 可调用的 runtime orchestration 补齐，但 Vibe 应用尚未配置/持有实例。下一步优先做 trusted config-backed runtime factory 和生命周期/状态接线，并继续确保缺失配置时 OFFLINE；状态界面/管理写 API 应先有可信 principal 与 grant，不能用当前 loopback/shared-key 身份开权限。


## 2026-10-21 goal 续做核对（十二）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；既有 dirty state 保持，无提交 |
| Trusted PAPER feed | B66a：3 passed、6 warnings；真实 Cerebro + `PortBroker` 在只有一根输入 bar 的情况下，idle 时处理回报/native fill；partial/future/symbol/order/queue 负例有断言 |
| 相关后端子树 | B66b：285 passed、569 warnings、15 subtests passed |
| 账户/provider边界 | feed 尚未接行情 provider、日历、真实合约或 app runtime；未连接 Gateway、读取账户或发送任何订单 |

B66 解决 host completed bar 到真实 Cerebro feed 与 no-bar broker notification 的机制验证。下一步继续完成策略固定版本在 PAPER Cerebro 中的 trusted wiring，并明确如何以同一 broker snapshot 初始化 BT 现金/持仓，再接入配置驱动但默认 OFFLINE 的应用 manager。


## 2026-10-21 goal 续做核对（十三）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；既有 dirty state 保持，无提交 |
| 冻结策略 PAPER 离线组合 | B67a：21 passed、18 warnings；StrategyVersion/run hash 校验，host-completed Bar 经逐事件 worker 驱动真实 Cerebro + PortBroker，fake offline fill 入 durable ledger |
| broker snapshot hydration | B67b：18 passed、8 warnings；reconciled PAPER snapshot 的唯一 USD `TotalCashValue` 和 conId positions 在 start 前一次性恢复；无首 bar 时持仓市值明确不可用 |
| 相关后端子树 / SDK runtime | B67c：301 passed、581 warnings、15 subtests passed；B67d：43 tests passed |
| 实际 Gateway/账户操作 | 未连接 Gateway、读取账户、发送订单或改变设置；没有新增真实持仓/委托 |

B67 完成同版本隔离 worker→Cerebro/PortBroker 离线 wiring primitive 和严格账户快照 hydration primitive，但它们还未被 production manager factory/app lifespan 调用，也未使用真实 snapshot/provider。下一步为 runtime composition root 提供经验证的账户/合约/行情/risk/grant 配置，并确保缺任一来源就不创建或连接 SDK runtime；管理写操作还需要可归因 Principal。


## 2026-10-21 goal 续做核对（十四）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；既有 dirty state 保持，无提交 |
| Session lifecycle / market contracts | B68a：13 passed、20 warnings；长驻 session push/stop/join/error 与未声明 capability 拒绝 |
| 相关后端子树 | B68b：308 passed、589 warnings、15 subtests passed |
| Execution UI | B68c：定向 3 passed；`npm run build` 退出码 0（Vite config 与 chunk warning） |
| 真实行情/交易环境 | 没有已认证 provider/calendar/rules/cost adapter；未配置或连接 Gateway、未读取真实账户、未发送订单 |

B68 的市场能力合同只是类型与 fail-closed adapter base；当前 history/realtime/paper/live capability 均没有真实实现。session 长驻逻辑仍受 offline transport 限制。下一步推进可供 production manager 显式注入的 PAPER stream/SDK session composition 与状态 API，同时保留账户、行情、授权事实缺失时绝不连接/派单的边界。

## 2026-10-21 goal 续做核对（十五）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；既有 dirty state 保持，无提交 |
| PAPER runtime factory | B69a：工厂/managed runtime/manager 专项 13 passed；factory 构造零子进程、零 lease、零网络连接 |
| 只读 status route/UI | B70a：49 route/API/factory tests；B70b：Execution 页面 3 passed、build 退出码 0 |
| 相关后端子树 | B70c：315 passed、589 warnings、15 subtests passed |
| 真实 Gateway/账户操作 | 未启动子进程、连接 Gateway、读取账户、发送订单或改变 Gateway 设置 |

B69 把已有 primitives 组合成显式配置、同库、离线构造的 factory；B70 将未配置/运行状态安全地呈现到 API 和 UI。默认 manager 仍无 factory。下一步是为 PAPER session 建立可信 account snapshot hydration 与已验证 market bar 的连接，并让其只在 managed runtime 的恢复/授权生命周期内工作；在身份归因与 grant/provider 前不增加会触发连接的控制 API。


## 2026-10-21 goal 续做核对（十六）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；工作区延续既有 dirty state，本轮无提交 |
| 开始前基础定向组 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：46 passed、148 warnings |
| Python upload 审阅确认 | B75：preview→显式确认；review token 绑定 source、CSV/manifest、spec、engine/compiler 与请求参数；输入变更、过期/重复 token 均在创建 run/version 前拒绝；worker image 缺失时不签发可运行 token |
| 相关后端子树 | B75：325 passed、657 warnings、15 subtests passed；API 专项 9 passed、58 warnings |
| 前端 / 静态检查 | Execution UI 3 passed；`npm run build` 退出码 0（Vite chunk-size warning）；修改的 Python 文件 compileall 通过，`git diff --check` 通过 |
| 账户与外部操作边界 | 未连接 Gateway、读取账户、发送 paper/live 订单或更改 Gateway 设置；未清理/重置工作区或提交 |

B75 补上 Python 策略上传在执行前的审阅与确认，但上传的策略语法目前只接受固定的 `on_event` 入口，尚未实现 NL2/多轮澄清；行情来源、日历、调整规则与隔离标签也仍需可信 provider 验证。下一阶段继续优先完成 T39 的可信 provider/release provenance 与可复现准入证据。


## 2026-10-21 goal 续做核对（十七）

| 项目 | 当前读数 |
|---|---|
| Vibe HEAD / 分支 | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；既有工作区更改持续存在，无提交 |
| 相关运行依赖 | Python 3.12.13、Backtrader 1.9.78.123、pandas 2.3.3、exchange-calendars 4.13.2、pytest 9.1.1 |
| B76 定向输入/provenance | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_market_data.py agent/tests/bt_runtime/test_backtest_event_journal.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/bt_runtime/test_python_upload_service.py agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，30 passed、335 warnings |
| B76 真实 Docker 组合 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_verified_bar_cursor_journal_and_real_docker_worker_share_one_cerebro_run --disable-warnings`：退出码 0，1 passed、16 warnings |
| B76 相关后端子树 | `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，330 passed、673 warnings、15 subtests passed |
| 工作区/账户边界 | 本轮仅改 backtrader event coordinator、market-data digest 验证、相关测试及交接文档；既有 provider/chat/settings/Electron/locales 等 dirty 文件保留。未 reset/stash/清理/提交、未连接 Gateway、未读取账户或发送订单 |

B76 的 coordinator 固定序列、durable cursor 和当前 Backtrader feed timestamp 对齐已进入真实 Docker+Cerebro 组合验收，且策略输入 digest 能检测 manifest 绑定数据被篡改。完整 T39 仍缺多个 host cursor 变体与全面 introspection/逃逸矩阵；真实 provider/授权/调整口径与真实行情冻结样本也仍未验证，故不提升 T39/T08 为 PASS。

# 2026-10-22 B161 增量基线

| 项目 | 当前证据 |
|---|---|
| Vibe HEAD / branch | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`，无提交。 |
| 工作区 | 继承 B160 开始时 53 个 dirty/untracked 路径；本轮只新增 `agent/src/execution/paper_control_grants.py`、`agent/tests/execution/test_paper_control_grants.py` 和 Backtrader 交付文档，不做 reset/stash/clean/stage。 |
| 新增能力 | SQLite-backed PAPER action grant evaluator：主体/auth method/tenant、IBKR PAPER account、单 action、有效期、issuer 与 revoke 持久化；授权失败在 runtime factory 前拦截。 |
| 定向验收 | `./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_control_grants.py agent/tests/execution/test_paper_execution_manager.py --disable-warnings --tb=short`：退出码 0，30 passed、0.46 秒。 |
| 组合验收 | execution + Backtrader runtime + backtest API + PAPER routes：626 passed、1607 warnings、19 subtests、58.71 秒，退出码 0。 |
| 依赖/账户边界 | 继承 B160 的 Python 3.12.13、Backtrader 1.9.78.123、独立 `ibkr-runtime` 环境和依赖记录。本轮未读取 Gateway/账户配置、未连接服务、未发送 PAPER/live 订单。 |
| 限制 | grant issue 仅供可信控制面代码调用，没有签发 API、真实 identity provider 或 production app root；T13/PAPER readiness 不通过。 |
