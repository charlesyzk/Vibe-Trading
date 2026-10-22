# B195 配置本机常驻隔离 Python worker（2026-10-22）

- 在本机 Docker context `colima-vibe-execution` 离线构建 Python worker，基础镜像使用 Dockerfile 已固定 digest 的 Python 3.12；读取构建产物 image ID 并将其作为 `VIBE_PYTHON_WORKER_IMAGE=sha256:...` 写入用户级 `~/.vibe-trading/.env`，同时固定 `DOCKER_CONTEXT=colima-vibe-execution`。配置文件权限为 0600，没有复制或输出其他环境变量。
- 调整 `scripts/dev` 合并加载项目 `agent/.env` 与用户级 `.env`，后者覆盖前者；此前新增用户级文件会因启动脚本只加载首个命中文件而遮住项目环境配置。
- 重启现有开发后端并确认进程环境包含配置的 worker image digest 与 Docker context；本机 worker seccomp/image preflight 及一轮隔离 JSON IPC 回环成功。
- **边界：** 验收的是本机 `scripts/dev` 开发后端与 Colima context。PAPER IBKR 下单/ack/fill/reconciliation 仍未验收。worker image 为本机 Docker daemon 中的 digest 身份；其他机器/安装包需自行构建受信镜像并配置其 image ID。

# B194 真实 PAPER Gateway 历史行情进入两条 Backtrader 回测入口（2026-10-22）

- 用户登录 PAPER IB Gateway 后，本机 127.0.0.1:4002 监听。初次只读状态检查因未安装可选 ib_async 依赖而未握手；按锁文件启用 ibkr extra 后，PAPER profile 握手成功、返回账户清单并通过 PAPER 账户格式校验。没有查询账户余额/持仓。
- 首次 AAPL 历史数据请求发现默认 TWS 端口 7497 与 Gateway 端口不同，且 IBKR 拒绝 America/New_York 的 endDateTime 标记（错误 10314）。增加 PAPER/LIVE 环境对应的 Gateway/TWS 端口选择，并改用 IBKR 接受的 US/Eastern。随后从 PAPER Gateway 取到 2026-09-21 至 2026-10-21 的 **23 根 AAPL TRADES 日线**，合约为 USD STK SMART、conId 265598。
- 真实 NL2 API preview→confirm 走上述 IBKR 只读行情，使用 Cerebro + BackBroker 完成回测，报告 provider=ibkr、execution integrity=verified；run artifacts 放在临时目录并随验收脚本退出清理。Python upload 行情接口也从同一 Gateway 返回 23 根 bar，生成 CSV 与 pinned XNYS schedule，并通过原 preview importer。随后使用临时 digest-pinned Python worker 和真实 IBKR 3 根日线完成 Python-upload preview→create run；run status=success、backend=backtrader、provider=ibkr、execution integrity=verified，临时 run artifacts 与 worker image 均已清理。
- **常驻配置待办：** 本机验收 shell/API 环境没有持久设置 `VIBE_PYTHON_WORKER_IMAGE`。本轮用临时 digest-pinned 镜像显式注入环境完成端到端验收；要让日常 UI 的 Python 上传运行可用，还需把受支持的 worker 镜像配置进启动 API 的 app 环境。
- 后端离线回归及连接端口/API 定向测试、前端 Execution/Portfolio 回归和 production build 见下方 B194 验收记录。
- **边界：** 只证明本次只读连接和这段历史行情可以驱动 Backtrader 离线回测及隔离 Python 上传策略。行情许可、point-in-time 发布时点和复权语义仍未核验；没有查持仓、做 what-if 或发订单。PAPER 下单 ack/fill/position/reconciliation 的验收仍未完成。

# B193 将只读 IBKR 日线接入 Python 上传，并展示连接验收证据（2026-10-22）

- 新增 Python upload 行情准备接口：用户选择本地 PAPER 或 LIVE read-only profile、证券代码和回测日期后，宿主只读请求 IBKR 日线，按固定 XNYS 日历生成 CSV/schedule/metadata，再交给现有上传预览冻结；上传确认执行不重连 IBKR。只接受范围内完整的交易日线，单次最多 1,000 根。
- Execution 页支持从选定 IBKR 只读 profile 获取行情，并可继续上传用户自己的 Python 策略；连接测试结果显示环境、socket endpoint、只读状态和脱敏账户尾号。
- 验收：IBKR provider/local connector **44 passed**；新增 Python 上传行情准备 API/导入 **1 passed**；Execution UI **14 passed**、Connection Center 所在 Portfolio UI **9 passed**，production build 成功；`py_compile`、locale JSON parse 与 `git diff --check` 通过。假 provider/本地构建证据，不是实际 IBKR 连接验收。
- **边界：** 未连接 Gateway、未验证用户指定账户的环境、未读取真实账户或市场行情、未发送订单。历史数据发布时间和复权语义未认证。若使用 LIVE 账户，只能选 LIVE read-only profile；PAPER profile 仅接受 PAPER 账户。完成真实只读握手和行情验收需用户确认已登录 Gateway/TWS 及对应环境，且 API Read-Only 保持开启。

# B192 让 PAPER 恢复集成测试保留真实 STOPPING/inbox 语义并清理 fake worker（2026-10-22）

- 定位 B189 合并组中 4 个线程清理断言：失败 Cerebro 对应的 broker delivery 按设计保持未 ACK，持久 inbox 等待显式恢复；真实 runtime 因此保持 STOPPING、保留 owner lease 和 callback worker。此前测试把预期中的安全保留当作泄漏。
- 测试现在确认 fake broker 已关闭、原始 callback spool 已处理、未 ACK inbox 仍持久且 scope 为 STOPPING，再停止仅用于测试的 worker 以结束 pytest 进程；没有自动 ACK 或改写生产 recovery 逻辑。
- 恢复参数矩阵 `test_failed_recovery_sends_exact_created_intent_before_retrying_released_bar`：**6 passed**。execution/Backtrader/API/PAPER 合并组：**715 passed、2525 warnings、19 subtests passed，98.57 秒**。
- **边界：** runtime 的 STOPPING/lease retention 仍是安全 fail-closed 语义，正式恢复需要 reconciliation 和显式 recovery；这组仍使用 fake transport，不是 Gateway 证据。

# B191 修正执行页缺失字段默认值并对齐双入口报告摘要（2026-10-22）

- 保存的回测报告缺 engine/version/mode/data source 时，Execution 页不再显示静态样例的 Backtrader 版本或 `sma_cross` 版本；缺失值显示 Unknown。NL2 成功报告也明确显示完整 strategy version hash 与独立 replay 状态，避免只在 Python upload 卡片显示。
- 去掉 RSI 成功预览仍标成 SMA 的误导文案，统一称受支持的策略模板；新增报告缺字段用例和 RSI 文案断言。
- `cd frontend && npm run test:run -- src/pages/__tests__/Execution.test.tsx`：**14 passed**；`npm run build` 成功。Vite 仍提示 charts bundle 超 500 kB。

# B190 独立复算 trusted NL2 sizing metadata（2026-10-22）

- 独立 BackBroker replay 新增每个决策点的现金、权益、持仓数量、bar 收盘价和 session identity；数值以原始 float 字符串传递，避免 serialization rounding 改变整股 floor 边界。
- NL2 host 使用 frozen `StrategySpec` allocation 与 run 配置中的 budget/fee reserve/price buffer，再调用纯 Decimal sizing helper 重算 BUY/SELL 数量和原因；`target_shares`、`sizing_requested_qty`、`accepted_qty`、`requested_qty`、reason 或 sizing provenance 不一致时，在报告发布前拒绝。完整成功 integrity 的 `checked_fields` 增加 `trusted_template_sizing_metadata`。
- 定向 `./.venv/bin/python -m pytest agent/tests/execution/test_execution_backend_routing.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/bt_runtime/test_report_replay_subprocess.py -q --disable-warnings --tb=short`：**34 passed**；追加缺失 `target_shares` 负例后的 `test_execution_backend_routing.py` **19 passed**；SMA/RSI/accounting/python-upload/replay/routing 核心组 **54 passed**。真实 NL2 runner 负向注入未知/缺失字段、target shares、requested sizing qty、accepted qty 与 reason 变更均失败。
- **边界：** 此复算验证 sizing 和其执行请求，不重算 SMA/RSI 信号为何触发；decision log 同 order intent 协同篡改仍属 frozen source/host boundary，两个 Backtrader 运行仍有共因缺陷可能。无 Gateway/account/order 操作。

# B189 将 strict order schema 覆盖到 NL2 双策略回测（2026-10-22）

- verifier 的 order projection 默认采用封闭字段 schema；显式允许 trusted SMA/RSI template 输出的 `target_shares`、`sizing_requested_qty`、`accepted_qty` 等测算字段。NL2 runner 与 Python upload 正式回测都要求 strict shape。
- 新增真实 Backtrader NL2 runner 负向回归：注入 `broker_reported_realized_pnl=1_000_000` 后，report 在发布前被拒；现存 SMA、RSI 与 upload 正常回归通过。
- 定向 `./.venv/bin/python -m pytest agent/tests/execution/test_execution_backend_routing.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/bt_runtime/test_report_replay_subprocess.py -q --disable-warnings --tb=short`：**30 passed**。完整合并组：**706 passed、4 failed、2335 warnings、19 subtests、118.18 秒**；4 个失败均为 managed PAPER 恢复用例的执行线程清理断言。恢复参数化用例全集重跑 **6 passed**，整个 `test_managed_python_paper_session.py` 重跑 **36 passed**；组合负载下的失败原因仍待定位，因此不记合并组通过。
- Execution 页面回归 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：**13 passed**；`npm run build` 成功，产生 `Execution-*.js` 页面 chunk，Vite 提示 charts chunk 超过 500 kB。
- **边界：** strict shape 拒绝未知声称，但认可的模板 sizing metadata 目前由冻结源码和模板行为负责，不由 order-log replay 独立重算；共同 Backtrader/会计算法缺陷与同机 sidecar/config 篡改仍未解决，T44 PARTIAL。无 Gateway/account/order 操作。

# B188 Python upload 拒绝报告订单中未验证的额外执行字段（2026-10-22）

- 独立 replay 的 order projection 原先只比较白名单字段，但忽略报告订单行中的附加 key。现在 verifier 支持 strict order schema，`run_python_upload_backtest()` 正式路径启用 strict；额外执行字段返回 integrity failure，不发布 verified 报告。
- 真实 Backtrader Python-upload 报告回归将 `broker_reported_realized_pnl=1_000_000` 注入 order row，严格 verifier 拒绝；同组仍执行真实 BackBroker 与独立回放。`./.venv/bin/python -m pytest agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/bt_runtime/test_p1_report_accounting.py -q --disable-warnings --tb=short`：**10 passed**；`py_compile` 与目标 whitespace 检查通过。
- **边界：** 上传 worker 不持有 Broker/Analyzer 对象，故本次没有声称已覆盖任意直接篡改这些对象的攻击矩阵；trusted NL2 verifier 保持兼容既有 order projection。共同 Backtrader/会计算法错误、同机 report+sidecar 同主体篡改风险仍存在，T44 PARTIAL。无 Gateway/account/order 操作。
- 与 B187 一并复跑 execution/Backtrader/API/PAPER 相关组合：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：**709 passed、2239 warnings、19 subtests、67.71 秒**，退出码 0。

# B187 验证 event_result/checkpoint/outbox 中途事务失败可安全重交（2026-10-22）

- 只新增真实文件 SQLite 故障回归，不更改事务逻辑。`BEFORE UPDATE` trigger 在 `commit_result()` 已写入 event_result、outbox、checkpoint 后、更新 `strategy_event.committed_at` 时 abort；连接关闭/数据库重开后，run 仍 RUNNING、cursor/checkpoint 未前进、event_result/outbox 为空、event 仍 released 未 committed。移除 trigger 后相同 result 首次提交成功，重开再次提交返回幂等 `False`，只有一份 outbox。
- `./.venv/bin/python -m pytest agent/tests/execution/test_event_journal.py -q --disable-warnings --tb=short`：**24 passed**；`py_compile` 与目标文件 whitespace 检查通过。官方 SDK runtime：`ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -q`：**46 tests OK**，并核验独立环境的 ibapi 为 10.50.2。
- **边界：** 证明 SQLite 该事务语句失败后的原子回滚和安全重交，不覆盖任意 SIGKILL、OS 崩溃、磁盘损坏、提交结果不确定，也不证明券商网络 exactly-once。T42 仍 PARTIAL。

# B184–B186 加固版本 pin、Docker seccomp、日历日期与 callback 连续性（2026-10-22）

- **B184 PAPER 版本与容器 seccomp：** `PaperExecutionComposition` 拒绝缺失或非法 lowercase SHA-256 strategy version pin；start API 在调用授权器/runtime factory 前要求请求回显同一冻结版本。PythonSandbox 每次启动前检查 Docker daemon 报告唯一 builtin/default seccomp profile，并在 `docker run` 显式带 `seccomp=builtin`；真实 worker 隔离测试核验该 HostConfig。版本/路由定向 59 passed；首轮合并回归发现旧容器断言未列新 seccomp option，更新断言后真实 Docker 目标 1 passed。
- **B185 数据日历日期：** completed daily importer 核验 schedule 的 UTC 开/收盘换算回声明 timezone 后都仍属于 `session_date`；错日 schedule 与同步错日 CSV 均拒绝。market importer 14 passed；market-data contracts + routing 21 passed。
- **B186 PAPER callback 连续性：** Cerebro 策略线程失败并暂停 service 后，健康 callback worker 在安全暂停边界把 delivery sink 切为 no-op，继续把成交/撤单回报写入 ledger；durable inbox 保留未 ACK event，供显式 recovery 重放，避免向已死 PortBroker queue 堆积。真实 Docker timeout/partial fill/cancel 用例 1 passed，相邻 timeout + callback failure 组 2 passed。策略 broker notification 直到恢复才会重放。
- **合并验收：** `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：退出码 0，**708 passed、2251 warnings、19 subtests passed、67.36 秒**。首轮在新增 `seccomp=builtin` 后被既有 `SecurityOpt` 精确相等断言拦截；更新断言核验 no-new-privileges + builtin seccomp 后目标 Docker 用例及整组回归通过。相关 `py_compile` 与 16 个目标文件 whitespace/conflict scan 通过；`git diff --check` 也执行退出码 0（本轮目标在现有 untracked 工作树中，该命令不覆盖未跟踪文件）。
- **边界：** builtin seccomp 是当前 daemon/profile 配置证据，不证明 kernel/container escape 不可行或跨平台等价；callback 保持 ledger 入账和管理撤单，但旧 Cerebro 已退出，native BT notification 需显式 recovery 重放。交易日契约仍使用调用方/fixture schedule，不证明官方日历授权、行情真实性、许可或 point-in-time。无 Gateway、账户读取或真实订单操作。T07/T37/T41/T43 仍 PARTIAL。

# B182–B183 指定行情来源与双入口 PAPER 离线组合继续收口（2026-10-22）

- **B182 精确 YFinance 路径：** 新增 `YFinanceDailyProvider`，直接实例化 Vibe 的 YFinance loader 并核对 `name == "yfinance"`，避免 NL2 指定 `yfinance` 时进入通用 registry/fallback。宿主记录请求参数、缓存 hit/miss、loader identity/class、调用时间、loader 输出表 hash、归一化 OHLCV hash 与冻结 CSV hash。各 hash 的对象有明确区分；loader 输出 hash 不是 raw HTTP response hash。
- **B182 诚实 provenance：** manifest 的 host-observed fetch 明确将供应商身份认证、point-in-time 可用性、复权语义和 broker contract mapping 标为 `UNVERIFIED`；provider attestation 未被升级。Python upload 的 caller-declared provider 语义未变。
- **B183 Python-upload PAPER 组合：** 新增 `PythonUploadPaperSessionBuilder` 与 `build_frozen_python_upload_paper_composition()`。冻结 SHA/version、原始上传字节/hash、worker compiler、Backtrader 版本、explicit_quantity/整股/常规日线执行 policy、USD STK feed/conId 绑定及共享 runtime SQLite 都在装配时校验；事件由 `PythonEventCoordinator + PythonSandbox` 驱动既有 `ManagedPythonPaperSession`，deployment factory pin 同一 version。
- **验收：** YFinance adapter + market data + backend routing + Backtest API 定向组 **62 passed、1349 warnings、7.97 秒**；Python upload composition + deployment + PAPER routes 定向组 **42 passed、4 warnings**；完整 `agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py` **687 passed、2242 warnings、19 subtests passed、64.78 秒**。相关 `py_compile` 与 `git diff --check` 退出码 0。
- **边界：** 上述是宿主观察及离线构造证据，不认证行情供应商、许可、原始网络响应、point-in-time、调整或合约真实性。组合没有接入主应用 root；也没有启动 SDK、获取账户租约、读账户或发送订单。T07/T08/T14/T40/T41/T42 仍 PARTIAL。

# B179–B181 双入口版本、provider snapshot 与恢复 gate 收紧（2026-10-22）

- **B179 NL2 版本身份：** trusted backtest 把机器版本 `backtrader/{backtrader.__version__}` 写入不可变 `StrategyVersionRecord.engine_version`；报告保留展示标签。真实 Cerebro/BackBroker 生成的报告版本通过 `_validate_frozen_nl2_version()`，证明同一 NL2 回测版本可进入 PAPER composition 校验。
- **B180 provider snapshot：** `DataManifest` 新 schema v1.1 增加币种，保留 v1.0 历史读取；importer 与合成 SMA 都写明币种。`MarketDataSnapshot` 验证 bars 覆盖 range 两端及 warmup/evaluation 边界、available_at 不晚于 fetched_at，且 v1.1 source/provider、currency、adjustment 一致并只收 completed bars。
- **B181 恢复门：** `resume_paused_released_event()` 在同一事务读取 run scope；共享 DB 存在 execution_control 时，缺失/不支持的 gate 或状态非 RUNNING 都拒绝续跑。恢复后的精确 released event 仅在执行 service 已显式打开 gate 后才可重开。
- 验收：NL2 Backtrader routing、manifest/snapshot/importer、SMA/Python report replay、SQLite backup/event journal 定向组 **105 passed、548 warnings、7.42 秒**；完整 `agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py` **673 passed、2240 warnings、19 subtests passed、65.19 秒**。包含实际 Backtrader 并核对版本 validator，未以 mock 替代引擎。
- 边界：source/provider 匹配只是 manifest 内部自洽，不认证 vendor、许可、点时数据或货币权威；恢复 gate 检查不替代 owner lease/grant/reconciliation，仍由 managed runtime 负责。无 Gateway/account/order 操作。T07/T08/T12/T41/T42 仍 PARTIAL。

# B178 备份恢复时 journal 与账户 execution gate 一起 fail-closed（2026-10-22）

- `restore_execution_database_backup()` 在同一 SQLite 写事务中将 execution control 置为 `RECOVERING`，并将 `execution_run` 中 `RUNNING` 行全部改为 `PAUSED`；journal schema 不支持时回滚拒绝。
- 新增恢复后真实 SQLite 回归：正常身份读取、stage、release 均被拒；paused identity 只读可用；只有 exact released event id/seq/hash 经显式恢复接口验证后才重新 RUNNING。已调整恢复 outbox 检查为显式 `include_paused=True`。
- 验收：备份和事件日志专项 **30 passed**，0.49 秒。无外部连接或订单操作。
- 边界：本地恢复状态原子一致性得到验证；异机备份、操作员恢复流程与恢复后真实 broker reconciliation 仍缺。T34/T42 继续 PARTIAL。

# B177 Python 上传 UI/API 到真实 Docker worker、BackBroker 与报告读取闭环（2026-10-22）

- 新增 `@pytest.mark.integration` API 用例：在 `colima-vibe-execution` 上从固定 worker Dockerfile 构建镜像并固定 image ID，随后经 Python upload preview token 与 confirm API 跑真实隔离 worker。策略只在 evaluation session 通过当前 bar event 提出 1 股 BUY；BackBroker 下一根 bar 完成 fill，报告绑定不可变 Python source version，并由独立 BackBroker subprocess replay 验证；run detail API 读取 integrity sidecar 通过。
- 测试断言隔离 worker image digest、evaluation bar count、原始股数、BUY 完成状态、成交日/期末持仓、report version 与 integrity；不使用 FakeSandbox、fake runner、mock Backtrader 或券商 transport。
- 验收：Docker API 集成用例 **1 passed、17 warnings、4.53 秒**；与 market data、dispatch、Backtest API 全文件合并 **56 passed、1258 warnings、8.39 秒**；集成测试 `py_compile` 与 `git diff --check` 退出码 0。Execution UI `npm run test:run -- src/pages/__tests__/Execution.test.tsx` **13 passed**；`npm run build` 退出码 0（保留 Vite chunk/native loader 与 Node localStorage warnings）。
- 边界：行情与时段由受控 fixture 提供；证明 Python 上传 API/容器/BT/报告串接和离线隔离，不证明供应商真实性、broker PAPER、真实账户 facts 或 UI 浏览器到后端的完整部署网络路径。T12/T37 继续 PARTIAL。

# B174–B176 修复数据时间顺序、PAPER 合约币种边界并补 NL2 API 真引擎闭环（2026-10-22）

- **B174 数据时间自洽：** `import_completed_daily_csv()` 现在拒绝 `fetched_at` 早于最后/任一 bar 的 `available_at`。新增负例以及相等时间允许的边界例。该校验只限制 caller-declared 时间顺序，不认证 vendor timestamp。
- **B175 账户合约币种：** `BacktraderDispatchPort` 仅接受解析得到的 exact `USD` STK 合约；EUR 在 `allocate_order_id`、durable binding、dispatch/transport 前失败。测试断言 intent 保留 CREATED、ID 分配数为零、native/broker binding 不存在、transport 零调用。
- **B176 NL2 API 真 runner：** 新增 preview→confirm→GET run detail 的 FastAPI 集成用例，不替换 `run_backtrader_backend`。确认仅用 preview 冻结行情（provider fetch 一次），实际执行 Cerebro/BackBroker 与独立 replay，验证 frozen version、manifest bytes/hash、报告 sidecar 与 detail integrity。
- **验收：** `test_market_data.py + test_backtrader_dispatch.py` 首轮 **24 passed、77 warnings、1.53 秒**；API→真实 runner 新用例 **1 passed、514 warnings、2.76 秒**；最终三文件组合（含 allocator-call-count 断言）**55 passed、1242 warnings、6.40 秒**；相关文件 `py_compile` 与 `git diff --check` 退出码 0。
- **边界：** 使用确定性 fixture provider、真实 Backtrader/BackBroker 和本地文件/SQLite；无 vendor 认证、真实行情、IBKR Gateway/account/order 操作。T07/T08/T14/T24/T12 均按现有部分状态保留。

# B173 NL2 trusted-template 回测与 Python upload 统一 evaluation window（2026-10-22）

- Trusted SMA 年化收益改为仅按 manifest `evaluation_start..evaluation_end` 中的交易 bar 计算，与 Python upload 一致；报告增加 evaluation bar count 和显式 evaluation window provenance。equity curve 仍保留完整 feed 轨迹。
- Trusted runner 将 evaluation start 传入独立 BackBroker subprocess replay；verifier 比较 manifest 窗口及 bar count。新增测试覆盖手算年化收益以及篡改报告窗口/bar count 时 fail-closed；路由测试检查评价 bar 数。
- 验收：SMA/accounting/replay/backend routing 定向组 **30 passed、348 warnings、4.48 秒**；含 B171/B172、API/backend routing 与真实 Docker worker 的合并组 **92 passed、1353 warnings、39.86 秒**。合并命令误将 backend routing 文件重复列入，因此计数包含重复执行；所有用例通过。
- 边界：这统一了窗口定义和年化公式，不证明行情来源或点时真实性，不构成真实 PAPER 或 IBKR 证据；T07/T09/T38 仍 PARTIAL。

# B172 explicit_quantity 资金不足时不缩量，BackBroker Margin 可见（2026-10-22）

- 新增 Python upload 负向回测：worker 明确提出 200 股；在下一根 bar 的 $11 开盘面对 $1,000 BackBroker 现金时，原 quantity 保留，真实 BackBroker 状态为 Submitted→Margin，零成交/零费用/持仓不变。
- 报告 decision/order 保留 200 股原意图，独立子进程真实 BackBroker replay 校验通过。不会把这个离线 Margin 结果混同为 PAPER 账户风控拒绝或 broker 侧拒单。
- 验收：定向新用例 1 passed；`test_python_upload_backtest.py + test_sma_backtest.py + test_position_sizing.py` 41 passed；B171/B172 与 importer/API/Docker worker 组合 **75 passed、1115 warnings、39.14 秒**。详见 `ACCEPTANCE.md`。
- 边界：这是离线资金不足/数量语义证据，不证明真实账户保证金/权限/订单回报。明确数量不能缩放；不足时拒单状态必须可见。

# B171 importer 将 manifest 窗口边界绑定实际交易日（2026-10-22）

- `import_completed_daily_csv()` 现在要求 `warmup_start` 与实际首根 bar 相同，`evaluation_end` 与实际末根 bar 相同，且 `warmup_end`、`evaluation_start` 必须是导入日历中的 session。日期次序、包含关系仍由严格 `DataManifest` 校验。
- 新增首末截断和周末边界的 4 个负例，拒绝 boundary metadata 与实际 bars 不一致的 manifest。
- 验收：market-data importer 专项 **11 passed、77 warnings、1.68 秒**；连同 Python upload/service、report replay、API/backend routing 和真实 Docker worker 集成共 **75 passed、1115 warnings、39.14 秒**。
- 边界：这只提高 manifest/窗口完整性，不认证 provider claims、行情许可、point-in-time 可用时间、复权或真实数据来源；T07/T09 仍 PARTIAL。

# B170 managed session 启动失败统一持久暂停（2026-10-22）

- `start_paused()` 现在统一捕获启动期间异常并 best-effort 持久 `pause_run`；如果执行 scope 已进入 RUNNING，则在仍持有 owner lease 时关闸回 PAUSED。覆盖 snapshot 校验/shadow replay、outbox restore、Cerebro/PortBroker hydration、native-order restore、CREATED staging、callback barrier 与线程启动阶段。
- 晚期注入失败用例验证 journal=PAUSED、scope=PAUSED、owner lease 保留、零订单发送、session 不可直接 activate；failed start 需要重新构造/对账恢复。
- 验收：execution + bt_runtime + backtest API + PAPER routes **658 passed、1692 warnings、19 subtests，62.36 秒**；定向失败窗口 **1 passed**。完整命令见 `ACCEPTANCE.md` B170。
- 边界：PAUSE persistence 是 best-effort；如果本地 journal/ledger 本身不可写，不能声称成功持久暂停。此离线故障注入不证明真实 Gateway 故障恢复，T41/T42 仍 PARTIAL。无 Gateway/account/order 操作。

# B169 恢复时逐笔重建原生 Backtrader 成交历史（2026-10-22）

- managed session 从可信 ledger 读取 immutable fill rows，将 execution ID、数量、价格和 aware timestamp 规整成严格 `ReconciledFill`。在重建前校验 execution ID 唯一、按 UTC 时间/ID 顺序、数量汇总、加权成交价、最晚成交时间及费用汇总都与 active order aggregate 一致；任何一项不符继续 fail-closed。
- `PortBroker.restore_reconciled_order()` 按每笔成交逐次生成 Backtrader `executed.exbits`，并根据最终已对账持仓推导前序 cost basis；恢复不调用 sender。逐笔成交费用只有存在可信 per-fill facts 才能重建；当前 IBKR fill ledger 未绑定费用时仍以未确认/零分摊处理，不伪造佣金分布。
- managed failed-session recovery 集成现包含两笔不同价格和时间的历史部分成交，断言恢复后 exbits 逐笔相同，重复旧 execution 幂等，之后第三笔可完成原 native ref，零重发。
- 验收：execution + bt_runtime + backtest API + PAPER routes **657 passed、1696 warnings、19 subtests，61.71 秒**。详见 `ACCEPTANCE.md` B169。
- 边界：仍只支持规定的 BUY recovery 子集，真实 Gateway 订单/成交/费用 reconciliation 和进程崩溃矩阵未验收；T41/T42 仍 PARTIAL。无 Gateway/account/order 操作。

# B168 Python upload 回测强制区分 warmup 与 evaluation（2026-10-22）

- runner 现在要求 manifest 中 warmup/evaluation 日期有序且完整覆盖 feed；`warmup_end` 与 `evaluation_start` 必须是实际交易日，数据从 `warmup_start` 精确到 `evaluation_end`。
- warmup bar 仍逐根交给隔离 worker 建立策略状态，但宿主忽略其订单提案；只有 evaluation 区间的提案进入真实 BackBroker。报告新增冻结 evaluation window、evaluation bar 数和被忽略 warmup proposal 数。equity curve 保留完整 feed 时间轨迹，warmup 段因不允许下单而不影响组合；年化归一化仅使用 evaluation bar 数。
- 独立 BackBroker replay 接收相同 `evaluation_start`，使用相同期间归一化，并核对 manifest 日期范围、evaluation bar 数和报告执行结果。
- 验收：`agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py` **123 passed、1209 warnings，9.69 秒**；边界定向 2 passed；`py_compile`、`git diff --check` 通过。完整证据见 `ACCEPTANCE.md` B168。
- 边界：此切片修复 Python upload 回测范围；不认证真实行情来源，也不改变 NL2 模板端的 evaluation 语义。provider provenance、官方日历授权与真实 PAPER 仍未验收。无 Gateway/account/order 操作。

# B167 coordinator 在 staging 前复核冻结策略的 completed bar（2026-10-22）

- `PythonEventCoordinator` 现在在接收新 bar 前核对 frozen StrategySpec 的唯一 instrument、USD、时区、`us_equity_rth`/XNYS 日历身份、纽约交易日及该 session 精确 UTC 开收盘（包括半日市），并拒绝晚于可信 clock 的 `available_at`。任何不匹配都会持久 PAUSE，且不 stage event、不调用 worker。
- durable released event 和 committed-prefix shadow replay 也重验同一 bar contract；损坏事件先暂停，不进入 worker。Python 与 managed PAPER session 将同一 trusted clock 绑定到 feed/coordinator，避免时间判断分叉。
- 验收：execution + bt_runtime 全集 **597 passed、1016 warnings、19 subtests，57.99 秒**；两个真实 Docker worker 集成回归 **2 passed、17 warnings、8.01 秒**。详见 `ACCEPTANCE.md` B167。
- 边界：calendar 来自 pinned XNYS package，bar 来源仍是测试构造/既有 feed；本切片不认证供应商 provenance、授权/point-in-time 数据或 corporate action，T07/T09 仍 PARTIAL。真实容器运行没有连接 Gateway/account 或发单。

# B166 将 NL2 澄清轮次持久化为可校验 transcript（2026-10-22）

- **服务端记录：** 每次 trusted-template preview 创建本地 SQLite transcript，记录完整策略描述；澄清请求必须携带 transcript ID、父 turn ID、新 turn UUID 与答复。服务端核对回传全文必须精确等于父描述加本轮答复，并将 turn 以不可更新/删除的链式记录追加。preview 上下文 hash 固定 symbol、instrument、日期区间、source 与 allocation，跨上下文续写返回 409。
- **重试和范围：** 同一最新 turn ID 与同内容重放幂等；ID 内容冲突、旧 parent、篡改全文、换标的/预算上下文均 fail-closed。turn 上限 32、单答复 512 字符、完整描述 4096 字符，数据库文件设为 0600。前端保存 cursor，网络失败后沿用同一 turn ID；用户编辑后需重新 preview。确认创建时仍使用原始策略 payload 和当前 preview token，不将 transcript 元数据交给回测运行器。
- **验收：** `./.venv/bin/python -m pytest agent/tests/execution/test_strategy_clarification_transcripts.py agent/tests/test_backtrader_backtest_api.py -q --disable-warnings --tb=short`：36 passed、656 warnings、5.40 秒；`npm run test:run -- src/pages/__tests__/Execution.test.tsx`：13 passed；`npm run build` 成功；`git diff --check` 通过。详见 `ACCEPTANCE.md` B166。
- **边界：** T12 仍 PARTIAL。日志本地持久化但未绑定可归因 actor、没有 retention/删除流程或多用户隔离验收，也不证明模型完整识别任意自然语言中的约束、否定或撤销；真实 provider 仍未验收。无 Gateway、账户或订单操作。

# B165 CREATED intent 只有成交后才释放 retry bar（2026-10-22）

- **收紧恢复边界：** B164 将 FILLED/CANCELLED/REJECTED 都视作可推进的终态；但本地收到取消或拒绝后仍可能出现迟到 execution。现在唯一 exact-linked CREATED BUY 仅在 durable ledger 到达 FILLED 后恢复原 retry event。CANCELLED/REJECTED 不释放事件，而是抛出受控恢复错误，走策略线程失败路径持久暂停，要求 fresh reconciliation。
- **验收：** `test_failed_recovery_sends_exact_created_intent_before_retrying_released_bar` 扩展为 fill/cancel/reject 三个 broker 结果，各配风险通过/拒绝，共 **6 passed、43 warnings、1.74 秒**；managed-session + `PortBroker` + dispatch 回归 **58 passed、259 warnings、9.35 秒**。进程内 pytest runner 均退出码 0，`LIVE_THREADS []`。cancel/reject 分支确认 journal released event 与 checkpoint 不变、sandbox 不再调用。
- **边界：** 未验证真实 IBKR 迟到 fill、真实 Gateway 对账或取消竞态；活动单与 CREATED intent 同场恢复仍拒绝。仅本地 SQLite/Cerebro 与 fake transport，无 Gateway/account/真实订单操作；T41/T42 仍 PARTIAL。

# B164 failed-session recovery 同时保留前序 CREATED intent 与 released-bar retry（2026-10-22）

- **实现：** managed recovery 现在允许一个受限组合：前序 committed proposal 已经生成唯一、同 run/version/account/deployment 且 exact outbox-linked 的 CREATED BUY；下一策略 bar 已 durable released 但尚未 commit。恢复仍先 fresh reconcile、shadow replay 与 outbox handoff 核验，保持 PAUSED，重建临时 native order 并暂存两个持久身份。若同时存在其他活动订单或 unresolved send，仍拒绝该组合。
- **顺序与闸门：** 独立显式 activate 打开 service gate 后，Cerebro broker cycle 先将 CREATED intent 交给现有 dispatch 和二次风控。retry event 不提前重试或调用 worker；SEND_STARTED/UNKNOWN、ACKNOWLEDGED/PARTIALLY_FILLED 都不会释放它。只有 durable ledger 到达 FILLED/CANCELLED/REJECTED，且 service 仍 RUNNING、lease 仍有效时，Cerebro 线程才恢复原 released event identity 并推入对应 bar。风险拒绝使 session/journal/runtime PAUSED，intent 留在 CREATED，event 不提交。
- **验收：** 新组合用例分别覆盖二次风险通过与拒绝：**2 passed、23 warnings、1.07 秒**；同一 runner 对 partial BUY + 未 ACK callback recovery、此新组合及 B162 八种 stage/activate/stop 组合运行 **11 passed、61 warnings、2.63 秒**。完整 managed-session + `PortBroker` + dispatch 三文件回归 **54 passed、247 warnings、8.44 秒**。execution + Backtrader runtime + backtest API + PAPER routes 合并组 **634 passed、1665 warnings、19 subtests、60.41 秒**。各 pytest runner 退出时 `LIVE_THREADS []`；`py_compile`、`git diff --check` 和交接文档 whitespace 检查通过。
- **边界：** 使用真实 SQLite/Cerebro 与 fake IBKR transport。只支持唯一 exact-linked CREATED BUY 和一个 released bar，不支持多 CREATED、SELL、其他 retry/event 队列或非终态订单结果下继续策略；不表示真实 Gateway 会回报指定终态，也没有验证真实账户对账、进程硬杀或 Docker worker crash。T41/T42 仍 PARTIAL；无 Gateway/account/真实订单操作。

# B163 安全停止保留唯一未发送的精确链接 CREATED intent（2026-10-22）

- **实现：** managed session stop 先关闭 PAPER send gate 并持久暂停当前 strategy journal，再验证账本恢复状态。若唯一剩余项是 B162 支持的同 run/version/account/deployment、精确 linked 的 CREATED BUY，则允许停止 Cerebro、callback worker 与 runtime；intent 继续保留为 CREATED。该路径不生成 native order、不 reserve/send，也不把 intent 伪标成 CANCELLED。其他活动/未决/不支持订单状态仍阻止安全停止。
- **验收证据：** 为 B162 staging 新增 stop-before-activation 维度后，8 个初始 journal 状态、二次风控结果与停止时机组合此前已在进程内 pytest runner 验收为 **8 passed**，并确认目标 callback/Cerebro 线程退出；本轮未重跑测试。随后的独立 pytest 子进程尝试未正常退出，故不将其计作新的通过证据，也未据此更新更大回归数。
- **操作语义：** CREATED 表示本地意图尚未 reserve/send，券商侧没有可撤销的订单。停止后记录仍可见；未来 session 必须重新 fresh reconcile、核对 committed outbox link，并再次等候独立显式 activate 和二次风控。若其他非安全状态存在，停止会失败并保持 gate/journal PAUSED。
- **外部边界：** 没有连接 Gateway、读取账户或发送真实 PAPER/live 订单。T41/T42 保持 PARTIAL；此切片只关闭 B162 intent 的 safe-stop 缺口，不声称完成跨重启 native ref 或 broker reconciliation。

# B162 恢复唯一已链接 CREATED intent 并在显式激活后重建 native order（2026-10-22）

- **实现：** fresh reconciliation、committed-prefix shadow replay 和 outbox handoff 后，managed session 只接受一个与当前 run/version/account/deployment 精确匹配、由完整 committed proposal 链接的 CREATED intent。Cerebro 尚未启动时将它暂存在 PortBroker；PAUSED 时不创建 native order、不 reserve/send。显式 activate 后，由 Cerebro broker thread 创建临时 native order，通过现有 one-shot resolver、`BacktraderDispatchPort`、二次风控、持久 SEND_STARTED 与 trusted PAPER transport；不重放策略事件或重复调用 authorizer。
- **拒绝边界：** 未链接/跨 scope/跨版本 intent、多个 CREATED、和 failed-bar retry 混合、其他 ready-to-send/unresolved 状态仍 fail-closed。只有 exact linked BUY/MKT/DAY 支持。新风险检查拒绝时仍为 CREATED、不发送，Cerebro 故障路径持久暂停 run 与 execution scope。临时 Backtrader ref 不跨重启持久化；需再恢复时仍依赖 fresh reconciliation/outbox link，且不推断先前 ref。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/execution/test_managed_python_paper_session.py agent/tests/bt_runtime/test_broker_proxy.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings --tb=short`：**48 passed、327 warnings、10.26 秒**。覆盖初始 PAUSED/RUNNING journal、启动时零发送、显式 activate 后恰好一次 fake PAPER 发送，以及二次风险拒绝时零发送并暂停。组合命令 `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：**628 passed、4597 warnings、19 subtests、95.41 秒**。首轮同命令有一个既有 2 秒 sandbox helper timeout；独立复跑 1 passed、0.33 秒，第二轮全组通过。`py_compile` 和 `git diff --check` 通过。
- **外部边界：** 所有发送证据使用 fake subprocess transport、本机 SQLite 和测试构造的 PAPER facts/grant；没有读取 Gateway/account 或发真实 PAPER/live 订单。T41/T42 仍 PARTIAL；生产 identity/root、facts provider、Gateway crash/open-order 对账和多 CREATED/SELL recovery 仍未完成。

# B161 增加 SQLite-backed PAPER 管理授权存储（2026-10-22）

- **新增：** `PaperControlGrantStore` 将可信部署 root 预置的管理 grant 持久到 SQLite。每条 grant 精确绑定可归因 subject/auth method/tenant、IBKR PAPER account scope、单一 `start/activate/recover/cancel/stop` 动作与半开有效期，并记录 issuer 身份；撤销后保留审计记录且重开数据库后仍生效。读取异常、错 actor/scope/action、共享 key 主体、过期或撤销均拒绝。manager 可直接将 store 注入既有 `control_authorizer`，授权在 start runtime factory 前执行。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_control_grants.py agent/tests/execution/test_paper_execution_manager.py --disable-warnings --tb=short`：最终版本退出码 0，30 passed、0.46 秒。覆盖 SQLite reopen、actor/tenant/account/action 隔离、起止边界、撤销、无授权 factory=0，以及共享 key 在 runtime factory 前拒绝。
- **组合回归：** `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：退出码 0，626 passed、1607 warnings、19 subtests、58.71 秒。新 grant 源码/测试 `py_compile` 和 `git diff --check` 通过。
- **边界：** grant 签发是仅供可信部署/控制面调用的代码接口；本轮没有 HTTP 签发/撤销端点，也没有真实可归因 identity provider 或生产部署 root。不能通过共享 key/loopback 为真实人创建授权。store 未与线上 app composition 接通，生产 API 仍默认 inert。T13 继续 PARTIAL；无 Gateway、账户或订单操作。

# B160 为 NL2 不完整策略加入结构化多轮澄清（2026-10-22）

- **实现：** 受信任 SMA/RSI preview 在需求缺项时保留旧 `supported/clarification/reason` 响应，并新增版本化 `clarification_form`：状态、静态问题和服务端允许的缺项字段；已知不支持条件明确标为 `unsupported` 且不给可补字段。请求仍 `extra=forbid`，不接受客户端提交 spec、参数、证据或会话状态；只有解析成完整 StrategySpec 并通过验证后才抓取行情和签发绑定冻结 spec/行情的 preview token。描述上限提升至 4096 字符以容纳原文和多轮用户补充。
- **UI：** Execution 对缺项展示问题/字段和答复框；答复以 `User clarification:` 追加进完整用户描述后重新调用同一 preview API，连续多轮保留全文且总长受 4096 字符限制。unsupported 与旧版纯字符串响应继续阻断并允许用户编辑。NL/Python 入口互相作废旧 preview，单调请求 generation 丢弃过期异步响应，确认使用当前预览保存的精确 payload/token。中文请求的结构化问题/字段标签使用中文。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py --disable-warnings --tb=short`：69 passed、656 warnings、6.49 秒；Execution UI 定向 13 passed；`npm run build` 退出码 0（Vite configLoader 与大 chunk warnings）；相关 execution + Backtrader + API 合并组 632 passed、1626 warnings、19 subtests、57.84 秒；py_compile 与 `git diff --check` 通过。
- **边界：** T12 仍 PARTIAL。答案当前由前端作为完整原始描述逐轮回传，服务端每次重跑完整解析、引文数值/角色检查和 deterministic unsupported prefilter；没有 durable conversation/transcript store 或逐轮 turn ID provenance，也不能证明模型发现任意复杂自然语言中的所有约束。支持预览仍需用户审阅完整原文与规则卡。离线/fixture 解析测试不等于实际 provider 全面准确。没有连接 Gateway、读取账户或发送订单。

# B159 恢复已提交但未完成的 proposal→ledger handoff（2026-10-22）

- **故障窗口：** Python worker 已原子提交 event result/checkpoint/outbox proposal，但进程在 handoff 前退出。启动时对账完成并通过 committed-prefix shadow replay 后，managed session 现在会从 RUNNING 或 PAUSED journal 读取并逐个幂等完成该 run 的 outbox handoff；prepared RESERVED intent 重用原冻结决策和 intent，PENDING proposal 重新走可信 authorizer。Journal 为 prepared intent/decision 保存 SHA-256，partial content corruption 会先持久 pause；跨恢复仍重新核对 prepared payload 与 event proposal/run identity。
- **安全边界：** 此恢复仅调用 `create_intent` / `link_outbox_intent`，不会 reserve 或 sender。若获批提案生成新的 CREATED intent，session 因没有可恢复的 Backtrader native order ref 继续保持 PAUSED 并拒绝启动；账本和 outbox 此时已一致，后续不能重复授权或建第二个 intent。此 slice 不伪造策略订单引用或自动越过新 send 风险检查。
- **验收：** managed-session/journal/outbox 定向组合 **64 passed、197 warnings**（含 RUNNING/PAUSED 启动、prepared row 哈希/身份损坏拒绝），startup case 断言 committed prefix、checkpoint、cursor 不变，outbox linked、ledger 单个 CREATED、authorizer 只调用一次，reserve/send 均为零；重试不会重复创建。execution + Backtrader runtime + API 合并组 **615 passed、1619 warnings、19 subtests、58.73 秒**。真实 Docker worker B158 shadow replay 集成复跑 **1 passed、2.69 秒**，三事件输出一致，重放前后 journal/cursor/outbox 不变且无派单。
- **边界：** PENDING proposal 若授权为拒绝，会持久记录拒绝并可继续；获批的新 CREATED intent 暂不支持原生 Backtrader order/ref 重建，因此仍停在 PAUSED。真实 Docker worker kill 后恢复、Gateway/真实账户及所有 dispatch crash boundary 仍未覆盖，T41/T42 继续 PARTIAL。无 Gateway/account/order 操作。

# B158 为 PAPER managed session 增加 committed-prefix shadow replay（2026-10-22）

- **Journal：** `execution_run` 新增 nullable `initial_checkpoint_json`，新 run 将 seed 与当前 checkpoint 分开持久化；历史数据库无法推导种子时，对已有 committed prefix fail-closed。新增只读 `read_committed_prefix()`，校验 seq 连续性、event/result identity、canonical JSON 与 SHA-256、committed checkpoint 和逐项 outbox proposal 映射，并排除 cursor 后结果/越界事件。
- **Shadow replay：** `PythonEventCoordinator.shadow_replay_committed_prefix()` 只读取暂停/待启动 run 的 durable prefix，以冻结 Python version/compiler、原始 completed bar 和 seed state 调用隔离 worker；逐项比较完整 `WorkerEventResult` 和最终 checkpoint。不会 commit、推进 cursor、创建 outbox、handoff 或 dispatch。managed session 在 fresh snapshot reconciliation 后、启动 Cerebro 前，以及失败恢复重建前调用；差异持久 PAUSE 并阻止启动/恢复。
- **验收：** event journal/worker/managed session 定向组 **58 passed、205 warnings**；execution + Backtrader runtime + Python upload/PAPER API 合并组 **610 passed、1615 warnings、19 subtests、56.06 秒**。新增三事件非空 seed 的只读重算、结果中段被合法重哈希仍被重算发现、历史库无初始 seed fail-closed；恢复测试核对重算历史不会产生额外 outbox/send。
- **边界：** 这是 fake sandbox + 真实 SQLite/Cerebro managed-session 的 Python event API 证据，不证明任意 Python/native Backtrader 指标状态、真实 Docker worker crash 后重建、Gateway/账户恢复或真实 PAPER。T41 仍 PARTIAL；无 Gateway/account/order 操作。

# B157 Python upload 日历必须匹配可信 pinned XNYS schedule（2026-10-22）

- **修正：** Python upload API 不再把客户端 `expected_sessions` 作为日历事实。入口强制 `XNYS@4.13.2` 与 `America/New_York`，以 metadata 的 warmup_start 至 evaluation_end 生成宿主 schedule，逐项核对日期、开收盘时刻及 early-close；若请求带 schedule hash，也必须匹配。导入器实际使用宿主 schedule，因此 manifest schedule hash 绑定 pinned 日历。通用 CSV importer 保持不变。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/test_backtrader_backtest_api.py --disable-warnings --tb=short`：25 passed、656 warnings、4.94 秒；`./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_market_data.py agent/tests/execution/test_execution_backend_routing.py --disable-warnings --tb=short`：20 passed、184 warnings、4.10 秒。负例覆盖伪造假日、开盘时间、半日市标记、calendar version、时区和 schedule hash；preview 不建 run 或 version。
- **边界：** XNYS 日历包提供可重演的 pinned schedule，不认证交易所官方授权/更正、供应商行情许可、复权或 point-in-time 可用性。T07/T09 仍 PARTIAL；没有 Gateway、账户或真实订单操作。

# B156 增加显式 NL2 PAPER app composition factory；默认 API 仍 inert（2026-10-22）

- **新增：** `build_frozen_nl2_paper_composition()` 要求调用方提供 IBKR/PAPER scope 与 runtime config/dependencies、当前安装的 frozen trusted NL2 SMA/RSI version、固定 run/deployment、risk providers、完成 bar feeds、conId/contract resolver、control authorizer 与 clock；校验 version hash/provenance/compiler/Backtrader/policy 及 scope/config account 一致后，组装 readiness-ready manager/composition。
- **安全边界：** runtime callback 在 session callback router 安装前 fail-closed。factory 不启动 SDK/process、不获取 lease、不连接 Gateway，也不会自动注册到主 app；API 默认 composition 继续 inert，必须由明确的可信部署 root 显式安装并提供真实事实源。
- **验收：** `./.venv/bin/python -m pytest agent/tests/execution/test_nl2_paper_composition_factory.py agent/tests/test_state_migration_wiring.py::test_api_lifespan_default_paper_composition_remains_inert -q --disable-warnings`：10 passed、1 warning；专项两文件 `py_compile`、`git diff --check` 通过。
- **边界：** 这是完整离线 assembly seam，不是已配置生产 app root、可归因身份/grant 存储、权威账户/行情/风险 provider 或真实 PAPER 证据；T14/T16/P3b/T40/T43 仍 PARTIAL。

# B155 PAPER 活动改为持久序列补传并接入 Execution SSE（2026-10-22）

- **后端：** order intent/state/reservation/send、broker report/fill、fee 和 execution control 写入，在同一 SQLite 事务内追加 scope-local sequence event 与 cursor；事件表阻断 UPDATE/DELETE。manager 只按当前可信 active scope 读取，read-after-seq 最多 100 条。SSE 复用 `require_event_stream_auth`，游标优先级 header `Last-Event-ID`、同名 query、`after_seq`；无 cursor 从 tail 开始，检测序号缺口发 `activity.gap` 并关闭，数据来自 SQLite pull 而非进程内 EventBus。
- **前端：** REST activity snapshot 提供 `latest_activity_seq`；Execution 页面在 active lease 下从该序号订阅 SSE，事件后刷新脱敏活动投影；显示连接中、实时连接和中断重连状态，原有 10 秒快照轮询保留为 UI 回退。现有 useSSE 的 query Last-Event-ID 重连格式由路由兼容。
- **验收：** ledger/manager/routes 定向组 74 passed、1 warning；前端 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：11 passed（含 snapshot cursor 订阅与事件触发 refresh）；`npm run build` 成功，保留既有 Vite configLoader 与大 chunk 警告。全 execution + Backtrader + API 回归 `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：607 passed、1563 warnings、19 subtests、55.91 秒。
- **边界：** 当前无 retention/compaction，因此 durable contiguous cursor 可从数据库重开后继续；UI 的 event→REST 刷新不是直接渲染完整订单事件投影，且 10 秒轮询会继续运行。已覆盖状态提示和 snapshot cursor 订阅，但未完成真实浏览器断线/跨服务进程重启演练、活跃运行时延/背压/长连接部署验收；默认 app inert，未连接 Gateway、账户或发送真实订单。T29 仅部分推进，T30 仍 PARTIAL。

# B154 T39 直接探测本次运行的宿主数据与事件文件（2026-10-22）

- **新增探针：** 扩展真实 Docker worker + importer + Cerebro future-only canary 集成。测试先在宿主持久化本次回测的 `market_data.csv`、含随机未来 canary 的 `verified_bars.json` 和 `backtest_events.sqlite3`，再把这些精确路径作为恶意策略输入；首根 completed bar 事件逐个尝试直接读取，断言 worker 均不可读。宿主侧在每次真实 worker 调用前断言文件已存在且 canary 确实位于 CSV/verified bars 中，避免“文件未创建”造成假通过。
- **launcher 契约：** `test_command_is_hardened_and_contains_no_host_mount_or_env` 现额外断言不使用 host PID、host IPC、host network 或 device 选项；已有 mount/env/privileged 拒绝断言保留。
- **验收：** `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_worker_cannot_introspect_future_bar_canary_from_first_cerebro_event --disable-warnings --tb=short`：1 passed、8 warnings、2.50 秒；`test_command_is_hardened_and_contains_no_host_mount_or_env` 已在后端定向组通过；当前改动后的 execution + Backtrader + API 回归 **589 passed、1562 warnings、19 subtests、54.40 秒**。
- **边界：** T39 仍 PARTIAL。直接路径读取被拒绝，补强了本次运行文件面证据；有界对象图探针仍不覆盖任意 Python 内省、所有 Docker/内核逃逸、host/daemon 故障或完整游标攻击面。仅真实 Docker + 本地合成 fixture，无行情供应商、Gateway、账户或订单活动。

# B153 PAPER 执行页展示受 scope 限定的持久活动（2026-10-22）

- **后端：** `SQLiteOrderLedger.list_activity_for_scope()` 返回可信 broker/account/environment 下最近 1–100 笔委托，包含终态委托、成交与费用；`PaperExecutionManager.activity_snapshot()` 只读取当前 active runtime 内部绑定 scope，附恢复队列计数、账本 reservation 摘要和持仓来源状态。意图、成交、费用响应均裁掉 account_ref、broker/environment 等重复身份字段；API 不接受调用方指定账户或环境。
- **API/UI：** 新增认证只读 `GET /api/execution/paper/activity?limit=50`。Execution 每 10 秒读取 activity 与 status，显示近期订单/成交、未确认费用、恢复队列和 reservation。只有 runtime 提供可验证的 `latest_reconciled_snapshot()` 才显示券商持仓；否则明确显示 unknown；实时风险限额一律标为 unavailable。
- **验收：** 后端定向 ledger/manager/API：65 passed；包含 sandbox launcher contract 的后端组合 80 passed。最终 execution + Backtrader + API 合并回归 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：589 passed、1562 warnings、19 subtests、54.40 秒。前端 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：10 passed；`npm run build` 成功，保留现有 Vite configLoader 与大 chunk warnings。B154 的真实 Docker path probe 1 passed。未连接 Gateway/账户。
- **边界：** 该页面读取 ledger history 和 manager 可验证快照，不是实时推送；T29/T30 SSE 断线补序、队列背压和端到端时延仍未验收。活动 API 没有生产 app composition 或真实账户源，因此当前不能据此认为存在真实 PAPER 活动。

# B152 trusted NL2 managed session builder 离线 composition（2026-10-22）

- **新增：** `agent/src/execution/trusted_template_paper_session_builder.py` 把显式注入的 `StrategyVersionStore` / immutable version ID、固定 run/deployment identity、proposal 与 dispatch risk providers、completed-bar feed、conId/feed map、contract identity resolver 组装为 `TrustedTemplateEventCoordinator`、host `TargetFractionPaperSizer`、风险 handoff、`PaperOrderDispatchService` 与 `ManagedPythonPaperSession`。session journal 与 broker ledger 复用 runtime 唯一 SQLite；默认 dispatch sender 明确拒绝绕过 managed Backtrader port。
- **前置验证：** 仅允许 frozen `sma_cross@1.0` / `rsi_threshold@1.0` trusted source、当前 compiler/Backtrader 和规定的 RTH 日线、Market/DAY、long-only 整股 target_fraction policy；显式校验 IBKR/PAPER scope、run/deployment/version identity、provider 返回的 grant/account/quote/instrument/version、唯一 feed/合约映射和 runtime 数据库。缺失或 mismatch 在创建 durable run 前拒绝。builder 创建对象不启动 SDK、持有 lease、调用 account/quote risk providers 或发送订单。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/execution/test_trusted_template_paper_session_builder.py`：16 passed（最终专项）；同次整合执行 + BT runtime + backtest API + PAPER routes 命令退出码 0，576 passed、1562 warnings、19 subtests、55.52s（完整组包含 builder 的前 11 个测试；新增 5 个 constructor 参数负例在整合组后单独复跑，已包含于 16 passed）。`py_compile`、`git diff --check`、新增文档 trailing whitespace 检查通过。
- **边界：** 这是可供受信 app composition root 注入的 NL2 composition builder，不是生产 app root/授权存储/版本选择 UI；grant、事实 provider、feed 和 contract resolver 仍由调用方注入，当前无生产实现。未启动 runtime、未连接 Gateway、未请求真实账户/行情、未发送订单。T14/T16/P3b/T41/T43 保持 PARTIAL。

# B151 RSI native-order 与 immutable deployment pin 离线切片（2026-10-22）

- **RSI 订单闭环：** `agent/tests/execution/test_managed_python_paper_session.py` 增加 frozen `rsi_threshold@1.0` 托管会话端到端回归。completed daily bars 经 trusted Wilder checkpoint coordinator 释放；RSI 下穿 30 后由 host `target_fraction` sizing 产生 19 股 BUY，经真实 Cerebro/PortBroker native order、fake IBKR execution callback、durable inbox 与账本闭环；更新成交事实后，上穿 70 再产生完整策略持仓 19 股 SELL。断言 native position 归零、订单无 active/unresolved 状态、callback inbox 排空，之后正常 stop。
- **版本绑定：** `ManagedPythonPaperDeploymentFactory` 可 pin 到 immutable `strategy_version_id`，并在 session 构造后从其 version store 复核 ID/hash；`/api/execution/paper/start` 若 factory 有 pin，只接受请求回显同一版本 ID，不允许 HTTP 请求选择另一版本或账户；status 暴露 pinned version。缺 pin 时保留原兼容行为，可信 app root 仍需显式构造和配置 factory。
- **验收：** RSI 定向 `./.venv/bin/python -m pytest -q agent/tests/execution/test_managed_python_paper_session.py::test_managed_session_runs_frozen_nl2_rsi_entry_and_exit_through_native_orders --disable-warnings --tb=short`：退出码 0，1 passed、115 warnings、1.59s；SMA+RSI 邻接组合退出码 0，2 passed、129 warnings、1.33s。部署/API 定向（cwd=`agent`）`./.venv/bin/python -m pytest -q tests/execution/test_managed_python_paper_deployment.py tests/test_paper_execution_routes.py`：退出码 0，27 passed。整合回归 `./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings --tb=short`：退出码 0，565 passed、1560 warnings、19 subtests、55.21s。相关 py_compile 与 `git diff --check` 通过。
- **边界：** 这是离线 fake runtime/transport + 真实 Cerebro/PortBroker/SQLite；版本 pin 是可信 composition 可用的 seam，不是已完成的 app composition root。仍缺真实 identity/grant、version store 与部署构造接线、account/quote/risk providers、跨重启 NL2 活动单恢复和真实 IBKR PAPER。没有连接 Gateway、账户、行情服务或发送真实订单；T14/T16/P3b/T41/T43 保持 PARTIAL。

## B150 managed PAPER 增加 trusted NL2 SMA/RSI event coordinator 与 target_fraction sizing（2026-10-22）

- **新增：** `TrustedTemplateEventCoordinator` 只接受当前安装的 `sma_cross@1.0` 与 `rsi_threshold@1.0` 固定版本；版本 source hash、模板 artifact bytes、compiler version、Backtrader engine version 均与可信 runtime 匹配后才逐 completed bar 计算信号。SMA checkpoint 保存最近必要收盘价；RSI checkpoint 保存 Wilder seed/平均 gain/loss、前收盘价与前一 RSI，避免无界累积历史。event result、checkpoint 和 order outbox 复用既有 SQLite journal。
- **目标比例：** 模板只产生 BUY/SELL 信号，不带用户侧股数。`TargetFractionPaperSizer` 从 host `ProposalRiskInputsProvider` 读取匹配账户/版本/标的的 grant 与完整新鲜账户报价事实，按 allocation fraction、grant strategy budget、现金 reservation、费用与价格 buffer 调用既有 Decimal sizing；然后原有 `PythonOutboxIntentHandoff` 再次取事实做精确数量风险授权。
- **托管组合：** `ManagedPythonPaperSession` 仍复用单一回调路由、账户快照 hydration、Cerebro、PortBroker、SQLite outbox/ledger 和 `PythonPaperEventBridge`，现按 source kind 选择严格隔离 Python coordinator 或 trusted NL2 coordinator。真实 Cerebro + frozen SMA version + `target_fraction=0.2` 的集成回归，经 fake IBKR callback 完成 180 股 native BUY；独立 sizing 测试确认 buffer 后 Decimal floor 为 19 股，并拒绝过期 quote/account、错 version facts，以及策略 reservation 同时包含于 account reservation 时的现金/目标股数计算；模板 artifact 篡改在 event release 前被拒。RSI checkpoint state machine 覆盖 oversold entry 与 overbought exit。定向组合 **9 passed、49 warnings、0.87 秒**；完整 execution + Backtrader runtime + API 回归 **542 passed、1492 warnings、19 subtests、54.50 秒**；`py_compile`、`git diff --check` 通过。
- **尚未完成：** SMA 已完成真实 Cerebro/native order/fake callback 组合；RSI 目前只有 checkpoint 状态机的阈值穿越回归，未完成 native PAPER order 与恢复组合验收。无生产 NL2 deployment composition/API/UI 选择固定版本、无真实风险/行情 provider 与纸账户配置；当前成功证明基于 fake transport，非真实 IBKR paper。T14/T16/P3b/T41/T43 与整体目标继续 PARTIAL；本轮未连接 Gateway、读取账户或发送真实订单。

## B149 Python PAPER 事件桥支持经过 host 授权的 SELL/长仓退出（2026-10-22）

- **修改：** `PythonPaperEventBridge` 现在允许 BUY/SELL 两种合法 long-only proposal，并在 durable handoff 已生成与 proposal 完全一致的 intent 后分别提交 Backtrader native `buy` / `sell`。未知方向仍 fail-closed；卖出数量与账户/策略持仓是否合法继续由 host proposal risk authorizer 决定，intent 字段由 bridge 再逐项核对。
- **故障用例：** 真实 Cerebro + `PortBroker` + fake IBKR transport 证明持仓 2 股并获 host authorization 的 SELL 2 以 native SELL 成交并由 callback 落 ledger FILLED；持仓事实仅 1 股而 proposal 要求 SELL 2 时 host 拒绝，未创建 reservation/发送并将 service pause。定向 bridge/session/deployment 组 **7 passed**。
- **相关回归与修正：** 全 execution + Backtrader runtime + backtest API 命令 **534 passed、1474 warnings、19 subtests、53.32 秒**。该组合复跑还发现一个部署失败测试仍期待 RECOVERING（runtime 已按既定策略收敛 PAUSED），另两个恢复测试过早读取 callback identity/发起恢复；分别改为断言稳定 PAUSED 和等待 durable delivery ID / strategy thread exit。并未放松业务行为断言。
- **边界：** SELL 端到端使用合成持仓与 fake transport，不证明真实 IBKR 账户上的持仓归属或真实成交。NL2 `target_fraction` PAPER session 仍不存在，真实生产 risk facts/provider 和 PAPER 配置也未就绪。没有连接 Gateway、读取账户或发送真实订单。

## B148 续审发现 PAPER 托管组合目前仅接 Python upload（2026-10-22）

- **核对证据：** `agent/src/execution/managed_python_paper_session.py::_validate` 只接受 `SourceKind.PYTHON_UPLOAD`、`PositionMode.EXPLICIT_QUANTITY`、`PythonEventCoordinator` 和 `PythonOutboxIntentHandoff`；`start_paused()` 固定使用 `PythonPaperEventBridge`。因此现有 manager/SDK/Backtrader paper 托管链的真实组合没有受信任 NL2 模板及 `target_fraction` 路径，不能把“两种入口有离线回测”误记为“两种入口都已接入 PAPER”。
- **本轮动作：** 未把 NL2 模板伪装成 Python 上传或绕过 host 风控；将该双入口缺口升到 `BLOCKERS.md` 与 T14 证据说明，明确下一实现应增加受信任模板 event coordinator/bridge，并复用同一 PortBroker、order ledger、grant、风险事实与派单闸门。
- **边界：** 尚未实现 NL2 模板托管 session，也没有 paper 配置或真实账户操作。PAPER 仍默认不就绪；缺账户参数不阻止实现离线组合和拒绝路径。

## B147 回测会计新增独立手算 oracle，覆盖已平仓亏损与未平仓估值（2026-10-22）

- **缺口：** 既有主 BackBroker 与独立 BackBroker replay 对齐，但两边可能共享费用/报告错误；原样本也没有独立核对已平仓交易的金额。
- **修改：** `run_synthetic_backtest()` 暴露可选 `slippage_fraction` 并传给同一显式 BackBroker 成本模型，默认仍为 0；扩展 12-bar 手算样本，覆盖两笔买入、一笔亏损平仓、期末未平仓、佣金与买卖两侧 OHLC 滑点封顶；同时由独立 BackBroker replay 子进程复算。
- **结果：** 另以 Decimal 手算 SMA(2/3) 交叉，独立重建出第 4 bar BUY、第 7 bar SELL、第 10 bar BUY。首笔 49 股 BUY 以 $10.05（$10.05 high cap）成交，佣金 $4.9245；49 股 SELL 的 $6.90 开盘价经 10% adverse slip 到 $6.21，由 $6.30 low cap，佣金 $3.087；之后再买 44 股，以 $13.20 high cap 成交，佣金 $5.808。手算最终 cash=$221.6305、44 股按末价 $12 估值、final equity=$749.6305；已平仓 gross P&L=-$183.75、扣除两侧费用后的 net P&L=-$191.7615。总收益率=-25.03695%；峰值权益 $1,041.6255 到末值的最大回撤为 $291.995（约 28.0326%）。数量、信号时点、成交、费用、cash、逐 bar equity、closed trade、期末持仓、回撤/收益和 subprocess replay 均匹配；篡改 total_fees 会被 verifier 拒绝。
- **验收：** 新回归定向 **1 passed**；连同 SMA 回测、market data、replay subprocess、Python upload 回归的合并命令 **30 passed、437 warnings、4.03 秒**；完整 Backtrader runtime + backtest API 子树 **116 passed、1147 warnings、9.04 秒**。编译、`git diff --check` 和 8 文件 trailing whitespace 检查通过。数据是标记为 synthetic 的小 fixture，不是供应商/许可行情；真实数据和统一完整统计口径仍未验收，T09/T10 继续 PARTIAL。无 Gateway/account/order 操作。

## B146 订单可能已被外部接受但本地 ACK 丢失时不重复派发（2026-10-22）

- **故障注入：** 第二个子进程回归使用独立 append-only 文件模拟 transport 外部接受记录。sender 将确定性的 attempt/client order ID 同步写入并 `fsync` 后挂起；父进程确认模拟外部接受后 SIGKILL 策略进程，重开同一 SQLite ledger 并重试。
- **结果：** 模拟外部记录恰有一条接受记录；durable ledger 为 `SEND_STARTED`，重试返回 `RECONCILE/send_outcome_unresolved`，sender 和风险 provider 均为 0 调用。dispatch 定向组 **27 passed、0.49 秒**。
- **边界：** 文件只是离线假 Broker 接受账本；不等价于 IBKR/Gateway 网络写入、服务器端订单受理或真实 ACK/回报丢失。证明的是本地重启不会盲目再次调用 sender，并不解决如何查明 Broker 是否接受、也不构成 exactly-once。T42 仍 PARTIAL；无外部账户或真实订单操作。

## B145 硬杀进程发生在 SEND_STARTED 提交之后时禁止盲目重发（2026-10-22）

- **故障注入：** dispatch 子进程调用真实 SQLite ledger，在 `start_send_after_risk()` 事务提交后、方法返回及 transport sender 进入前写 readiness marker 并挂起；父测试进程确认 marker 后用 SIGKILL 硬杀子进程，再从同一数据库重开 ledger 并重试 intent。
- **结果：** 重启后 intent 仍为 `SEND_STARTED`，dispatch 返回 `RECONCILE/send_outcome_unresolved`；risk facts provider 与 sender 调用均为 0，未假装知道外部发送结果或创建第二次发送。dispatch 定向组 **26 passed、0.49 秒**。
- **边界：** 该真实进程/SQLite 故障点仅位于 durable send boundary 与调用注入 transport 之间；不证明外部 Broker 原子性、实际网络部分发送、真实 Gateway 对账或后续人工/自动 reconciliation 的正确性。T42 仍 PARTIAL。没有连接 Gateway、账户或真实订单。

## B144 Docker deadline 会清理忽略 TERM 的策略后代进程（2026-10-22）

- **故障注入：** 扩展真实 Docker integration。上传事件代码 `fork()` 一个子进程；子进程先安装 `SIGTERM` 忽略处理器，再在容器 tmpfs 写 ready marker。测试等到 marker 可见后杀死宿主 Python supervisor 与 Docker CLI，避免只观察到容器启动就误判后代已运行。
- **结果：** 容器内 `/usr/bin/timeout` 的 2 秒硬期限和 daemon 托管的 `--rm` 最终使容器在限定轮询窗口内退出并自动删除。Colima Docker 专项 **1 passed、3.62 秒**；launcher 单元 **15 passed、1.39 秒**；相关 `py_compile` 与 `git diff --check` 退出码 0。
- **边界：** 验证的是当前 Colima/Linux 容器配置与一个已就绪、忽略 TERM 的子进程；不覆盖 daemon 被杀、容器 runtime/kernel 缺陷、所有进程逃逸、其他 host 以及完整资源耗尽组合。T37 继续 PARTIAL。没有连接 Gateway、账户或真实市场，也没有订单操作。

## B143 联合验收 failed-session recovery 中活动 BUY、未 ACK 回报与原事件重试（2026-10-22）

- **完成：** 新增 `test_failed_session_recovery_replays_unacked_partial_buy_without_duplicate_fill_or_send`，把当前 run exact committed outbox lineage、ref=77 的部分成交 BUY、同一 execution 的重复累计回报（durable inbox 未 ACK）和 PAUSED released retry event 放入一条真实 Cerebro/SQLite 恢复流程。
- **验证：** 恢复重建后仍为 PAUSED，ref=77、filled=1、position=1 与账本一致；重复 callback 不增加 fill，旧 inbox 在新 native ref 上重放并 ACK；另一笔新注入的唯一 execution 在 PAUSED 下将订单完成到 filled=2/position=2。之后单独 activate，sandbox 只收到原 event ID 与 bar 一次，checkpoint/cursor 只前进一次，order send=0。组合组 **181 passed、93 warnings、19 subtests**；IBKR SDK 子进程 suite **46 passed**。
- **修复：** 组合测试发现 replacement feed 在 `Cerebro.adddata()` 前用于 native order restore，Backtrader 尚未为 feed 固定 instrument name；恢复现改为先注册重建 feeds 和 broker，再恢复订单引用。
- **边界：** 失败由测试在 service gate 已 PAUSED 时将 exact retry bar 注入真实 Cerebro/bridge 边界，证明 fail-closed session exit 与其后 recovery 交接；不是 Docker sandbox timeout、callback worker fatal、SDK/Gateway/daemon/host crash 与活动 BUY 的联合复现。IBKR runtime、行情、账户和订单均为 fake/synthetic。SELL、取消/拒绝、未知发送等未支持状态仍 fail-closed；T41/T43 继续 PARTIAL。没有连接 Gateway、账户或真实订单操作。

## B142 failed strategy session 可在独立授权后重建并保持 PAUSED（2026-10-22）

- **管理入口：** `PaperExecutionManager.recover_failed_session()` 需要独立 `recover` grant、同一可信 scope、稳定的 `managed_python_session_failed` 状态、PAUSED durable service 与仍持有的 owner lease；`POST /api/execution/paper/recover` 从可信 active scope 取 scope，不接受 body 覆盖。成功恢复不会 activate。
- **恢复编排：** 仅接受策略/Cerebro 线程已经退出、callback worker 健康存活的失败。复核原 released event 与旧 feed cursor；同一 SDK connection 经私有 IPC 调用 `reqIds(-1)` 并等待 pump 新的 nextValidId generation，随后做 fresh account/order/position snapshot reconciliation。暂停 callback worker 后核验 raw spool/queue 与 snapshot，按 durable order binding 恢复支持的 BUY/native refs，回绑/replay 未 ACK durable inbox，重建 feed/Cerebro。恢复返回仍为 PAUSED；后续独立 activate 才恢复同一 event ID/seq/payload 的策略重试。恢复对账失败时 runtime 把 RECOVERING 收敛到 PAUSED，allocator 保持关闭。
- **验收：** execution/journal/callback/session/runtime/manager/API 定向组合 **180 passed、81 warnings、19 subtests**；独立 SDK runtime suite **46 passed**。覆盖 same-connection nextValidId refresh、RECOVERING→PAUSED fail-closed、独立授权 manager/API 路径、无活动单失败事件在独立 activate 后按原 identity 只提交一次；相关完整 managed-session 既有回归仍通过。
- **边界：** 新的失败后端到端用例仅覆盖无活动单、无未 ACK inbox；B127 partial BUY cold-start 和 B141 未 ACK callback sink replay 仍是分离证据，尚未在一次 failed-session recovery 中联合验收。只恢复 callback worker 健康的策略线程失败；pump/worker fatal、SDK/Gateway 断连或 host crash 需要另行运行态/进程 epoch recovery，目前不可用。snapshot 使用 fake IBKR process，真实 Gateway、账户、行情、order 和生产可归因身份未验收。T41/T43 保持 PARTIAL。

## B141 callback worker 暂停后可安全转移未 ACK 投递（2026-10-22）

- **新增持久视图：** `BrokerOrderIndex.pending_delivery_event_ids()` 读取所有 `delivered=0` 且有 delivery event ID 的 callback inbox 记录，并拒绝重复/非法 event identity。
- **受控换接：** `IbkrCallbackPumpWorker.rebind_delivery_sink()` 只允许存活、健康且 gate 已暂停的 worker；持有 `process_one` 边界锁期间核对 durable pending IDs 和调用方预期完全相同，再将 pump sink 改到新 broker/router，并只清除可由未 ACK inbox 重放的进程内 claim。worker 重启消费 inbox 时账本再次幂等记录、重新生成相同累计 callback，由恢复后的 native order ACK；无 durable replay source 的 claim 不会被静默清除。
- **验收：** `test_paused_callback_worker_can_rebind_and_replay_unacked_delivery` + worker pause 并发专项 2 passed；journal/event-worker/callback-pump/managed-session 合并组 **111 passed、71 warnings、6.41 秒**；`git diff --check` 通过。
- **边界：** callback 换接原语尚未被 managed session/manager recovery 编排调用；不代表 callback barrier 到 fresh snapshot 的全程原子化，也未把旧 PortBroker notification deque/native registry/strategy state 一并迁移。snapshot 前后新 callback、raw spool/fee facts、unsupported order state 仍必须由完整 recovery orchestrator 核验；managed-session timeout 实测仍使用 fake IBKR，没有 Gateway/account。T41/T43 仍 PARTIAL。

## B140 为失败会话重试固定同一个 released event（2026-10-22）

- **新增 journal 原语：** `inspect_paused_released_event()` 只读返回 PAUSED run 在 authoritative cursor 上已 released、未提交的事件，并核对 canonical payload/hash、无同 seq result/outbox、无后续事件及 checkpoint 与前一条已提交结果一致。
- **受保护重试：** `resume_paused_released_event()` 要求 caller 提供刚检查到的 event ID、seq 与 payload SHA-256；在 `BEGIN IMMEDIATE` 内复核 run/cursor/identity/hash/checkpoint/side effects，然后只将 run gate 从 PAUSED 改成 RUNNING。原 released event 的 ID、seq、payload、released_at 与 cursor 不变；完整性或状态检查失败保持 PAUSED。
- **验收：** `./.venv/bin/python -m pytest -q agent/tests/execution/test_event_journal.py --disable-warnings --tb=short`：23 passed、0.22 秒；覆盖精确事件恢复、错误 ID/cursor、corrupt payload 与 outbox side effect 拒绝；`git diff --check` 通过。
- **边界：** 这是 trusted recovery orchestrator 的持久化前置原语，不是 manager/API 授权或端到端 session recovery；当前没有生产调用者。它不解决 runtime fresh snapshot 与 callback quiescence 竞态、旧 `PortBroker` 内存通知/队列、pump delivery claim/ACK 转移、native order/callback 重建或冻结策略线程替换。只有在这些检查完成后才可调用；恢复成功后必须单独授权 activate。T41/T43 仍 PARTIAL。无 Gateway/account/真实订单操作。

## B139 stop 挂起时保留独立授权撤单通道（2026-10-22）

- **修复：** `PaperExecutionManager.cancel_owned_order()` 在 manager phase 为 READY 或 STOPPING 时都允许继续走独立 `cancel` grant；runtime 仍核验服务状态、owner lease、账户 scope 和 intent→broker order durable binding。未授权主体、未知订单或 service 已不是 RUNNING/PAUSED 时保持拒绝。
- **回归：** 扩展 B137 真实 Docker timeout 场景：已有活动 owned order 使安全 stop 返回未完成，manager phase=STOPPING，但 service 仍 PAUSED 且 lease 保留；后台部分成交落 ledger，SHARED_KEY cancel 被拒，可归因 principal 发出唯一 exact broker ID=55 撤单，Cancelled callback 收敛为 CANCELLED。
- **验收：** Docker 专项 1 passed（3.86 秒）；managed-session + manager 回归 35 passed（4.88 秒）；paper routes + manager 回归 37 passed（2.07 秒）；相关源码/test py_compile 与 `git diff --check` 通过。
- **边界：** 仅覆盖 managed session 发现活动订单、尚未调用 runtime.stop 而保持 service PAUSED 的情形；没有把撤单开放给 service 已进入 STOPPING/STOPPED 的 runtime，也不解决 failed-session replacement。fake IBKR transport；T41/T43 继续 PARTIAL，无 Gateway/account/真实订单操作。

## B138 阻止策略线程死亡后的错误重新 activate（2026-10-22）

- **修复：** `ManagedPythonPaperSession.activate()` 现在先检查该 session 是否已记录策略线程异常。失败 session 返回稳定类别的 `ManagedPythonPaperSessionError`，不调用 runtime activate；不会因运行时 SDK 仍健康而把已无策略线程的 session 恢复成 RUNNING。
- **回归：** 扩展 B137 真实 Docker 无限循环集成：超时后确认 session/manager status 暴露 `managed_python_session_failed`，principal 即使持有 activate grant 也不能重新打开闸门，service 仍为 PAUSED、策略 order send=0；callback/fill/exact cancel 仍在 PAUSED 下工作。
- **验收：** `DOCKER_CONTEXT=colima-vibe-execution uv run pytest -q --tb=short -W ignore agent/tests/execution/test_managed_python_paper_session.py`：17 passed、5.36 秒；`py_compile` 与 `git diff --check` 通过。
- **边界：** 这是 fail-closed 禁止错误重启；没有提供同进程 failed-session replacement/recovery。failed session 的 lease 必须保留，尚无路径安全消费其未 ACK native callback 并重建 Cerebro/order/ref state。T41/T43 仍 PARTIAL；无 Gateway/account/真实订单操作。

## B137 真实 worker 超时暂停 managed session，callback worker 仍独立运行（2026-10-22）

- **完成：** 在 managed Python PAPER session/Cerebro 组合中运行真实 Docker 上传策略死循环，2 秒 worker deadline 以 `SandboxTimeout` 到达 session；service 与对应 SQLite execution run 均进入 PAUSED，健康码为固定 `managed_python_session_failed`。
- **fail-closed 与 callback：** seq=1 released event 保持未提交，`next_seq=1`、event_result/order_outbox 均为 0，策略订单发送为 0。策略线程失败后真实后台 callback worker 仍存活且无故障；注入有效 `nextValidId=777` 后，SQLite allocator 持久记录候选值 `(777, candidate=1)`，scope/run 继续 PAUSED。
- **既有订单管理：** 策略异常后注入绑定到 PAPER intent 的一股 execution callback，ledger 在 Cerebro 线程已失败时仍记录部分成交。共享 key 主体撤单被拒；可归因主体用本地 intent ID 撤单，fake transport 只收到 durable binding 对应的 broker order ID=55；后续取消回报将 intent 收敛至 CANCELLED，累计成交仍为 1。整个过程 service/run 保持 PAUSED，策略订单发送为 0。
- **验收：** `test_managed_session_real_worker_timeout_pauses_run_but_keeps_callback_and_cancel_alive` 真实 Docker 专项 1 passed（单独复跑 3.68 秒）；完整 managed-session 文件 17 passed（最终复跑 5.10 秒）；py_compile 与 `git diff --check` 通过。唯一 worker image 在 finally 中删除，测试结束停止线程并释放本地 lease。
- **边界：** 真实 Docker worker + managed session/journal/service 的离线组合，但 IBKR runtime/transport 是 fake。证明 callback pump 可持久记录 fill，并由 manager 授权发送 exact-intent cancel；不证明真实 SDK/Gateway、持续对账、daemon/host 崩溃恢复或 production identity/app wiring。T37/T42/T43 仍 PARTIAL；无 Gateway/account/行情/真实订单操作。

## B136 真实 Docker worker 超时不替换已释放事件（2026-10-22）

- **完成：** 在真实 Docker worker + SQLiteEventJournal/PythonEventCoordinator 集成用例中运行无限循环策略，设置容器内 2 秒 deadline。真实 SandboxTimeout 通过 EventWorkerError cause 链暴露；首个已释放 event 仍保留 seq=1、原 event_id 与原 Bar，event_result/order_outbox 均为 0，next_seq 不动。
- **重试与清理：** 用另一个 event_id 和不同 close 的 Bar 重试，coordinator 再次超时但仍复用原 released event，不能替换成 caller 新 Bar；两次 worker container 都通过 Docker inspect 确认已删除。
- **验收：** test_real_worker_timeout_preserves_authoritative_released_event_and_removes_container：真实 Docker 定向 1 passed（5.50 秒）；agent/tests/execution/test_event_worker.py 11 passed（0.20 秒）；py_compile、whitespace 与 diff check 通过。
- **边界：** 覆盖真实隔离容器 deadline、事件日志的 durable identity 与容器清理，不覆盖 ManagedPythonPaperSession 接收该异常后的 service/journal PAUSED 联动、callback continuity/授权撤单，也不覆盖 Docker daemon/host 崩溃。T37/T42/T43 仍 PARTIAL。未连接 Gateway、账户、行情或发送订单。

## B135 联合恢复部分成交 BUY 与非空 Python checkpoint（2026-10-22）

- **完成：** 在同一 managed-session/SQLite reopen 场景合并 B127 active BUY/native ref 恢复与 B134 非空 checkpoint 续跑。持久化首事件 checkpoint、当前 run exact committed proposal/outbox/intent lineage、ref=77 partial BUY 与对应账户快照；关闭旧 store 后新 runtime/session 重开。
- **恢复及顺序：** start_paused 时没有 sandbox 调用、cursor 不推进、没有重新发送旧单，原生 ref=77 恢复为 Partial。后台 callback worker 收到剩余 execution 后，ledger 累计 fill=2，Backtrader 将原 order 更新为 Completed，position=2、cash=8898，callback inbox/ack 清空；再激活 session 处理下一 bar，sandbox 收到精确 durable checkpoint，event_count=2、close_total=203，next_seq 从 2 到 3，事件仅提交一次，fake SDK send=0。
- **验收：** 新联合场景 1 passed（0.75 秒）；managed session + journal + callback 合并组 96 passed（2.95 秒）；目标测试文件 diff check 通过。
- **边界：** 使用 fake IBKR/runtime 与 sandbox，真实 Cerebro、SQLite/journal、ledger、callback worker 和 Backtrader native order。没有真实 Gateway/account；没有 Docker worker crash；仅恢复已验证的 BUY partial-fill 状态。SELL、拒绝/取消/未知发送等仍 fail-closed，T41/T42 仍 PARTIAL。

## B134 managed session 非空 Python checkpoint 重开续跑对照（2026-10-22）

- **完成：** 增加 managed session 级 SQLite reopen 对照。先用真实 Cerebro/PortBroker 跑完两个 completed bar 作为不中断参考；另一路在首个事件已提交后关闭并重开 journal，创建新的 runtime/session，再消费相同的下一根 bar。重开后 cursor 从 2 继续，checkpoint 完整传给 sandbox，event_count、close_total、last_close 与参考 run 一致，下一事件 proposals 与 reference 相同，cursor 到 3；没有 broker send。
- **执行边界：** 使用确定性 StatefulCheckpointSandbox 验证 session/coordinator/journal 的 checkpoint 交接，不启动 Docker worker。首事件提交前缀由 reference result 写入 journal 构造，非真实子进程崩溃；恢复启动本身不调用 sandbox，随后仅消费第二事件一次。
- **验收：** checkpoint 专项 1 passed（0.83 秒）；managed session + journal + callback 定向合并组 95 passed（2.90 秒）。合并组发现既有 B132 失败回调断言存在观察时序竞态，测试现等待 service 与 journal 均稳定 PAUSED 后再检查原有断言；没有放宽语义。diff check 通过。
- **边界：** T41 仍 PARTIAL。此用例不把非空 checkpoint 与活动 native order/callback 一起恢复，不覆盖 Docker worker crash、Backtrader indicator/order-ref state、Gateway 或账户事实，也不证明 production provider。真实 PAPER 仍需另行授权。无 Gateway、账户或真实订单操作。

## B133 策略阻塞期间 callback 仍落账且 manager 撤单可用（2026-10-22）

- 在 `agent/tests/execution/test_managed_python_paper_session.py` 增加 managed-session 并发集成回归。第一根 completed bar 进入 sandbox 后阻塞在结果返回前，断言 event cursor 未推进、结果未提交、无 pending outbox、无策略订单发送；再为测试预置一笔已 ACK 的 durable intent/native order，避免改动生产事件顺序。
- 阻塞期间，真实后台 callback worker 收到一股部分成交并立即更新 SQLite ledger，而 inbox callback 仍等待 Cerebro 消费；`SHARED_KEY` 主体的撤单请求被拒，可归因主体经 `PaperExecutionManager.cancel_owned_order()` 按本地 intent ID 撤单，transport 只收到精确持久 broker order ID。取消回报持久为 CANCELLED。释放 sandbox 后未提交事件才提交并产生策略订单；队列回报由 Backtrader 消费并 ACK。
- 定向命令：`uv run pytest -q --tb=short -W ignore agent/tests/execution/test_managed_python_paper_session.py::test_blocked_managed_strategy_still_persists_fill_and_authorized_exact_cancel`：**1 passed、0.73 秒**；session/callback 回归 **73 passed、2.71 秒**；runtime/manager 回归 **27 passed、0.53 秒**。该 Python 测试文件 `git diff --check` 通过。
- **边界：** 回归使用 fake IBKR、合成账户/行情与测试预置的已绑定活动单；它证明当前进程内策略线程阻塞时后台 ledger 回报与授权精确撤单路径可独立推进，不证明真实 SDK/Gateway callback、进程崩溃恢复、跨进程 native-order 重建或生产身份接线。T43 仍 PARTIAL。没有连接 Gateway、账户或行情服务，也没有真实订单操作。

## B132 callback worker 致命故障会同时暂停 session journal 与 PAPER scope（2026-10-22）

- `ManagedPythonPaperSession` 在组合时链式包装 runtime 已安装的 `IbkrCallbackPumpWorker.failure_sink`，保留原 runtime failure handler；worker 致命失败后先尽力持久暂停该 session 的 execution run，记录本地异常供诊断，再调用原 handler 关闭账户级执行闸。状态只暴露既有稳定 `managed_python_session_failed`，不暴露 callback 详情。
- 新增真实后台 callback worker + fake IBKR integration：session 完成假 handshake/reconciliation 并激活后注入账户不匹配的 `managed_accounts` 回报；验证 callback worker 锁存失败、service scope 为 PAUSED、对应 execution journal 为 PAUSED、稳定 health code 无敏感详情。专项命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_managed_python_paper_session.py agent/tests/execution/test_ibkr_callback_pump.py --disable-warnings --tb=short`：**72 passed、33 warnings、2.48 秒**；新增故障用例单独 **1 passed、3 warnings、0.73 秒**。
- 既有 runtime/manager 接口回归 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_managed_runtime.py agent/tests/execution/test_paper_execution_manager.py --disable-warnings --tb=short`：**27 passed、0.49 秒**。
- **边界：** 对 journal 的持久暂停仍是 best-effort，SQLite 不可用时不能声称 durable pause 已写入。该用例证明致命 callback 被识别后的故障联动，不证明策略阻塞时正常 fill/cancel callback 持续处理、授权撤单成功、重连/恢复或 Gateway 行为；T43 仍 PARTIAL。fake fatal 后 runtime 会按恢复语义保留 owner lease，测试 teardown 只清理假进程/临时 lease。没有 Gateway、账户、行情或订单操作。详见 `ACCEPTANCE.md B132` 与 `BLOCKERS.md B132`。

## B131 半核 Docker worker 的 CPU 饱和限额负例（2026-10-22）

- 在 `cpu_count=0.5` 的真实隔离容器中运行两秒纯 busy loop，读取前后 cgroup v2 `cpu.stat/usage_usec`；墙钟至少 1.9 秒时，进程组 CPU 用量必须不超过 1.75 秒，并核对 `cpu.max` 的配额。这样验证 CPU quota 的运行效果，不只看 Docker 参数回显。
- 定向 Docker 用例 **1 passed、7.18 秒**；含安装器、PID/内存/CPU 探针的 execution/Backtrader/API/PAPER 合并组 **580 passed、1395 warnings、15 subtests、44.85 秒**，退出码 0。`compileall` 与 `git diff --check` 通过。
- **边界：** 这是固定 0.5 CPU 下的一次 2 秒压力样本，不替代不同运行负载/平台上的压力与 fairness 矩阵；Docker daemon 仍是宿主信任根，T37 继续 PARTIAL，Python PAPER composition 未开放。无 Gateway/account/行情/订单操作。详见 `ACCEPTANCE.md B131`。

## B130 低内存压力负例验证容器受限并回收（2026-10-22）

- 在隔离测试镜像里请求连续触碰 128 MiB 页面，将容器 cgroup memory cap 降到 32 MiB。验收只接受两种受限结果：worker 收到 `MemoryError` 后报告分配被拒，或 cgroup OOM 使 Docker worker 返回 137；两种路径都须经过 launcher 清理并确认容器不存在。探针只在测试镜像中运行，不触碰宿主数据。
- 真实 Docker 定向用例 **1 passed、5.31 秒**；含该探针的 execution/Backtrader/API/PAPER 合并组 **580 passed、1397 warnings、15 subtests、42.92 秒**，退出码 0。`compileall` 与 `git diff --check` 通过。
- **边界：** 本用例证明一次小额度分配压力会受限并回收容器；不证明所有分配器/内核/OOM 场景的完整恢复矩阵，也没有 CPU 饱和后的核算与回收证据。T37 仍 PARTIAL，默认 app 无 Python PAPER production composition。无 Gateway/account/行情/订单操作。详见 `ACCEPTANCE.md B130`。

## B129 真实 Docker 资源限额读数与 PID 超额负例（2026-10-22）

- 将真实容器隔离用例改为较小内存/半核/PID=4 配置，worker 从 cgroup v2 读取 `memory.max`、`cpu.max`、`pids.max`，并尝试超额派生进程；断言内核限额读数分别匹配容器配置，PID 超额创建得到拒绝。Docker inspect 同时核对 memory、memory-swap、NanoCpus 与 PidsLimit。只在隔离测试 helper 中加探针，不改策略 API/运行配置。
- 定向真实 Docker 用例 **1 passed、4.63 秒**；最终 execution/Backtrader/API/PAPER 合并组 **580 passed、1393 warnings、15 subtests、42.75 秒**，退出码 0；相关源码 `compileall` 和 `git diff --check` 通过。
- **边界：** 已实测该 Docker context 中 memory/CPU/PID 的 cgroup 限额被设置，且 PID 创建上限实际生效；没有通过大内存 OOM 或 CPU 饱和测试验证资源超限后的全部 kill/回收语义，T37 仍 PARTIAL。无 Gateway/account/市场数据/订单操作。详见 `ACCEPTANCE.md B129`。

## B128 官方 IBKR SDK 安装器增加锁校验回归与直接调用保护（2026-10-22）

- `install_ibapi_from_official_source.py::install()` 现在自行复核官方解压源码树的固定 SHA-256、文件数和版本，避免直接调用函数时绕过 CLI 前置验证；来源不匹配时，在检查目标 Python/调用 `uv` 之前拒绝。
- 新增 7 个离线回归：正确源码树与内容篡改、符号链接、版本错配、build dependency 错配、`uv` 缺失、精确 venv Python/`--no-build-isolation --no-deps` 调用契约，以及 `install()` 再验证。专项 7 passed；execution/Backtrader/API/PAPER 合并组 **580 passed、1393 warnings、15 subtests、42.41 秒**，退出码 0；`compileall` 与 `git diff --check` 通过。
- **边界：** 测试使用合成微型源码树和 mock 安装器进程，不是 IBKR 官方发行包的干净环境重装；本轮未安装/运行 SDK child，未连接 Gateway、账户或市场数据，也未发送订单。D-009 既有 source-tree lock 保持；真实官方源码许可下载与 archive 级验证仍需独立处理。T37、PAPER 和 SDK 实际连接验收不升级。详见 `ACCEPTANCE.md B128`。

## B127 将严格 BUY 订单恢复接入 managed-session 冷启动（2026-10-22）

- `ManagedPythonPaperSession.start_paused()` 现在只恢复 ledger 为 `ACKNOWLEDGED` / `PARTIALLY_FILLED` 的 BUY 活动单。每笔订单必须同时匹配 PAPER account scope、ledger intent、broker binding、durable BT ref、合约/feed、deployment、冻结策略版本，以及当前 RUNNING journal run 下对应的已提交 event/result、prepared intent 和 exact outbox link。相同 deployment/version 的另一 run、未链接 intent、哈希/事实不一致均拒绝。
- 恢复前要求 callback worker 在 `process_one()` 边界 quiesce；暂停后复核账户快照中的 open-order 集合与 durable orders/positions。只在 native refs 已恢复、Cerebro 已启动之后恢复 callback worker。恢复阶段存在 commission fact（活动单关联费用或未关联 pending 费用）、pending callback/inbox/outbox、未知状态、SELL、SEND_STARTED/UNKNOWN/CANCEL_PENDING 或账户/合同/成交事实不一致时保持 PAUSED 并保留 lease。测试用 reopened SQLite 与 fake IBKR account snapshot 验证 ref=77 partial BUY 恢复、下一执行回调只在 native ref 注册后消费、cash/position 正确增长且发送端调用次数为 0。
- 新增 `require_linked_outbox_intent()` 校验当前 run 的事件/result摘要、proposal、冻结 intent 与 ledger intent 的精确一致性；新增 callback worker pause/resume quiescence 并发回归。
- 定向 session/callback 命令 `./.venv/bin/python -m pytest -q agent/tests/execution/test_managed_python_paper_session.py agent/tests/execution/test_ibkr_callback_pump.py`：**71 passed、35 warnings**。完整 execution/Backtrader/API/PAPER 组合 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：**573 passed、1397 warnings、15 subtests、44.49 秒**，退出码 0。`compileall`、`git diff --check` 通过。
- **边界与发现：** T41/T42 仍 PARTIAL。只恢复上述窄 BUY 活动单；SELL、撤单/拒绝、未知/发送不确定状态、indicator/native state、实际 Gateway 故障矩阵仍不支持或未验收。无 bar 且已有持仓时，Backtrader stop 读取持仓市值会失败；本轮只在测试中确认该边界，没有改造行情估值或真实账户运维。没有 Gateway、账户或真实订单操作。详见 `ACCEPTANCE.md B127` 与 `BLOCKERS.md B127`。

## B126 为受信任 BUY 活动单增加离线 native Backtrader Order 重建原语（2026-10-22）

- `ReconciledOrderState` 只接受严格的 durable ref、合约、数量、状态、成交均价/时间、费用及对账后持仓事实。`PortBroker.restore_reconciled_order()` 只能在账户 reconciliation snapshot 已 hydration、broker 尚未 start、回调队列健康且干净时运行；只接受同一 feed 的 BUY `SUBMITTED` / `ACCEPTED` / `PARTIALLY_FILLED` / `FILLED`。
- 方法以 Backtrader 1.9.78.123 的真实 native `BuyOrder`、`submit/accept/execute` 和 execution bits 重建已存在订单，保留 durable ref，只注册到 broker；不调用 `BrokerPort.submit_order`，不重复改已由账户快照 hydration 的 cash/position。历史累计 callback 重放时不重复记现金/持仓；真实 Cerebro `notify_order` 收到恢复订单；新订单使用 broker-local 单调 ref，避免恢复 ref 与 fresh-process 序列冲突。
- SELL 聚合成交拒绝恢复：当前输入没有逐笔 closed/opened/PnL 和足够的 cost-basis facts，不能猜 Backtrader realized PnL bits。取消/拒绝/未知状态也未在此 primitive 中支持。
- 定向 broker/hydration 命令 `./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_broker_proxy.py agent/tests/bt_runtime/test_broker_snapshot_hydration.py --disable-warnings`：**23 passed、14 warnings**。完整 execution/Backtrader/API/PAPER 组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`：**566 passed、1385 warnings、15 subtests、43.16 秒**，退出码 0。`compileall`、`git diff --check` 通过；Execution UI 9 passed、production build 成功。
- **边界：** 原语还未接入 `ManagedPythonPaperSession`、ledger binding/order-history 提取、恢复后的 strategy/indicator state 与 callback cursor。它证明特定 BUY native order 可离线重建，不证明 managed session cold-start recovery，也不升级 T41/T42；T41/T42/T43 继续 PARTIAL。无 Gateway/account/order 操作。详见 `ACCEPTANCE.md B126`。

## B125 策略线程异常的持久暂停与稳定健康码（2026-10-22）

- `ManagedPythonPaperSession._run_cerebro()` 捕获未处理线程异常后，先尽力将该 session 的 SQLite execution run 持久置为 PAUSED，再在 execution service 仍为 RUNNING 时关闭 scope 闸门。异常详情保留给本地诊断属性，不进入状态 API。
- session 暴露固定 `managed_python_session_failed` health code；`PaperExecutionManager.status()` 在没有更高优先级 manager 启动错误时向 status/API 提供该稳定码。测试验证 journal 与 execution service 均暂停、状态不泄露异常文本，以及 manager status 展示健康码。
- 定向 session/manager 测试 **24 passed、23 warnings、1.07 秒**。完整 execution/Backtrader/API/PAPER 组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short`，**561 passed、1377 warnings、15 subtests、42.79 秒**，退出码 0。`compileall` 与 `git diff --check` 通过。
- `/execution` PAPER 状态卡在存在 `last_error_code` 时显示本地化标签与稳定错误码；9 种现有 locale 都有文案。页面打开时立即读取状态，之后每 10 秒只读刷新；上一请求未完成时跳过本次 tick，卸载时清理 interval。Execution UI **9 tests passed**，覆盖无需重载就出现新的错误码；`npm run build` 成功（有既存大 chunk 提示）。
- **边界：** journal/service 持久暂停均为异常处理中的 best-effort；若 SQLite 或持久层本身不可用，固定 health code 仍可由当前 manager 进程读取，但不能宣称持久告警成功。此切片没有测试策略异常后 callback pump 继续接收回报或授权撤单，也没有实现跨进程 native Order/ref/state 恢复；T41/T42/T43 继续 PARTIAL。没有 Gateway/account/order 操作。详见 `ACCEPTANCE.md B125`。

## B124 RUNNING 状态下重新 start 必须关闸并完成新恢复（2026-10-22）

- 修复 `PaperExecutionService.begin_recovery()` 的 RUNNING+lease 快捷返回。现在 `start()`/runtime recovery 会把已 RUNNING 的 durable control gate 先写为 RECOVERING，强制执行 fresh recovery check，成功后只回到 PAUSED；不因本地已有 lease 就默认为继续运行。
- 新增回归：PAUSED→显式 activate 到 RUNNING→再次 `start()`，检查两次 recovery、durable `recovered_requires_explicit_activation`，lease 保留。定向 lifecycle/manager/managed-runtime/deployment 组 **40 passed**；完整 execution/Backtrader/API/PAPER 组合 **559 passed、1379 warnings、15 subtests、44.16 秒**，退出码 0。首次定向组合有一项 managed-runtime cancel-stop 返回 false；该用例独立复跑通过，随后完整组合也通过，未移除或放松断言。
- **边界：** 这修复的是持有同一内存 runtime 时重复进入 recovery 的关闸语义；不实现新进程的 Backtrader native order/ref/state 重建。T41/T42/T43 仍 PARTIAL；没有 Gateway/account/order 操作。详见 `ACCEPTANCE.md B124`。

## B123 接入显式 PAPER app composition，并收紧 T39/T44 报告读取（2026-10-22）

- 新增类型化 `PaperExecutionComposition` 与 `configure_paper_execution_app()` deployment hook；FastAPI lifespan 启动时显式安装 manager/readiness/scope，缺配置仍 materialize inert 默认值。构造/注册不启动 runtime；start 仍受归因身份、grant 和 readiness gate 控制。新增组合与默认 inert 回归。
- T39 拒绝 datetime 子类，校验 UTC 归一化结果并把 bar/time 比较纳入 fail-stop 边界；恶意 `astimezone()` 返回 naive 或非 datetime 时，先持久 PAUSED、worker=0、cursor 不前移。
- T44 增加 `load_verified_report()`：校验并解析同一份 bytes snapshot，API 直接返回已验证对象，不再二次打开 report。新增校验后替换磁盘报告的竞态回归，仍返回旧的已验证快照。
- 定向 app/API 组 **33 passed**；T39 journal 专项 **22 passed**；合并 execution/Backtrader/API/PAPER 组 **558 passed、1381 warnings、15 subtests、42.63 秒**，退出码 0。无 Gateway/account/order 操作。
- **边界：** composition 只是部署注入 seam，不存在可信生产 identity/provider 实现；app 默认仍 inert。T39 全面 introspection/escape matrix 仍缺；T44 sidecar/config/report 同机可写且缺签名或独立信任域，均继续 PARTIAL。详见 `ACCEPTANCE.md B123`。

## B122 报告完整性校验不能因 config 标记丢失而降级（2026-10-22）

- `/runs/{id}` 读取报告时，若同目录 integrity sidecar 存在，即使 `config.json` 的 schema 标记丢失，也继续校验 sidecar 并保持 verified/invalid 语义；config 声明未知 schema 直接 fail-closed。只有没有标记、没有 sidecar 且报告合法的历史 run 才标记 `unverified_legacy`。
- 新增真实 API 回归：执行报告与 sidecar 正常发布后删除 config 标记，读取仍校验并返回 verified；将 config marker 改成未知 schema 时返回 409。报告完整性专项 **8 passed**；合并 B120–B122 后端组 **553 passed、1377 warnings、15 subtests、42.10 秒**，退出码 0，见 `ACCEPTANCE.md B122`。
- **边界：** sidecar 与报告及 config 仍处于本机可写信任域；同一主体若同时删改 sidecar/config 仍可降级/伪造，未形成签名或独立存储。T44 仍 PARTIAL；无 Gateway/account/order 操作。

## B121 T39 feed timestamp 失配持久暂停；T40 PAPER 控制 API 绑定可信 scope 与可归因授权（2026-10-22）

- `PythonBacktestEventCoordinator.process_event()` 现严格拒绝非 `datetime`、naive 时间、异常 `tzinfo` 归一化；每类错误都先将 SQLite backtest journal 持久置 `PAUSED`，再抛稳定 `EventWorkerError`。游标不前移、worker=0，后续合法 retry 仍被暂停闸拒绝。新增 `None`、字符串、任意对象及异常时区测试。
- PAPER manager 新增 `start_for_principal()`，要求 attributable principal 和独立 `start` grant 后才可进入 manager startup/factory；API 增加 start/activate/cancel-owned-order/stop 路由。start scope 只由可信 app composition setter 安装，activate/cancel/stop 使用当前 manager active scope；撤单请求只允许本地 intent ID 并禁止额外账户、actor、grant、broker ID 字段。共享 key/loopback principal 在 manager 层被拒绝。
- 定向执行管理、routes、journal 和 wiring 组：62 passed；完整离线 execution/Backtrader/API/PAPER 组合 **551 passed、1371 warnings、15 subtests、42.43 秒**，退出码 0。前端 Execution 测试 7 passed，`npm run build` 退出码 0；`compileall` 与 whitespace/diff 检查通过，命令见 `ACCEPTANCE.md B121`。
- **边界：** T39 仍 PARTIAL（未完成全面内省/逃逸攻击矩阵）。T40 仍 PARTIAL：当前真实 API auth 只有共享 key/loopback，不具备人类归因身份；主 app 无 production runtime composition/factory/可信 scope，故 start 入口不会接入 Gateway。无 Gateway/account/order 操作。

## B119 发布后的 execution report 完整性重验（2026-10-22）

- trusted NL2 与 Python upload 的 `artifacts/execution_report.json` 发布后写入固定 schema 的同目录 sidecar，绑定规范路径、字节数和 SHA-256；`/runs/{run_id}` 读取新报告时重验 sidecar。缺失、截断、替换、摘要损坏或 JSON 非对象均标记 invalid 并返回 HTTP 409；合法但没有 sidecar 的旧 run 明确标为 `unverified_legacy`，畸形旧 JSON fail-closed。
- sidecar 使用原子替换；runner、Python upload service 及两种 API 创建入口均覆盖。完整离线回归证据见 `ACCEPTANCE.md B119`。
- **边界：** 报告和摘要位于同一主机/可写目录，无法抵御可同时修改二者的本机攻击者；不是签名、WORM 或独立信任域，也不消除共同 Backtrader 算法缺陷。T44 仍 PARTIAL；无 Gateway/account/order 操作。

## B118 SQLite 成功 migration 的备份可验证与旧版恢复证据（2026-10-22）

- 为 `apply_atomic_execution_database_migration()` 增加成功路径回归：migration 新表/数据与 `user_version +1` 在事务后落地；返回的 pre-migration backup manifest 可验证；从其恢复后旧 `user_version`、旧 schema 回来且新表不存在，恢复 receipt 要求 broker reconciliation 并将 execution gate 置 `RECOVERING`。
- `test_sqlite_backup.py` 5 passed；execution 子树 380 passed、256 warnings、15 subtests。与 B114–B117 合并的 execution/Backtrader/API/PAPER 子树 535 passed、1351 warnings、15 subtests、40.46 秒，退出码 0，见 `ACCEPTANCE.md B118`。没有外部依赖。
- **边界：** 这是本地 SQLite migration/backup/restore 成功路径测试，不涵盖定期或异机备份、加密/密钥、保留、操作员停机恢复演练或真实 broker reconciliation；T34 仍 PARTIAL。无 Gateway/account/order 操作。

## B117 非法 Backtest host cursor 在策略调用前持久 fail-closed（2026-10-22）

- `PythonBacktestEventCoordinator.process_event()` 对非正整数、bool/float/None 和超出冻结 verified bar 范围的 cursor 先持久 `mark_paused(run_id)`，再抛出 `EventWorkerError`。恶意/损坏 cursor 不会调用 worker 或前移 `next_seq`；该 journal 的后续合法 seq 也不能解除 PAUSED。
- 新增参数化 6 个非法值：`0`、`-1`、越界 `3`、`True`、`1.0`、`None`；逐例断言 worker=0、cursor 不动、PAUSED 持久和重试被拒绝。定向 `bt_runtime` 79 passed；与 B114–B116 同组完整 execution/Backtrader/API/PAPER 回归 **534 passed、1349 warnings、15 subtests、41.62 秒**，退出码 0，见 `ACCEPTANCE.md B117`。`compileall` 通过。
- **边界：** 这只覆盖 host coordinator 的 cursor 类型/范围 fail-stop，不补齐伪造合法 cursor、Python introspection 或完整容器逃逸矩阵；畸形/越界调用会暂停本次 backtest run。无外部数据/Gateway/account/order 操作。

## B116 IBKR commission 回报的 append-only 持久化与成交关联（2026-10-22）

- SDK commission callback 现进入可信 pump 的 fee-fact 路径；严格核对 execution ID、币种、scope 与有限金额，将原始 payload 规范化后在 SQLite 持久化。Exact payload replay 幂等返回同一 `fee_fact_id`；同一 scope/execId 的内容变化追加新 fact、将全部版本标为 `PENDING_REVIEW`，不覆盖旧记录。数据库 trigger 拒绝 UPDATE/DELETE。
- Commission 早于 execution 到达时保存为 `PENDING_EXECUTION`；成交回报入账后按 broker/account/environment/execId 动态链接。Durable spool 只有在 fee fact 提交后才 ACK。金额状态始终是 `REPORTED_UNCONFIRMED`，不送入 Backtrader commission 累计，也不宣称 broker-final。
- 定向 callback + ledger：74 passed；完整 `agent/tests/execution`：379 passed、260 warnings、15 subtests。随后与 B114/B115 及真实 Backtrader/API 子树一同复跑完整组：527 passed、1349 warnings、15 subtests，命令见 `ACCEPTANCE.md B116`。`compileall` 和 `git diff --check` 通过。
- **边界：** IBKR commission report 没有被视作权威最终费用；变化金额是新 evidence、需人工/后续 reconciliation 核对。尚无完整 account reconciliation、费用更正状态机、Backtrader commission/报告同步或 Gateway 实测。未连接 Gateway、读取账户或发送订单。

## B114 NL2/SMA 周期来源证明与已知条件预检（2026-10-22）

- SMA intent schema 新增 `fast_period_evidence` / `slow_period_evidence`；模型输出的每个周期都必须带原文短引文、匹配数值，并将数字与 fast/slow 角色就近绑定。缺失、伪造、值不符、角色重复或引文只同时包含但没有绑定数字与角色时 fail-closed。Preview 不发 token，也不创建 run/version。完整确定性 SMA 语法仍不依赖模型。
- 扩展中英文已知 unsupported 条件词族，涵盖额外指标/行情过滤、止损止盈、short、杠杆/保证金、仓位和风险规则、价格/日历/周期过滤；加入模型未被调用的测试。另有 API 用例模拟模型填默认 10/30 但无原文 evidence，确认以 clarification 拒绝且没有 token/run。
- 定向 parser/API/真实 SMA/RSI 回归并含 B115 readiness 用例：93 passed；加入数字与角色错配负例后，完整命令见 `ACCEPTANCE.md B114/B115`，520 passed、1349 warnings、15 subtests，退出码 0。
- **边界：** 引文校验只证明两个 SMA 周期来自带角色的原文片段，不证明模型没有漏掉非数值条件；有限词典也不覆盖任意表达、所有语言或委婉语义。没有真实 provider 解析验收；T12 继续 PARTIAL。没有行情/Gateway/account/order 操作。

## B115 PAPER status 与实际 manager readiness 启动闸保持一致（2026-10-22）

- `/execution` 使用 `manager.startup_readiness`（`start()` 实际执行的声明）生成 status。若 app state readiness 与 manager 绑定对象不一致，返回 `composition_ready=false`、追加稳定缺项 `readiness_binding_mismatch`，并按 manager 实际缺项报告；status 不再可能被另一份声明误报 READY。app 没显式 readiness、但直接安装了有绑定 readiness 的 manager 时，以 manager 声明补齐 app-state alias。
- 新增 readiness 漂移 status/start 一致性测试：manager 启动仍被 readiness gate 拒绝、factory 未调用、无 runtime slot；完整 setter 绑定的 ready status 也覆盖。定向 routes/state 20 passed。完整合并离线组 519 passed、15 subtests，见 `ACCEPTANCE.md`。
- **边界：** 同一 readiness 声明不认证其 provider 的真实身份/质量，不建立 production composition，不启动 SDK/Gateway，不读取账户或发送订单。

## B113 为未消费 preview 快照增加 SQLite 容量闸

- Snapshot JSON 单项受 80 MiB 上限保护，所有未过期/未消费的 `strategy_spec_json + market_snapshot_json` 总量限制为 256 MiB，SQLite 按 UTF-8 blob 字节数在 `BEGIN IMMEDIATE` 事务内汇总，避免并发 preview 同时突破上限。超出单项限值返回 413；超过活动总量时返回 429，授权 token 不创建，run/version 也不会创建。额度随 token 消费清理 payload 或 token 过期回收。
- 回归 `./.venv/bin/python -m pytest -q agent/tests/test_backtrader_backtest_api.py agent/tests/bt_runtime/test_python_upload_service.py --disable-warnings --tb=short` **退出码 0：16 passed、594 warnings、5.31 秒**；`compileall`、`git diff --check` 与交接文档 trailing-whitespace 检查通过。容量负例将测试进程额度临时降至现存快照约 2 倍，第二个同尺寸 preview 被拒绝，已有 token 仍可用；不是实际大文件压测。
- **边界：** 该额度限制 preview authorization active payload，不限制 SQLite page freelist/文件磁盘历史峰值或 runs/artifacts 总容量；仍需部署级磁盘配额/保留策略。没有外部数据/Gateway/account/order 操作。

## B112 Python upload create 从 preview 授权读取冻结行情快照

- Python upload preview authorization 现在保存 schema-versioned CSV、metadata、expected session schedule 和 snapshot SHA-256，同时绑定来源代码、策略 ID、成本和 worker image 的 confirmation request hash/review hash。
- Create API/UI 只需发送策略源码/标识、成本参数和 token；为兼容旧客户端，仍接受可选的 CSV/metadata/schedule 字段，但完全忽略这些确认时重传值。服务从 token 取快照、检查严格字段集和快照 hash、重建 request 并重新执行 importer，然后以现有 review hash 原子消费 token；损坏快照、修改后的策略/成本/worker 输入均在 run/version 创建前拒绝。确认后 report CSV 与 frozen snapshot 字节一致；run 的 metadata/schedule 用 importer 解析后的规范格式保留以支持 resume。
- 消费一次性 token 时立即清空 snapshot/spec 大 payload；新 preview 清理过期与已消费授权行，避免一直保留不用的数据。重复确认返回 409。UI 已改为不重传行情文件。API/service 命令 `./.venv/bin/python -m pytest -q agent/tests/test_backtrader_backtest_api.py agent/tests/bt_runtime/test_python_upload_service.py --disable-warnings --tb=short` **退出码 0：16 passed、594 warnings、5.23 秒**；UI **7 passed**、production build **退出码 0**。测试涵盖省略确认行情、故意重传不同 CSV/metadata/schedule 仍执行 exact snapshot、快照自身 hash 损坏与攻击者重算 snapshot hash 但 review hash 不匹配两种失败、run/version 创建前拒绝、消费后重放拒绝、worker image 绑定/恢复拒绝，以及真实 Backtrader API 路径。
- B112 合并后的完整离线 execution/Backtrader/API/PAPER routes/state 回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py agent/tests/test_state_migration_wiring.py --disable-warnings --tb=short` **退出码 0：478 passed、1351 warnings、15 subtests、41.56 秒**。测试不连接 Gateway、账户或订单。
- **边界：** 快照和授权保存在本机 SQLite，摘要用于检测意外损坏并与预览 review hash 复核，不提供对可写本机数据库攻击者的签名/防篡改证明；provider/calendar/adjustment 声明仍未认证。离线回测，不连接 Gateway/账户或发单。

## B111 Python upload 确认绑定 digest-pinned worker image

- `_prepare_python_upload` 现在验证 worker image 必须是 `@sha256:<digest>` 或不可变 `sha256:<image-id>`，并将精确镜像身份纳入 review hash。Preview 返回身份；缺失镜像仍无 token，格式无效时作为不支持的 preview 返回。
- Create 重新读取且校验当前镜像身份。与 preview 不一致时 hash/token 校验失败并返回 409，在 run/version 创建之前拒绝。成功身份写入 `config.json`、不可变 `artifacts/backtest_run_config.json` 与 execution report；恢复要求当前镜像与原 run 相同，且在恢复状态变更前校验。
- `/execution` Python 审阅卡现在显示 digest-pinned worker image。定向后端命令 `./.venv/bin/python -m pytest -q agent/tests/test_backtrader_backtest_api.py agent/tests/bt_runtime/test_python_upload_service.py --disable-warnings --tb=short` **退出码 0：16 passed、586 warnings、4.79 秒**；UI `npm run test:run -- src/pages/__tests__/Execution.test.tsx` **7 passed**，`npm run build` **退出码 0**；`compileall` 与 `git diff --check` 通过。
- **边界：** 锁定的是配置的不可变镜像身份，不证明镜像内容安全/来源可信或 Docker host 独立；Python upload 仍是离线回测，未访问行情服务、Gateway、账户或订单。T02/T37 保持 PARTIAL。

## B110 trusted NL2 预览与执行共用冻结行情快照

- Trusted SMA/RSI 预览现在一次抓取 provider bars，规范化成完整 XNYS session CSV，并通过现有 `import_completed_daily_csv` 校验；短期 preview authorization SQLite 保存 CSV、metadata、expected sessions 和摘要。review hash 绑定 data SHA-256 与 manifest SHA-256。
- 确认时从同一 token 载入 snapshot；Backtrader runner 不重新抓行情，重新执行 strict importer、核对 data/manifest hash，然后用这些 bars 运行真实 Cerebro/BackBroker。原 CSV 与 manifest 写入 run artifacts；`/execution` rule review 展示两个 hash。Python upload 仍使用同一 importer/service，两个入口的 manifest 和 completed-bar 校验底层共享。
- 定向真实 runner/API + manager/state 回归通过；完整 `agent/tests/execution + bt_runtime + backtest API + PAPER routes/state` 组 **478 passed、1339 warnings、15 subtests、41.30 秒**。Execution UI **7 passed**，production build 成功；`compileall` 成功。完整命令见 `ACCEPTANCE.md B110`。
- **边界：** provider response 在 preview 时抓取并冻结，便于确认和复现；但 provider label/source claim、授权、point-in-time availability、adjustments/corporate actions 和 symbol→broker contract mapping 仍未认证。使用本地可重复 fixture；未访问真实行情服务、未连接 Gateway/账户、未发订单。

## B109 app-scoped PAPER composition 拒绝替换活动 manager

- `_set_paper_execution_composition(..., app=app)` 对同一 manager/readiness pair 幂等；若当前 app manager `status()` 非空，替换抛 `PaperRuntimeConflict` 且旧 pair 保留；空 slot 才允许替换。避免 active runtime/lease 脱离 lifespan shutdown 的 manager 引用。
- 新增 active slot 保留/不停止旧 runtime、幂等注册与空 slot replacement 回归。定向 routes/state 组 **18 passed、9 warnings、2.82 秒**；随 B110 合并的完整后端组 **478 passed、15 subtests**。未改变默认 inert manager 或 readiness 缺项。
- **边界：** 这只保护 app-local 生命周期装配，不提供真实 providers/identity/grants/risk/account facts，不开启 PAPER。没有连接 Gateway、查询账户或发送订单。

# 实施进度

更新：2026-10-22（Asia/Shanghai）

## B108 IBKR 绑定订单 201 拒单落账

- IBKR `error()` 201 仅在 `request_id` 为正整数、命中 durable `BrokerOrderIndex`、binding account/environment 与当前 pump scope 一致，且 intent 仍为 SEND_STARTED/UNKNOWN/ACKNOWLEDGED、尚无成交时归一成 `BrokerEventKind.REJECT`。现有账本事务写拒单、更新 submission attempt 为 REJECTED、释放现金/SELL share reservation；pump 再将累计 REJECTED 状态交给 Backtrader native order。
- 未映射 ID、错 account/environment、部分成交、终态/其他状态冲突 fail-stop 并暂停。exact raw callback 重复会由已交付的 durable inbox 先识别并复用原 event ID；不同错误内容不会被当作同一拒单静默忽略。未把 IBKR 原始拒单字符串写入常规订单账本；SDK durable spool 保留原始 primitive callback 供审查。
- 验收：callback/pump/reconciliation/managed runtime 定向 **71 passed、15 subtests**；完整 execution/Backtrader/backtest API/PAPER routes/state wiring **475 passed、802 warnings、15 subtests、41.05 秒**；官方 isolated SDK suite **44 passed**。基线合同/数量/SMA **48 passed、210 warnings、0.59 秒**。详见 `ACCEPTANCE.md B108a–B108d`。
- 完整组第一次复跑 **474 passed、1 failed**：managed session integration 在 native order 完成后立即检查异步 durable callback ACK，撞到 consumer ACK 与 order completion 之间的时序窗口。单测复现后将断言改为显式等待 ACK，保留 3 秒硬超时；该用例单独及后续完整组通过。不是跳过、放宽业务断言或忽略失败。
- **边界：** 官方 SDK callback override 与 pump/ledger 用合成离线事件验证；没有真实 Gateway callback、account reconciliation 或 paper ack/fill。T23 仍 PARTIAL。

## B107 PAPER manager/readiness 绑定 FastAPI app

- `/api/execution/paper/status` 与 `/pause` 通过 `Request.app` 读取该 FastAPI 实例的 manager/readiness；`_set_paper_execution_composition(..., app=app)` 将 readiness 绑定到同 app manager。Lifespan shutdown 同样从当前 app 取 manager，确保状态路由、pause 与关闭操作使用同一实例。无 app 的既有 state helper 保留作为兼容接口，实际 routes/lifespan 不再走进程级兼容路径。
- 两个独立 FastAPI app 各自注册不同 readiness 和 manager，status 缺项互不串用；各自 lifespan shutdown 仅关闭本 app manager。相关 routes/lifespan/manager 组 **32 passed、9 warnings、2.85 秒**；包含 B106、Backtrader、upload/API/PAPER 的扩展后端组 **471 passed、804 warnings、15 subtests、40.36 秒**；`compileall` 与 `git diff --check` 通过。
- **边界：** 当前主 app 仍使用惰性默认 manager，没有 production composition root/provider；本切片只修复 app 实例边界，不启动 PAPER runtime，也不构成 Gateway/账户验收。

## B106–B108 IBKR SDK error/status callback 分类与拒单映射

- `IbapiCallbackAdapter.error(...)` 将官方 SDK 五参数 payload 持久化到 durable spool。Pump 忽略官方正常 farm status 2104/2106/2107/2108/2158；连接状态 1100/1101/1102/1300 要求暂停、重连与 reconciliation。
- B108 将 error 201 映射为 ledger REJECT，仅当正整数 request ID 命中持久化 broker-order binding、account/environment scope 一致、订单仍可拒绝且零成交。原子释放剩余 reservation 并通知 native Backtrader。未映射/错 scope/已有 fill/终态冲突及未知码仍 fail-stop；精确重复幂等。
- Pump/reconciliation/managed runtime 71 passed、15 subtests；完整后端组当时 475 passed，B110 后当前 478 passed；isolated SDK suite 44 passed。T23 仍 PARTIAL：无真实 Gateway callbacks 或账户对账。详见 `PROGRESS.md` 和 `ACCEPTANCE.md`。

## B105 Python upload import/API policy

- upload preview/create 共用 `_prepare_python_upload` 入口；resume 在将状态切回 running 前重验 frozen source；online `PythonEventCoordinator` 在 release/stage event 前验证；`ManagedPythonPaperSession` 在 composition 时验证冻结版本。
- Policy 仅允许 `collections`, `decimal`, `functools`, `itertools`, `math`, `statistics`；要求唯一、无 decorator、两个名为 `event/state` 的位置参数同步 `on_event`；拒绝其他/相对 import、AST 直接语法形式的动态执行与反射调用、源码直接引用 dunder 名称/属性。通用 `invoke_strategy_worker` 与 offline durable event journal 不做 source policy 检查，保留 lower-level adversarial isolation probes。
- 定向验证 `test_strategy_source_policy.py + test_event_worker.py + test_backtrader_backtest_api.py`：**41 passed、32 warnings、3.58 秒**。原合同/数量/SMA 基线：**48 passed、210 warnings、0.49 秒**。第一次完整 execution/backtest/API/PAPER 回归为 442 passed/11 failed/769 warnings/15 subtests：10 项确认是将 policy 错放到通用 worker 入口，导致离线测试和有意的攻击源 probe 被拦；修复为只在 upload/online PAPER application trust boundaries 校验。另一个是已过期 `_build_synthetic_manifest` 参数，更新为 `evaluation_start_index=1`。随后相同完整命令**退出码 0：453 passed、793 warnings、15 subtests passed、39.72 秒**。
- 真实 Docker `test_uploaded_code_runs_per_event_inside_network_and_host_file_isolation`：**1 passed、1.86 秒**。本地只启动隔离 worker image，无行情/Gateway/account/order。
- **边界：** AST policy 定义兼容 API 并早拒绝明显不支持的代码；它只识别 AST 中直接写出的危险调用，不解析函数别名或字符串表达式，也不证明 Python source 无法规避。Docker 仍是执行隔离边界。App PAPER composition inert。

## B104 IBKR PAPER endpoint/account pre-connect guard

- `IbkrPaperRuntimeConfig` 现在要求显式 endpoint profile，并且只接受 loopback + profile 对应的官方默认 PAPER socket：IB Gateway PAPER 4002 或 TWS PAPER 7497。LIVE 默认端口 4001/7496、未知/错配端口、远程 host 和非 `DU` 数字单账户格式均在 factory/本地 SQLite 创建前拒绝；managed runtime 自身也只接受 loopback 与两个 PAPER 默认端口，避免绕过 factory 时重新开放已知 LIVE/未知端口。
- `managedAccounts` callback 必须恰好返回目标单账户且没有重复或空项；不匹配时 durable callback 不 ACK、handshake 不完成、runtime 不请求账户 snapshot，因此不能进入 reconciliation/PAUSED。IBKR 允许修改 socket port，故本轮明确拒绝自定义端口；这条静态策略和 DU 格式只降低误连风险，仍不能证明端口背后的 Gateway 登录会话真实是 PAPER。
- 离线组合命令见 `ACCEPTANCE.md B104`，覆盖 known LIVE、未知端口、profile mismatch、非 loopback、非 DU/多账户 ID、额外/重复/空 managed account，以及失败后不开始 snapshot/recovery。无 SDK child/Gateway/账户连接。
- **边界：** app 仍默认 inert、paper readiness 不完整；没有真实账户/合约/预算/risk configuration，也未连接任何 Gateway。Custom endpoint/profile、账号类型的 broker-side attestation、生产身份与授权仍未实现；因此 T14 继续 PARTIAL。

## B103 trusted SMA/RSI 报告独立 BackBroker 复算

- Trusted provider runner 在第一次 Cerebro 回测后，将订单 decision_date 映射到 verified Bar 序号，生成带理由的 decision log；冻结策略版本与数据 manifest hash 后，以有界 JSON envelope 启动第二个 Backtrader/BackBroker 子进程复算。
- Verifier 对比订单执行事实、成交、资金/权益曲线、closed trades、费用、持仓和 metrics，并绑定 decision log、输入 bars、manifest、策略版本、佣金/滑点和 instrument。策略报告可保留额外 sizing 字段；只对执行核心字段逐项比较。报告 replay mismatch 时 runner 在创建/发布 `execution_report.json` 之前失败；支撑文件先落盘，最终 report 通过同目录临时文件 + fsync/replace 原子发布。Python upload 继续使用兼容别名，上传 importer 仍单独限制 1,000 根；共享 replay worker 最大 5,000 根以容纳 trusted 十年日线窗口，字节与时间上限仍生效。
- Python-upload verifier 对 status_history 中不稳定出现的合成 `Created` 状态做归一化，仍严格对比实际 callback 状态；同时将过期测试调用同步为 manifest helper 当前的 evaluation start index 入参。
- trusted SMA/RSI 成功路径、Python upload 兼容以及 mismatch 不发布报告均有回归测试。组合命令退出码 0，26 passed、232 warnings、5.84 秒；详细范围见 `ACCEPTANCE.md B103`。
- **边界：** 复算验证首跑捕获 decision log 后的撮合与会计一致性，不独立证明 SMA/RSI 信号计算正确；两次运行共享代码库/Backtrader 版本且 replay 在同机独立子进程，未认证市场数据供应商或账户 facts。全程本地 fixture，无行情/Gateway/account 连接。

## B102 PAPER manager 启动前强制 composition readiness gate

- `PaperExecutionManager.start()` 在调用 runtime factory 之前检查不可变 `PaperExecutionReadiness`；省略 readiness 时默认全缺项/拒绝。缺少 session builder、人类可归因授权器、账户对账事实、行情/合约或风险/成本任一项时抛出稳定 `PaperRuntimeNotReady`，不构建 runtime、不创建 DB、不获取 lease。
- `state._set_paper_execution_composition()` 将同一个 readiness 对象绑定到 manager 与 status；活跃 slot 存在时拒绝替换，避免 UI 状态和启动闸来源分离。完整声明只允许原有 `start_paused()` 恢复，仍要求最终状态 PAUSED 且 lease 持有；它不授权 activate 或逐单发送。
- 验收命令与结果见 `ACCEPTANCE.md B102`。**边界：** readiness 是可信 app composition 的静态声明，不认证数据/账户真实性，也不替代动态风控。当前没有生产 providers 或已注册 composition，app 默认仍 inert；没有连接 Gateway/账户。

## B101 PAPER SELL 本地股数预留与派单状态机

- 增加独立 SQLite `share_reservation`，以 broker/account/environment/instrument 为范围，基于可信策略持仓扣减已有本地 SELL 预留；`BEGIN IMMEDIATE` 中将 SELL 预留与最终风险复算、唯一 `SEND_STARTED` 一致性绑定。风险事实源给出的外部 SELL 预留会与本地 hold 相加，防止忽略券商/其他服务已挂卖单。
- 成交回报原子扣减剩余股数；UNKNOWN、ACK、部分成交、CANCEL_PENDING 与 CANCEL_REJECTED 保留未成交量；REJECT 或确认 CANCELLED 释放。活动 SELL 缺 reservation 的旧 ledger 启动迁移持久置 RECOVERING；最终发送事务再次查找同 scope 此类遗留订单。终态迟到 fill 记录后要求重对账，重放成交 ID 不重复扣减。
- 风险快照时间若与已记录 SELL fill 的时间戳相同也视为不够新，避免粗粒度时间戳无法证明快照已包含成交。
- 定向命令与结果记录于 `ACCEPTANCE.md B101`。**边界：** 本轮只证明离线合成 facts 与共享 SQLite 行为，不提供可信生产账户/策略持仓归属、IBKR open-order/provider facts、跨库/跨主机 reservation，也未连接 Gateway、读取账户或发送订单。T16 继续 PARTIAL，真实 PAPER composition 保持未就绪。

## B99 FastAPI lifespan PAPER runtime 安全关停

- `PaperExecutionManager.shutdown_active_runtime()` 作为 app 内部生命周期动作，不伪造用户 principal、不撤单、不清仓，只调用 manager 当前 runtime 的 `stop()`。只有返回成功且 durable control state 为 STOPPED、owner lease 已释放时才清除唯一 slot；失败、异常或状态校验不符时保留 entry，记录稳定错误类别供 status/recovery 使用。
- FastAPI lifespan 在关闭 scheduled/live/channel 服务之前，通过 `asyncio.to_thread` 执行 PAPER shutdown，避免阻塞 async event loop。未配置默认 manager 无 entry，shutdown 不触发 runtime factory。失败日志不输出账户、endpoint 或 broker 错误正文。
- manager/lifespan/live runtime 组合命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_paper_execution_manager.py agent/tests/test_state_migration_wiring.py agent/tests/test_api_live_runtime.py -q --disable-warnings`：退出码 0，**55 passed、9 warnings、3.02 秒**。覆盖无 entry 的 inert shutdown、RUNNING→STOPPING 后静默不足保留 lease、后续安全停止释放 slot、runtime exception 保留 entry/lease，以及 PAPER cleanup 先于 channel/scheduler teardown 执行；原 live runner teardown 回归也通过。
- **边界：** 当前 lifespan 不启动或自动恢复 PAPER runtime，只会停止显式注入且已经 active 的 manager entry。未静默时服务状态/lease 保留；若宿主进程在此后退出，下一进程仍必须依赖官方 IBKR reconciliation 才能恢复，不能假设已有 native BT order/ref 已重建。没有 Gateway、账户或订单操作。

## B98 manager-owned PAPER deployment factory 离线组合

- 新增 `ManagedPythonPaperDeploymentFactory`：先调用显式 runtime factory 构建未启动、未持 lease、绑定 IBKR/PAPER scope 的 `IbkrManagedPaperRuntime`，再由可信 session builder 绑定同一 runtime、scope 和共享 durable SQLite 的 frozen Python/Cerebro `ManagedPythonPaperSession`。构建仅创建本地对象；连接、恢复和取 lease 仍只发生在 manager 显式 `start()` 后的 `start_paused()`。
- `ManagedPythonPaperSession.cancel_owned_order(intent_id)` 精确转发至 runtime；管理授权仍由 `PaperExecutionManager` 检查，runtime 继续校验 intent scope、持久 broker-order binding 和取消状态。
- 恢复失败后的 session `stop()` 现在允许 runtime 在 `RECOVERING` 下执行安全 quiescence/stop，不先调用只接受 `PAUSED/RUNNING` 的 pause。新增组合测试证明 manager 管理真实 session 对象的启动至 PAUSED、授权 activate/pause/cancel/stop、错误 scope 与分离 DB 拒绝、builder 失败不启动，以及恢复失败时 slot/lease 保留并可清理。
- 后端组合命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_managed_python_paper_deployment.py agent/tests/execution/test_paper_execution_manager.py agent/tests/execution/test_managed_python_paper_session.py agent/tests/execution/test_ibkr_paper_runtime_factory.py agent/tests/execution/test_ibkr_managed_runtime.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/test_paper_execution_routes.py -q --disable-warnings`：退出码 0，**76 passed、27 warnings、3.54 秒**，pytest 进程正常退出。另 `py_compile` 与 `git diff --check` 通过。
- **边界：** 这是 manager 接口上的离线 Python deployment composition primitive，尚未在 app startup/state 注册；builder/runtime factory 及 account profile、可归因授权、账户与行情事实、风险规则和 production contract mapping 都没有应用级可信配置源。T40/T43 与 P3a 仍 PARTIAL；没有启动本地 SDK child、连接 Gateway/账户、读取账户或发送订单。

## B97 离线 journal 前缀扰动续跑 + PAPER readiness 解释

- 在 `agent/tests/execution/test_strategy_worker_docker_integration.py` 增加真实 Docker worker + Python upload service + Cerebro/BackBroker 集成验收。两份独立 frozen run 共享前三根 bar、使用不同未来后缀；各自提交共同前缀后模拟服务中断，关闭并重开 SQLite journal/coordinator，再恢复完整 Cerebro 回测。重放最后一条已提交事件复用 journal 结果且不重复调用 worker；只计算后两根 bar。两份 run 的 prefix bars/state/proposals/checkpoint/orders/fills 一致，完整终值随后缀不同；不同 run config hash 被 journal 拒绝并持久 PAUSED。
- 定向 Docker 命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_cerebro_suffix_perturbation_is_invariant_after_reopened_sqlite_prefix -q --disable-warnings`：退出码 0，**1 passed、36 warnings、6.91 秒**。`py_compile` 与 `git diff --check` 通过。
- `/execution` 对已注册但未完整的 PAPER composition 逐项显示五个缺失能力的说明；九种 locale 均增加翻译。Execution UI **7 passed**，`npm run build` 退出码 0，`git diff --check` 通过。
- **边界：** T04 仍 PARTIAL，未覆盖更多伪造 cursor、内省与容器逃逸组合；合成离线数据不证明 provider 行情真实性。UI 说明是 readiness 诊断，不提供账户事实源或控制身份。没有 Gateway、账户或市场服务活动，也未发送订单。

## B96 RSI 严格 NL2 意图抽取

- 为 RSI 增加独立的 `RsiThresholdIntent` JSON schema，限定 period、entry/exit threshold、固定 entry/exit rule、long-only、unsupported conditions 和 clarification；模型不能生成代码、StrategySpec 或任意参数。
- 模型抽取的 period/阈值必须各有一段能在原始用户描述中逐字找到的 evidence quote，quote 内的数字与字段精确匹配，且包含对应的周期/下穿/上穿语义。显式完整句式仍走确定性 parser；语义非法的完整句式不会被模型重解释。缺项、混合 SMA/RSI、未支持条件、额外字段、错误 crossing/direction、非法阈值和伪造/不匹配 evidence 均 fail-closed。缺少 provider 时退回指出需明确 period/threshold 的固定提示，不发 preview token。
- `/runs/backtests/preview` 把严格 intent 产物编译为 StrategySpec；API 测试确认 create 消费一次性 token 内冻结的 spec，不会再次调用模型。preview 前模型只被调用一次。
- 后端命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/bt_runtime/test_rsi_backtest.py agent/tests/bt_runtime/test_sma_backtest.py agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/execution/test_execution_backend_routing.py -q --disable-warnings`：退出码 0，**70 passed、366 warnings、6.15 秒**。详细证据见 `ACCEPTANCE.md B96`。
- **边界：** 模型生成的 quote 能验证字段数值确实来自用户原文，不能证明模型完整理解或穷举了用户所有隐藏条件；需要由用户在规则卡核对。尚无真实 ChatLLM provider 响应验收、RSI 多轮对话状态/继续补问体验或线上意图遗漏矩阵。无真实行情/Gateway/PAPER 操作。

## B95 第二受信任模板 RSI 首个离线切片

- 在 `CONTRACTS.md §2.2` / `DECISIONS.md D-054` 冻结 `rsi_threshold/1.0`：SMA seed 的 Wilder RSI、两均值为零时取 50、period+2 bars 的交叉暖机、严格下穿买入/严格上穿退出、等于阈值不触发、首个有效值不追单；period 与 entry/exit 阈值均要求显式给出。
- trusted compiler 使用真实 Cerebro/BackBroker 执行 RSI，支持固定 `{period, entry_below, exit_above}` 参数集合；runner 使用模板 warmup/evaluation metadata，报告返回通用模板参数，并按冻结 spec 保存不可变版本。
- `/runs/backtests/preview` 支持完整显式 RSI 阈值句式；preview token 绑定同一 StrategySpec、compiler identity 和完整输入，create 直接复用冻结 spec。缺参数、混合 SMA 条件和非法阈值继续 fail-closed。SMA 仍保留原来的受控 ChatLLM intent 解析；RSI 当前尚不支持任意模型生成/多轮澄清。
- `/execution` 审阅卡根据 template 显示 RSI 周期/阈值及严格穿越说明，保留原 SMA 审阅语义。
- 后端命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/bt_runtime/test_rsi_backtest.py agent/tests/bt_runtime/test_sma_backtest.py agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/execution/test_execution_backend_routing.py -q --disable-warnings`：退出码 0，**58 passed、366 warnings、9.08 秒**。原 contracts/position-sizing/SMA 基线最后复跑 **48 passed、210 warnings、0.80 秒**。Execution UI **6 passed**，`npm run build` 成功；记录见 `ACCEPTANCE.md B95`。
- **边界：** 后端 provider runner 通过本地 fixture 验证，不下载行情、不核验来源真实性；RSI 只支持完整显式固定句式，未支持 RSI 模型抽取、多轮澄清或通用漏项检测；未连接 Gateway/账户、未发任何订单。

## B94 Python 上传失败反馈安全归一化

- worker 失败协议仅输出固定类别、白名单内置异常名和 `<uploaded-strategy>` 源码行号，不包含异常消息、traceback 或用户策略 stdout。Docker launcher 对非零退出中的 JSON 结构和字段做白名单校验，并将它转换为受信任的 typed failure；其他 worker/基础设施失败继续走通用错误路径。
- Cerebro 运行错误携带安全元数据到 API。初次运行和 resume 共用转换：策略运行错误返回 HTTP 422，并在 `state.json` 写入 `error_code/event_seq/error_type/source_line/message`；message 由 host 模板生成。任意 worker 文本不会进入 API detail 或持久状态。
- 真实 Docker 用 NameError + `SECRET-MARKER` 验证 worker 只暴露错误类型和第 3 行，敏感文本与 traceback 不在异常字符串中。API 故障路径用例验证第 1 根 bar 的稳定反馈和状态落盘；worker/API/Python upload/Cerebro 相关组合：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_python_sandbox.py agent/tests/execution/test_event_worker.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/test_backtrader_backtest_api.py agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_cerebro_prefix_results_are_invariant_to_future_suffix agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_worker_reports_bounded_strategy_error_without_message_or_traceback --disable-warnings`：退出码 0，**45 passed、178 warnings、17.22 秒**。
- `npm run test:run -- src/pages/__tests__/Execution.test.tsx`（`frontend/`）：退出码 0，**5 passed**；新增确认 API 安全错误字符串能显示 bar 和源码行，原始异常正文不会进入文案。保留既有 Vite config/native loader 与 Node localStorage warning。
- `npm run build`（`frontend/`）：退出码 0；TypeScript 与 production build 成功，保留既有大 chunk warning。
- **边界：** 当前只定位常见内置异常类型与可用源码行；unknown/custom exception 归为 `OtherError`，不会给任意异常文本。仍需更多策略 contract 负例。未改策略能力、未连接 Gateway/账户。

## B93 T04 未来后缀扰动端到端验收

- 新增真实 Docker worker + importer + Cerebro/BackBroker 测试：同一冻结 Python 策略在相同前三根已完成 bar 下运行两次，仅改变后两根 bar。两次的完整决策日志一致，截点前订单和成交一致；整段成交历史也一致，而最终净值按未来后缀变化，证明前缀输出没有被后缀回写。
- 命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_cerebro_prefix_results_are_invariant_to_future_suffix --disable-warnings`：退出码 0，**1 passed、24 warnings、6.93 秒**。
- T04/T39 继续为 **PARTIAL**：用例让完整服务两次创建并写入各自 durable journal，但没有模拟在同一运行中途关闭/重开 journal 后再执行扰动矩阵；伪造 cursor、内省和容器逃逸的完整组合也未覆盖。输入均为离线合成 fixture，不构成真实行情或 PAPER 证据；没有 Gateway/账户活动。

## B92 T03 SMA 零差值与初始持仓边界验收

- 新增真实 Backtrader/Cerebro 用例，采用 fast=2、slow=3 的手算序列覆盖差值 `-1→0→+1→0→-1`：相等本身不产生信号，只有 `0→正` 买入、`0→负` 卖出；断言决策日期及下一 bar 开盘成交日期和整股数量。
- 新增初始状态已 bullish 的序列：flat 状态不追买，只有后续死叉/再金叉交易；另以显式合成 7 股初始 BackBroker Position 验证不因初始 bullish 关系误交易、死叉只卖出原有 7 股、再金叉才重新入场。持仓在 `Strategy.start()` 通过 Backtrader 原生 `Position.set()` 注入，并从现金扣除相应 7 股成本；后续 order/fill 均由真实 BackBroker 撮合。
- `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_sma_backtest.py --disable-warnings`：退出码 0，**9 passed**。交接基线组合 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**48 passed、210 warnings**。
- 因新增证据与契约一致，T03 更新为 **PASS（受信任合成切片）**；其余数据真实性、策略入口和 PAPER 验收不因此升级。未修改生产策略语义、未连接 Gateway/账户。

## B91 IBKR 恢复快照事实摘要验证

- `SQLiteIbkrSnapshotStore.complete_snapshot()` 读取每条 account/position/open-order 事实时重算规范 JSON 与 SHA-256，拒绝 hash 不符、非规范/损坏 JSON 或未知事实类型；并基于有序 kind/fact_key/hash 集合生成 `facts_sha256`。
- `IbkrRecoveryReconciler` 将 `facts_sha256` 纳入持久 reconciliation proof。后续再次读取已 reconciled 快照时，必须同时匹配 snapshot/account/environment 和相同事实集摘要，阻止对账后被改写的快照用于 PortBroker hydration。
- 验收命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_reconciliation.py agent/tests/execution/test_ibkr_managed_runtime.py --disable-warnings`：退出码 0，**15 passed**。包含保留原 hash 的采集后损坏、重算单行 hash 但不匹配已有 proof 的已对账后篡改；前者确认 allocator 不会开放。
- **边界：** 数据库内 SHA-256 检查能发现持久化损坏/不一致，不提供对能够同时改写数据和 proof 的恶意本机用户的防篡改保证；真实 Gateway/账户恢复未验收。

## B90 终态订单迟到成交触发账户恢复闸门

- `SQLiteOrderLedger.record_broker_event()` 收到已有 CANCELLED/REJECTED intent 的新 FILL 时，在应用原子事务内同时写入成交事实并将精确 broker/account/environment 的 `execution_control` 置为 `RECOVERING`，原因 `late_fill_after_terminal_order`。不会重开旧 intent 或恢复其 reservation；scope 后续派单须先经 broker reconciliation 恢复到 PAUSED，再由显式授权激活。
- 回归覆盖 CANCELLED 与 REJECTED 两类迟到成交，并在真实 callback pump 流程确认累计 fill 后仍进入 RECOVERING；dispatch service 测试验证同 scope 新 intent 的最终发送闸门返回 `execution_not_running`、不创建 attempt。
- 定向命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_order_ledger.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_dispatch_service.py --disable-warnings`：退出码 0，**66 passed**。
- 执行子树回归 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution --disable-warnings`：**291 passed、1 failed、15 subtests passed**；唯一失败为无关的 `test_bounded_pipe_helper_round_trips_json_without_shell` 在 fake shell 的 2 秒超时内未退出。立即单独复跑该项同命令追加 `agent/tests/execution/test_python_sandbox.py::test_bounded_pipe_helper_round_trips_json_without_shell`：退出码 0，**1 passed，0.56 秒**。整体子树因此不记为全绿。
- **边界：** 该本地 durable gate 会阻止新派单，但不自动修正进程内真实账户/持仓快照，也不自行运行完整恢复流程；managed runtime 必须通过现有可信 recovery 流程重新对账。没有真实 PAPER 回报验收或 Gateway/账户活动。

## B89 PAPER 风险降低型紧急暂停

- 新增认证 `POST /api/execution/paper/pause`。接口没有请求体；由服务端从 `PaperExecutionManager.status()` 取得 active scope，再用固定 reason 调用 manager pause，忽略调用方尝试附带的账户、订单、身份或 reason。无 active runtime 返回 409；runtime 未确认持久暂停也返回通用冲突，不泄露账户 scope。
- 该操作只减少风险：派单 gate 持久转为 PAUSED，托管 runtime 和 callback/reconciliation 仍保持运行，不会撤销现有委托、不释放 lease、不停止策略服务。`/execution` 仅在读取到 gate RUNNING 时显示“暂停新增派单”按钮；UI 明示不会撤单，成功后刷新状态。
- 后端验收 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**8 passed**；包括伪造请求体不改变 trusted active scope、无运行时拒绝，以及真实 `PaperExecutionManager` + SQLiteOrderLedger 持久 gate 转 PAUSED、owner lease 保留且不调用 cancel。前端 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：**4 passed**；`npm run build`：退出码 0（保留既有 Vite 大 chunk warning）。
- **边界：** 该请求仍使用 shared-key/loopback API auth，因为 pause 是单向收紧风险的操作；它不是可归因人类控制，也不授权 activate/cancel/stop/flatten。后续可归因控制仍需身份来源和持久 per-action grant。无 Gateway/账户活动。

## B88 在线 Python execution journal checkpoint 恢复完整性

- `SQLiteEventJournal.get_checkpoint()` 现在在 `BEGIN IMMEDIATE` 下校验 checkpoint/result 的严格 JSON（重复 key、NaN/Infinity、非对象均拒绝）和 checkpoint 规范序列化；cursor 必须是正整数，且不存在 cursor 之后的已提交 result。
- 对于 cursor 前一事件，必须同时存在已提交 event/result 且 event ID 一致；重算并比对 result SHA-256，严格解析 `WorkerEventResult`，要求 result JSON 是规范序列化、payload event ID 匹配，并要求恢复 checkpoint 与前一已提交 `result.state` 完全一致。任一完整性故障在同一事务持久设 `PAUSED` 后抛 `JournalError`，因此 `PythonEventCoordinator` 不会把被篡改状态传给 sandbox。
- 回归覆盖合法 JSON state 篡改、重复/非规范 JSON、非法 cursor、raw hash mismatch、event ID mismatch、缺少 prior result、result at/beyond cursor、正常重开及幂等重放；worker restart tamper 用例确认 sandbox 调用数不增加。完整命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_event_journal.py agent/tests/execution/test_event_worker.py agent/tests/execution/test_paper_event_bridge.py --disable-warnings`：退出码 0，**33 passed**。其中 bridge 组继续使用真实 Backtrader Cerebro/PortBroker，不以 mock 替代 Backtrader。
- **边界：** T41/T42 仍 PARTIAL。checkpoint JSON 状态可验，但没有恢复进程内 Backtrader native indicator/active Order/order-ref registry；存在未决订单、pending callback 等状态仍按既有规则 fail-stop。未连接 Gateway 或账户。

## B87 SQLite execution 数据库备份、恢复与迁移回滚

- 新增 `sqlite_backup.py`：用 SQLite online backup API 在数据库仍处于 WAL 模式时获取事务一致快照；备份目标独占新目录，完成后将副本 checkpoint 并切为 DELETE journal mode，形成 `execution.sqlite3` 单文件与最后写入的 `manifest.json`。manifest 绑定数据库 SHA-256/字节数、user_version 和 schema 摘要；验证同时检查文件集合、哈希、schema、`integrity_check` 与 `foreign_key_check`。
- 恢复只允许安装到不存在的新路径，原子 no-replace 写入；若有 `execution_control` 行，将所有 scope gate 改为 `RECOVERING` 并标明 `backup_restore_requires_broker_reconciliation`。恢复 receipt 明示必须完成 broker reconciliation，不能直接恢复到 RUNNING。
- `apply_atomic_execution_database_migration()` 先生成并验证迁移前备份，再在 `BEGIN IMMEDIATE` 中执行 SQLite-only 回调；成功前检查完整性，异常时事务回滚且保留可验证备份。调用方须先停止应用/runtime；迁移回调不得自行 commit 或关闭连接。
- 定向验收：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_sqlite_backup.py --disable-warnings`：退出码 0，**4 passed**。包含活动 WAL 已提交事务、订单/成交与持久 mapping、pending callback、journal checkpoint/outbox 的恢复；恢复后 gate 为 RECOVERING；损坏/schema mismatch 拒绝、现存目标不覆盖、失败 DDL 回滚。
- **边界：** T34 仅 PARTIAL。没有运行实际部署的定时/异机备份、操作员恢复演练、生产 SQLite 占用/停机程序、备份保留/加密策略、恢复后的真实券商对账或账户准入；SQLite 备份只恢复本地证据，不代表 broker state。没有连接 Gateway 或账户。

## B86 策略线程之外的 PAPER owned-order 撤单

- `PaperExecutionManager.cancel_owned_order(scope, intent_id, principal)` 增加独立 `cancel` action authorization；非归因 principal、无 cancel grant、scope 不匹配或 manager 不处于 READY 均不会调用 runtime。控制输入只接收本地 intent ID，不接受券商 order ID 或 IPC payload。
- `IbkrManagedPaperRuntime.cancel_owned_order()` 只允许持 lease 的 RUNNING/PAUSED runtime；校验 intent 为当前 IBKR/PAPER/account，且 durable broker mapping 的 intent/account/environment/symbol/side/quantity 对应一致。ledger 原子创建唯一 cancel attempt 并进入 CANCEL_PENDING 后才调用 SDK subprocess；IPC 异常会持久标记 UNKNOWN，任何后续取消请求要求先 reconciliation，不会自动重发。取消完成须由 `orderStatus=Cancelled/ApiCancelled` callback 更新账本；stop 仍是单独动作。
- 测试新增 manager 的 attribution 与独立 cancel grant 用例；managed runtime 用 fake IBKR IPC + SQLite + callback worker 验证 exact mapping、确认回报、unknown 后收到迟到确认，以及 UNKNOWN 状态不得重发。完整相关定向命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_execution_manager.py agent/tests/execution/test_ibkr_managed_runtime.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings`：退出码 0，**54 passed**。
- **边界：** 本轮不增加 FastAPI control route；当前 API auth principal 不可归因。用户不能从 UI 发出该动作，真实订单/撤单确认未验证。仍需人类身份认证/grant provider 与生产 app factory；无 Gateway/账户操作。

## B85 PAPER app composition readiness 状态接缝

- `PaperExecutionReadiness` 描述可信 app composition root 已显式安装的策略 session builder、可归因控制授权、账户/恢复事实、行情/已验证合约和风险/成本事实。API status 现分别报告 `runtime_factory_registered`、`composition_ready` 与稳定的 `missing_requirements` 码；保留旧 `configured` 字段作为兼容别名。惰性默认还会列出 `ibkr_runtime_factory` 缺项，composition ready 要求 factory 与其余能力同时存在。
- 新增 `_set_paper_execution_composition(manager, readiness)` 作为可信 composition root 注册 seam，只设置对象，不启动 runtime。 readiness 仅用于状态诊断，不授权控制、不证明 provider 内容真实，也不会使 `dispatch_gate_open` 变真。Execution 页面不再把裸 factory 显示为“已配置但停止”。
- 定向验收：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/test_paper_execution_routes.py agent/tests/execution/test_paper_execution_manager.py --disable-warnings`：退出码 0，**12 passed**。相关模块 `compileall` 与 `git diff --check` 通过。
- **边界：** 仍没有实际 composition root、PAPER profile、授权身份、账户/行情/合约/风险 provider 或管理控制 API；status 的 readiness 声明必须由未来可信装配方在验证依赖后填写。未连接 Gateway、未查询账户、未发送订单。

## B84 durable backtest checkpoint 与已提交结果一致性

- `SQLiteBacktestEventJournal` 现在在每次使用 journal 前，在 `BEGIN IMMEDIATE` 事务内校验 `checkpoint_json`：首事件必须为空对象；后续事件必须匹配上一个已提交 event 的有效 SHA-256 结果、严格 JSON/`WorkerEventResult`、事件 ID 和 state。使用规范 JSON 比较，`1` 与 `1.0` 不会被 Python 宽松数值相等误认为相同状态。
- 检查覆盖新事件释放、未提交事件重试以及重放旧的已提交事件。checkpoint malformed、值变化、前一结果缺失/损坏都会在同一事务持久标记 `PAUSED`，先于任何 worker 调用抛错；无需 schema migration。
- 定向组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_backtest_event_journal.py agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_worker_sma_checkpoint_resumes_from_reopened_backtest_journal --disable-warnings`：退出码 0，**11 passed**。相关最终回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**353 passed、571 warnings、15 subtests passed**。
- **边界：** B84 收紧离线 checkpoint 完整性，T41/T42 仍 PARTIAL；不恢复 Backtrader native indicator/Order/ref，不验证 online dispatch 与外部 broker 状态原子性。没有 Gateway 或账户交互。

## B83 T41 有状态策略事件后的 SQLite 重启续跑

- 新增真实 Docker worker + 两根合成 completed Bar + `SQLiteBacktestEventJournal` 用例。策略将 close 窗口、rolling SMA(2) 与决策保存在 JSON state；先运行完整两事件参考，再单独运行事件 1、重新打开 journal/coordinator 后继续事件 2。
- 重启续跑第二事件的 checkpoint 与 BUY proposal 和不中断运行完全一致；重放已提交事件 1 复用 SQLite 结果，worker 调用计数不变。没有更改 production 代码。
- 定向 Docker 命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_worker_sma_checkpoint_resumes_from_reopened_backtest_journal --disable-warnings`：退出码 0，**1 passed、3.09 秒**。相关子树最终回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**349 passed、569 warnings、15 subtests passed**。
- **边界：** T41 仍 PARTIAL；该用例证明 worker JSON state 的 durable event continuation，不覆盖 Backtrader native indicator/Order/ref、账户状态或 paper/live 进程恢复。没有 Gateway 或账户交互。

## B82 复算 IPC 的输入/输出资源上限

- 报告复算子进程现在限制最多 1,000 根 bar/decision、每条决策理由 1,024 字符、JSON 请求 8 MiB、stdout 8 MiB、stderr 64 KiB，运行时间仍有限制。父进程用可选择的非阻塞 pipe 读写并监视时间；超时或超出任一输出上限会终止子进程并拒绝报告。
- Worker proposal `reason` schema 限为 1,024 字符，避免单条不可信策略结果放大为极大的 host decision log。
- 新增超限拒绝与 stdout/stderr 输出洪泛杀进程测试。定向组 `./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_report_replay_subprocess.py agent/tests/bt_runtime/test_python_upload_backtest.py --disable-warnings`：退出码 0，**13 passed、122 warnings**。相关后端子树复跑：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**348 passed、571 warnings、15 subtests passed**。
- **边界：** 限额降低上传策略通过超长理由制造 host 进程内存/pipe 消耗的风险；子进程使用最小环境变量 allowlist，但仍以同一 OS 用户运行，不构成独立身份或沙箱。真实行情/纸面账户边界均未变化。

## B81 Python upload 报告复算移至独立 Backtrader 子进程

- Python upload 可信报告现在将 OHLCV feed、importer 验证的 Bar records、host decision log、初始现金、佣金/滑点、manifest hash 和 instrument ID 序列化为严格 JSON；规范请求摘要随协议发送给单独启动的本地 Python 子进程。
- 新增 `report_replay_worker.py`，在子进程中重建输入、验证请求摘要，并运行一套全新的真实 Cerebro/BackBroker 复算。父进程校验协议、请求摘要、结果摘要及不同 PID；启动失败、超时、输入陈旧、响应结构或摘要不符均 fail-closed。完整性报告现声明 separate local process。
- 定向验证：`./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_report_replay_subprocess.py agent/tests/bt_runtime/test_python_upload_backtest.py --disable-warnings`：退出码 0，**10 passed、122 warnings**。覆盖真实子进程和 BackBroker、陈旧输入、伪造结果摘要及同进程响应拒绝。
- 与 B80 T39 canary 一起的相关后端总回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**345 passed、571 warnings、15 subtests passed**。
- **边界：** T44 仍 PARTIAL。独立子进程复算减少了用户策略对复算器的同进程干扰，但不是 artifact 签名或防篡改存储，不排除共享 Backtrader 算法错误，也不证明供应商行情、收益或 paper 准入。没有连接 Gateway 或账户。

## B80 T39 future-only canary 内省探针

- 新增真实 Docker worker + importer + Backtrader/Cerebro 集成用例。随机 9 位数值只存在于冻结 CSV 的第二根 bar；第一根 bar 执行时，真实策略 worker 扫描受限 Python 调用栈、模块引用、GC 对象图和 `/proc` 环境/命令行/maps/文件描述符可见内容。
- 外层测试确认 canary 只属于第二根导入 Bar，worker source 和第一根 bar 不含该值；真实 Docker 逐事件 runner 正常处理全部 bars，first-event probe 未发现该数值，完整 BackBroker 报告成功。若对象扫描达到上限或发现 canary，测试失败。
- 定向命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_real_worker_cannot_introspect_future_bar_canary_from_first_cerebro_event --disable-warnings`：退出码 0，**1 passed、8 warnings**。
- **边界：** T39 仍 PARTIAL；这是受限对象图和容器文件面探针，不覆盖所有 Python introspection、cursor 变体、容器逃逸或任意动态构造的编码读取路径。未发现可复现泄漏，不等于证明不存在未来数据泄漏。未连接 Gateway 或账户。

## B79 P2 受控自然语言 SMA 意图解析

- 保留固定 SMA 句式的确定性解析；其他自然语言尝试使用应用当前配置的 `ChatLLM` 抽取严格 JSON 中间意图。解析 schema 含 fast/slow period、entry/exit 规则枚举、long-only direction、unsupported conditions 和 clarification；禁止额外字段、非 JSON 包装、不完整 period、period 颠倒、其他交易语义和模型工具调用。模型结果只用于受信任固定 `sma_cross` 模板参数，不能提供代码或任意 `StrategySpec`。
- 若 provider 不可用、输出无法解析、字段不合规、信息缺失或报告不支持条件，则 preview fail-closed 并返回说明，不发 token、不创建回测。已知 RSI/其他指标、止损止盈、做空、成交量等关键词由确定性预检拒绝；提示模型可能漏提用户原文中的其他条件。`/execution` 审阅卡同时展示用户原始描述和规范化规则，便于检查是否遗漏意图。
- Preview token 授权表迁移为保存原始请求 hash 与短期规范 spec JSON；创建回测先按 token 和请求 hash 取出 preview 时的 spec，再复算 review hash 并一次性消费授权，不重复调用 LLM。Python upload 继续使用原来只返回真假值的 token 消费路径。
- 回归测试新增 fake intent generator 的 schema/交易语义/unsupported/provider-independent 拒绝用例，以及 API preview→create 只调用生成器一次并复用冻结 spec 的用例。API 中英混排 RSI 负例发现 ASCII 词边界不能匹配相邻汉字；改为 ASCII 字母数字 lookaround，并让确定性已知 unsupported prefilter 在无 token create 入口直接 422 拒绝，不触发模型。
- `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**19 passed、58 warnings**。相关后端子树 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_strategy_design.py agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**340 passed、703 warnings、15 subtests passed**。
- `cd frontend && npm run test:run -- src/pages/__tests__/Execution.test.tsx`：**3 passed**；`npm run build`：退出码 0，Vite 的 configLoader/native 与大 chunk warning 保留。`git diff --check` 通过。
- **边界：** 这是一次请求内的受控意图抽取与显式规则确认，不是多轮对话，也不保证模型穷尽识别任意自然语言中的所有条件；原文和规则卡必须共同审阅。测试通过不等于实际配置 provider 可用或正确解析所有用户意图。T12 仍 PARTIAL。未触及券商、账户或 paper 写入。

## B76 verified bar release 与回测输入 provenance 绑定（2026-10-21）

- Python upload 离线 `PythonBacktestEventCoordinator` 不再接受调用方每次提供 `(seq, Bar)`；可信 artifact service 初始化 coordinator 时传入 importer 验证的 `bar_records` 不可变副本。每次只按 `seq` 从该序列选出当前 Bar；跳过序号由 durable journal 的 `next_seq` 拒绝，范围外序号在 worker 前拒绝，事件接口本身无法注入另一根 Bar。
- importer 在 `DataManifest.quality_report` 写入规范化策略输入 schema 与 digest（时间戳及 Backtrader 实际消费的 OHLCV 浮点值）；原始 `file_sha256` 继续只表示上传 CSV 原始字节。受信任 SMA 及 Python upload 真实 importer runner 在进入 Cerebro 前重算并比较 digest，防止用旧 manifest 换同范围数据。
- coordinator 在释放前再核验 Backtrader 当前 feed UTC 时间与固定序列中该 seq 的 `available_at` 完全一致；naive 或错位时间会在 worker 调用前 durable pause。真实 Backtrader 实测的 UTC numeric date API 返回 naive datetime，trusted shell 按该引擎约定显式附 UTC，再交给 coordinator strict aware-time check。
- 负向/重放覆盖协调器先请求 seq=2 时 worker 调用为 0、journal cursor 不推进；feed timestamp 错位/naive 时 worker 调用为 0 且 journal PAUSED；Backtrader float-date 的微秒舍入在严格 50µs 窗口内接受；API 不接受额外 Bar 参数；策略输入 close 被替换或 digest 被伪造时拒绝。定向组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/bt_runtime/test_market_data.py agent/tests/bt_runtime/test_backtest_event_journal.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/bt_runtime/test_python_upload_service.py agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**30 passed、335 warnings**。
- 增加 `test_verified_bar_cursor_journal_and_real_docker_worker_share_one_cerebro_run`：真实 Docker worker 与 Backtrader/BackBroker、importer 输出、PythonBacktestEventCoordinator、SQLite journal 同一条回测路径运行；策略尝试 `event["bar"][1]` 被拒，父 worker request 只含一根 bar，不含 bars/future_bars/feed/dataframe/cursor；持久 journal 中的三根 bar 严格对应日序列。定向 Docker 测试 **1 passed、16 warnings**。
- 相关后端子树最终复跑：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**330 passed、673 warnings、15 subtests passed**。
- **边界：** T39 仍为 PARTIAL：组合路径现在真实运行并核对当前 Backtrader timestamp；仍缺多个伪造 host cursor 变体、全面 Python introspection 和所有逃逸攻击验收。输入 digest 证明数据被改后会脱离 manifest，不认证数据供应商、授权、point-in-time 历史可得性、调整口径或真实行情质量。未连接 Gateway 或账户。

## B75 Python upload preview / confirm（2026-10-21）

- 新增认证 `POST /runs/backtests/python-upload/preview`：只解析上传源码 AST、校验顶层同步 `on_event`，并通过严格 CSV/calendar importer 生成 manifest、StrategySpec 与输入哈希；预览不创建 run 或不可变 strategy version。摘要显示行情区间、bar 数量、现金/佣金、source/CSV/manifest/spec hash、Backtrader 与 worker protocol 版本；provider/calendar/adjustment 标注仍显示 caller-declared unverified。
- Preview token 保存 hash、不保存明文，只能使用一次且 10 分钟过期；确认请求 hash 覆盖整个请求体（源码、CSV、schedule、metadata、账户模拟金额/佣金及标的）、派生 spec/manifest 与引擎/worker 版本。最终 POST 必须携带 token，重新执行 importer 和 spec 校验后才消费 token 并创建 run/version；修改任意输入会 409 且不生成 run。worker image 未配置时仍能看校验错误/不完整 preview，但明确标记无法运行且不给 token。
- `/execution` Python 表单改为“预览→审阅→确认运行”；确认卡锁定表单输入，显示哈希/数据摘要和未验证边界，用户点编辑后才能修改。增加 API 回归证明 preview 无 run/version、改源码令确认失效、正常确认仍保存 frozen artifacts。
- `agent/tests/test_backtrader_backtest_api.py` **9 passed、58 warnings**；相关后端子树完整回归 **325 passed、657 warnings、15 subtests passed**。`npm run test:run -- src/pages/__tests__/Execution.test.tsx` **3 passed**；`npm run build` 退出码 0（保留既有大 chunk warning）。
- **边界：** 受限 Python callback 模板、离线 Backtrader 仍非 NL2 生成或 paper deployment。真实数据/日历来源未认证，未连接 Gateway 或账户。

## B74 XNYS 日线 bar session 边界闸门（2026-10-21）

- `PaperCompletedBarFeed` 发布前使用已固定版本的 `exchange-calendars==4.13.2` 解析 XNYS session；拒绝非交易日、非纽约时区，以及 start/end 不精确等于排定开盘/收盘时间的 bar。半日市按当日缩短后的真实 close 校验。单日解析缓存减少重复生成日历表；session 表构造对周末/假日边界留出相邻日历上下文。
- `ManagedPythonPaperSession` 同时核对 frozen strategy 的 daily/regular/completed/XNYS 时区策略与每个 feed 的 instrument/calendar/version 配置，防止 bar feed 与冻结版本意图不一致。
- 增加回归覆盖周末、常规收盘后延长 bar 的拒绝，以及感恩节半日市按 13:00 ET 收盘接受、按常规 16:00 ET 收盘拒绝；更新离线和 managed session fixtures 为合法 XNYS 日界线。受影响 feed/Python session/managed session 测试 **18 passed**。
- 相关执行、Backtrader 和 PAPER API 子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**325 passed、655 warnings、15 subtests passed**。
- **边界：** 这校验的是日线时间戳与项目固定 XNYS session 的一致性，不证明供应商数据授权、OHLCV 正确性、复权策略、完整性或 point-in-time 可用性；provider 仍须可信。没有 Gateway/账户活动。

## B73 managed Python PAPER session composition（2026-10-21）

- 新增 `ManagedPythonPaperSession`，在一个可信 session 中绑定 frozen Python upload version、共享 SQLite event journal/order ledger/index、managed runtime、single-consumer callback worker、fresh reconciled snapshot、原子账户 hydration、verified instrument/conId/feed mapping、真实 Backtrader Cerebro/PortBroker、Python event bridge、授权 intent handoff、逐单风险 dispatch 和 runtime 自带的 SDK subprocess place/cancel transports。session 不接受调用方另给订单 transport 或 synthetic starting cash。
- session 实现 `PaperExecutionManager` 的 managed runtime protocol，可在可信 `PaperExecutionManager` 下使用归因 principal 触发 activate/stop；`start_paused()` 要求 runtime 尚未启动，先恢复账户并在 PAUSED + owner lease 下拒绝任何活动/未知/未恢复 native order、pending callback、staged callback 或 dispatchable outbox；随后接收 fresh snapshot、构造并 hydrate PortBroker，再启动 idle Cerebro。PAPER bars 只在 service 明确 RUNNING 时可进入 frozen feed。callback staging router 在 Broker 创建前有界收集回报；任一 pending recovery race 都拒绝 Cerebro attach。
- `stop()` 先暂停。存在排队 bar、活动/未知/未恢复订单或待确认回调时保留 session/lease 供继续处理；可安全 drain 后关闭 feeds、等待 Cerebro、停止 runtime。join 超时后可重试 stop，不会丢失 stop 状态。
- `./.venv/bin/python -m pytest -q agent/tests/execution/test_managed_python_paper_session.py --tb=short`：**5 passed、10 warnings**。BUY 纵向测试经风险/账本、`IbkrSubprocessPaperTransport` fake child、后台 durable callback worker，最终真实 `PortBroker`/Backtrader native order 完成 2 股 fill；另验证 PAUSED bar gate、错误 conId、未恢复 CREATED intent 拒绝及 router capacity/order。
- 本轮相关子树回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**323 passed、599 warnings、15 subtests passed**。
- **边界：** 整条纵向 BUY 使用 fake SDK subprocess / 本地 SQLite / 合成 snapshot、risk facts、feed 和合约映射；它不是真实 Gateway/PAPER 成交。FastAPI manager 虽可管理此 session protocol，但当前 app factory 仍只创建裸 runtime，没有冻结策略/feed/facts factory；生产 provider、账户唯一 owner/外部订单隔离、授权 UI/API、Gateway 恢复演练和真实 PAPER 成交仍未通过。无 Gateway、无账户查询、无订单发送。

## B72 managed runtime 的新鲜 reconciliation snapshot handoff（2026-10-21）

- `IbkrManagedPaperRuntime.latest_reconciled_snapshot()` 仅在当前恢复轮次完成、scope lease 仍由 runtime 持有、SDK 健康、execution gate 为 PAUSED，且完整快照包含匹配 snapshot/account/PAPER scope 与 reconciliation checks 时返回 durable snapshot。
- 新快照只在 `start_paused()` reconciliation 成功后可用于 hydration；成功 `activate()` 后即失效，即使之后 pause 也不能拿旧账户快照初始化新 Broker。重新恢复并采集新快照后才重新开放该 handoff。
- 快照 handoff 默认最多 10 秒（runtime/factory 可配置 1–300 秒）；超龄、无时区或比本机时钟超前 5 秒以上的完成时间 fail-closed。测试将完成时间回拨 20 秒确认拒绝，再恢复当前时间确认接受。
- `./.venv/bin/pytest -q agent/tests/execution/test_ibkr_managed_runtime.py`：退出码 0，**2 passed**。覆盖连接前拒绝、PAUSED 恢复后可读 scope/proof、过期拒绝、RUNNING 及后续 PAUSED 期间均拒绝旧 snapshot handoff；回调/runtime/Cerebro dispatch 组 52 passed、8 warnings。完整相关 execution/Backtrader/API 子树 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py agent/tests/test_paper_execution_routes.py --disable-warnings`：退出码 0，**318 passed、589 warnings、15 subtests passed**（此轮加入 freshness/worker ownership 改动前的完整子树回归；改后定向组另见后续记录）。
- **限制：** accessor 只提供严格的 latest snapshot；实际 Backtrader hydration 仍需可信的完整 conId→feed 映射和 managed session composition。没有连接真实 Gateway 或读取真实账户。

## B71 callback pump 单一 consumer 与 deferred-ACK 重放（2026-10-21）

- 修复 deferred-ACK inbox 被后台 worker 连续重复交付的问题。Pump 在当前进程内按 callback event ID/source key 记住已放入 Broker callback queue、仍等待 ACK 的 handoff；未 ACK 时跳过该 inbox，继续处理后续 durable/SDK callbacks，避免重复占满有限 Broker queue。ACK 先持久提交到 `callback_inbox`，再释放内存 claim。
- 每个新的 Pump 进程从空的内存 claim 集合开始，因而 SQLite 中未确认的 inbox 在进程重启后仍会按顺序重放。若 sink 拒绝队列交付，会撤销本次内存 claim并 fail-stop；持久 inbox 保留以待恢复。
- 创建 `IbkrCallbackPumpWorker` 会先为 Pump 注册唯一 background-consumer role；`BacktraderDispatchPort.process_callbacks()` 检测该注册后不再与 worker 竞争，即使组合调用方遗漏布尔选项。显式 `callback_worker_owns_pump=True` 仍可用于不经 worker 对象注入时的配置。默认模式保留离线单 consumer 行为；PythonPaperSession 仍强制离线 transport。
- `PortBroker` 对已应用且内容一致的重复 event ID 再次 ACK；内容冲突仍 fail-stop。专项回归 `./.venv/bin/pytest -q agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/bt_runtime/test_broker_proxy.py`：退出码 0，**50 passed、8 warnings**；另有 compileall 与 `git diff --check` 通过。
- **限制：** 重启测试证明 pump 从未 ACK 的 durable inbox 重播到新 sink；没有证明 Backtrader native order/ref 可在新进程恢复。若重放 callback 指向不存在的 native order，当前 Broker 会 fail-stop。实际 app factory/manager 尚未把 callback worker、Python strategy session、账户快照 hydration、真实 facts provider 和生命周期串成同一 runtime；没有 Gateway 或账户活动。

## B70 PAPER runtime 只读状态接线（2026-10-21）

- `agent/src/api/state.py` 增加惰性默认 `PaperExecutionManager(runtime_factory=None)`；manager 不做网络动作，控制 authorizer 默认 deny-all。FastAPI 新增受认证 `GET /api/execution/paper/status`，只返回 runtime/configured/phase/control gate/lease/稳定 error code，不返回 Gateway endpoint、账户 ref、余额、持仓或委托；未配置时明确返回 `NOT_CONFIGURED`。
- `/execution` 页面改为显示 API 报告的 manager 状态；状态 API 失败时 UI 表示 unknown，不再把硬编码的未配置文案冒充实时读取。该 GET 不包含连接、启动或控制动作。
- 验收：status route/API infrastructure/factory 定向组 **49 passed**；Execution 页面定向测试 **3 passed**；相关 execution/Backtrader/API 子树 `315 passed、589 warnings、15 subtests passed`；`npm run build` 退出码 0（保留 Vite 大 chunk warning）；compileall 与 `git diff --check` 通过。完整子树第一次复跑遇到既有 2 秒 bounded-pipe 子进程测试超时，单项复跑通过，随后整组复跑通过。
- **边界：** 默认 app manager 仍没有 factory，runtime status 当前应为 NOT_CONFIGURED；B69 factory 尚未被 app config 创建。未提供 start/activate/pause/stop API，未启动 SDK 子进程、连接 Gateway 或读账户。

## B69 显式 IBKR PAPER runtime factory（2026-10-21）

- 新增 `agent/src/execution/ibkr_paper_runtime_factory.py`：要求调用方显式给出 SDK Python/cwd、Gateway host/port、client ID、PAPER account、共享 SQLite 和 owner-lock 目录；recovery、activation、quiescence、callback sink、intent loader 与 contract loader 均为必需注入。拒绝已记录为 LIVE 的 Gateway 4001，也不读取环境变量或创建默认账户/profile。
- 工厂按同一 canonical SQLite 创建 SDK subprocess supervisor、账本、broker order index、order-ID allocator、durable callback pump/worker、health monitor、snapshot coordinator/store/reconciler 与 `IbkrManagedPaperRuntime`；同时提供惰性的 PAPER place/cancel transports。工厂调用不启动 SDK 子进程、不 acquire lease、不连接 Gateway。只有显式 `runtime.start_paused()` 才进入连接/恢复步骤；恢复完成后仍只到 PAUSED。
- 验收：`cd agent && ../.venv/bin/python -m pytest -q tests/execution/test_ibkr_paper_runtime_factory.py tests/execution/test_ibkr_managed_runtime.py tests/execution/test_paper_execution_manager.py --disable-warnings`：**13 passed**；`compileall -q src/execution/ibkr_paper_runtime_factory.py` 通过。
- 合并回归：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**312 passed、589 warnings、15 subtests passed**；`git diff --check` 通过。
- **边界：** factory 尚未绑定到 FastAPI 的 `PaperExecutionManager`，没有可信应用配置源或授权 principal；callback sink、risk/account facts、market-data provider 和合约映射需要调用端提供。新 factory 没有创建/接入 `PythonPaperSession` 或 Cerebro feed，且未验证 PAPER Gateway 配置。无子进程启动、Gateway 连接、账户读取或订单发送。

## B68 长驻离线 PAPER session 与市场能力契约（2026-10-21）

- `PythonPaperSession` 增加单次 `start()`、host 运行期间 `push_completed_bar()`、`finish_input()/stop()`、`join(timeout)`、`is_alive` 与 `exception`；Cerebro 在非 daemon 线程运行，异常会捕获、尽力持久 pause，bar 顺序/队列失败继续 fail-closed。transport 仍必须明确 `offline=True`，所以这是可持续推送离线 fake session 的生命周期 primitive，不是 SDK runtime 接线。
- 新增 `agent/src/execution/market_data_contracts.py`：MarketDataProvider、TradingCalendar、MarketRules、CostModel 及 snapshot/quality/calendar/rules/cost schema。history/realtime/backtest/paper/live capabilities 全默认 false；未声明能力、只声明但未实现方法都会明确抛错。quote 订阅因当前无 quote schema 而被禁用，不伪造空报价。review 时补强 schedule SHA-256 与正整数量验证。
- B68 session 专项与 market contract：`./.venv/bin/python -m pytest -q agent/tests/execution/test_python_paper_session.py agent/tests/execution/test_market_data_contracts.py --disable-warnings`：**13 passed、20 warnings**。相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**308 passed、589 warnings、15 subtests passed**。
- UI 回归/构建复核：`npm run test:run -- src/pages/__tests__/Execution.test.tsx`：**3 passed**；`npm run build`：退出码 0，仍有 Vite `__dirname` 与 bundle chunk size warning。该项无前端源码改动。
- **边界：** 当前没有任何实现 market adapter、不能因此声称真实 provider/calendar/rules/cost 已通过；Python session 仍锁定 offline fake transport，也未接 manager/FastAPI/IBKR process。无真实数据下载或 Gateway/账户操作。

## B67 同版本 Python PAPER session composition 与账户快照 hydration（2026-10-21）

- 新增 `agent/src/execution/python_paper_session.py::PythonPaperSession`，显式绑定 immutable `StrategyVersionStore` 记录、journal run version hash、PAPER/account scope、同库 ledger/index、事件 worker/handoff/dispatch/callback pump 与已持有 owner lease。真实 Backtrader `Cerebro(stdstats=False, preload=False, runonce=False, quicknotify=True)` 经 `PaperCompletedBarFeed` 接受 host-pushed completed Bar；可信 shell 在 `Strategy.next()` 中调用 `PythonPaperEventBridge`，逐事件隔离 Python worker 读取冻结源码并产出 proposal，再走 native `PortBroker`/`BacktraderDispatchPort`/durable ledger。离线 transport 必须显式标记 `offline=True`；该 session 不连接、不启动 SDK process，也不接 FastAPI。单事件仍最多一个 proposal，未结活动委托继续阻断下一事件。
- `PaperCompletedBarFeed.current_completed_bar` 提供只读的当前 host 已释放 Bar，避免组合层重新从 Backtrader lines 重建时间/完成度语义。
- `PortBroker.hydrate_reconciled_account_snapshot()` 新增一次性 pre-start 初始化：只接收 scope 匹配、采集完整且已有 durable reconciliation proof 的 PAPER snapshot；cash 只采用唯一 USD `TotalCashValue`，不把 NetLiquidation/AvailableFunds 当现金；所有 position conId 必须唯一映射到 Backtrader data，整份快照校验通过后才原子写入现金/持仓。hydration 拒绝已 start 或出现订单/回报的 broker。无首 bar 时不能得到持仓市场价值，`getvalue()` 明确失败，不用成本价冒充实时估值。原 `initial_cash` 默认没有改变。
- allocator stale-candidate 回归已确认覆盖：过期 nextValidId 清除旧 candidate，必须有新鲜候选后才能 reconciliation-confirm/allocate。
- 聚焦组合：`./.venv/bin/python -m pytest -q agent/tests/execution/test_python_paper_session.py agent/tests/execution/test_paper_event_bridge.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/bt_runtime/test_paper_completed_bar_feed.py --disable-warnings`：**21 passed、18 warnings**；snapshot hydration + broker proxy：**18 passed、8 warnings**。Host/SDK allocator 专项分别 **8 passed / 7 passed**。
- 整体相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**301 passed、581 warnings、15 subtests passed**；SDK runtime：`./.venv/bin/python -m unittest discover -s tests -v`（`ibkr-runtime/`）：**43 passed**。`git diff --check` 通过。
- **边界：** 本轮离线 session 的订单与 fill 使用 fake offline transport；snapshot hydration 测试使用合成、已证明格式的 snapshot。没有生产 app factory、Gateway profile、账户/行情/grant/risk facts provider、合约解析来源或管理 Principal；新 primitive 尚未在生产 manager 内调用。未连接 Gateway、未读取真实账户、未发送订单。

## B66 PAPER completed-bar feed 与无新 Bar 回报推进（2026-10-21）

- 新增 `agent/src/bt_runtime/feeds/paper_queue.py::PaperCompletedBarFeed`：trusted host 仅能推送严格 `Bar` 合约；拒绝未完成、尚不可用、错标的/币种/时区和非递增 session date。Feed 队列有界；队列饱和锁存错误并调用可选 pause sink。它不自己连接行情源，也不替代 provider entitlement、XNYS/session、缺口、公司行动和修订验证，调用端必须在 `push_completed_bar` 前完成这些可信校验。
- Feed 按 live 模式运行，无新 bar 时 `_load()` 有界等待后返回 `None`，让 Cerebro 的 `quicknotify=True` idle loop 继续调用 Broker 通知泵。实测真实 Backtrader Cerebro + `PortBroker` 仅输入一根 bar 后，在无第二根 bar 的 idle polls 中处理 Submitted/Accepted/Completed 回报；Backtrader 的 fill/position/cash 由原生 BrokerBase 更新。
- B66 定向 feed 回归：`./.venv/bin/pytest -q agent/tests/bt_runtime/test_paper_completed_bar_feed.py --disable-warnings`：**3 passed、6 warnings**，包括 future/partial/wrong-symbol/non-monotonic bar 拒绝、queue overflow pause sink 和 idle callback/fill。相关后端子树：**285 passed、569 warnings、15 subtests passed**。
- **边界：** feed 没有真实 provider，也没加入 app runtime；实际行情授权、日历/会话校验与数据修订处理仍缺。策略 class/frozen version→Cerebro/PAPER bridge、IBKR account snapshot→Backtrader 初始现金/持仓映射、可信风险 provider 和 manager/FastAPI factory 尚未组合。未连接 Gateway、未读取账户、未发送订单。

## B65 Managed IBKR PAPER recovery composition（2026-10-21）

- 新增 `IbkrManagedPaperRuntime` 编排对象，将 `PaperExecutionService`、SDK subprocess、durable callback pump worker、health monitor、snapshot coordinator、reconciler、同库 ledger/index/allocator/store 绑定到同一 PAPER account scope，并在构造时核对全部规范 SQLite 路径。Host、port、client ID、账户、超时和 quiescence/risk check 均由可信创建者显式传入；无默认 Gateway 参数，构造对象不会启动子进程。
- 显式 `start_paused()` 先持有 owner lease 并写 RECOVERING，再启动 child/pump，等待已验证的 `nextValidId` 与含配置账号的 `managedAccounts`，请求并等待完整快照，执行严格恢复对账和同候选 order-ID proof，最后转 durable PAUSED。仅通过显式 `activate()` 并满足 child 健康、reconciliation proof、注入的账户/风险 readiness 才允许开启 RUNNING。
- callback consumer 或 SDK health 故障会尽力 durable pause；启动/重试失败保持 RECOVERING 和 lease。Stop 先要求外部 quiescence，再由服务的 quiescence callback 在释放 lease 前关闭 child、消费持久回调；未处理的 inbox、worker 失败或 shutdown 不确定会保留 STOPPING/lease。
- B65 定向回归：managed runtime + B64 snapshot/callback/process/allocator **57 passed、15 subtests passed**；相关后端子树 **282 passed、563 warnings、15 subtests passed**。新增编排测试使用 fake subprocess 边界，验证构造零连接、显式连接请求、成功只恢复到 PAUSED、错账户时保持 RECOVERING/allocator 关闭、停止无法证明安全时不释放 lease。
- **边界：** 此对象尚未由生产 `PaperExecutionManager.runtime_factory` 或 FastAPI `lifespan` 创建，也没有管理 API、可归因 Principal 或部署配置。未实现真实账户历史导入与恢复、策略/Cerebro运行组合、实际 risk/account/quote facts 与策略级授权；没有连接 Gateway、真实账户、发送订单。不能据离线编排测试宣称 paper 就绪。

## B64 PAPER 账户快照采集与持仓/开放订单恢复对账 primitive（2026-10-21）

- SDK runtime 新增显式 `request_reconciliation` 命令；只在 client RUNNING 后发起 reqAccountSummary、reqPositions、reqAllOpenOrders。SDK callback 为账户/持仓/开放订单及三个 end marker 绑定同一 snapshot ID，account summary/positions 到达结束后停止相应订阅；错误 request ID、队列溢出或取消失败均锁存失败。
- Host 在发送请求前先将 PAPER snapshot run 写入共享 SQLite，再由 callback pump 将 primitive 回调交给 durable snapshot store。采集需同时见 account summary、positions、open orders 三个结束标记和 USD 必需余额标签；回调幂等、冲突重复、账户/请求 scope、整股数量、合约与订单字段均作严格校验。记录请求/采集完成时间；兼容迁移既有 snapshot 表。
- 新增 `IbkrRecoveryReconciler`，在统一 SQLite 上将全部 broker open orders 精确匹配到本地 PAPER durable binding/活动 intents，将账户仓位逐合约与已持久 execution fills 对比，并要求 fresh nextValidId 高于 broker 与本地 order-ID 高水位。仅在对账 proof 先持久化后才按同一候选 ID 打开 allocator；执行集不一致需要独立执行历史修复路径，当前拒绝恢复。
- B64 SDK 全套：`ibkr-runtime/.venv/bin/python -m unittest discover -s tests -v`：**43 tests passed**。Host 定向组：`../.venv/bin/pytest -q tests/execution/test_ibkr_reconciliation.py tests/execution/test_ibkr_callback_pump.py tests/execution/test_ibkr_runtime_process.py tests/execution/test_ibkr_order_ids.py`（从 `agent/`）：**55 passed、15 subtests passed**，包括 broker 存在但本地无 binding 的开放委托拒绝恢复。
- 相关后端子树复跑：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**280 passed、563 warnings、15 subtests passed**。首次整组有一项现有 bounded subprocess helper 在 2 秒内超时，单测复跑通过，随后两次整组复跑通过（最终结果详见 ACCEPTANCE B64c）。
- **边界：** B64 完成 SDK snapshot query、durable capture 和 strict ledger comparison primitive，不代表应用恢复链路已启动。`PaperExecutionManager` 仍未工厂化/注册，pump worker、health monitor、snapshot coordinator/reconciler、Backtrader bridge 和 service 尚无 app-managed composition；也未建立授权账户/行情事实、execution history repair、认证管理 API。没有连接 Gateway、读取真实账户或发送 paper/live 订单；运行仍必须保持 PAUSED、allocator 不可分配。

## B63 Host durable order-ID gate 与 stale callback 修复（2026-10-21）

- 新增 `agent/src/execution/ibkr_order_ids.py::IbkrOrderIdAllocator`，在主应用 Python 环境实现与独立 SDK runtime 相同的 `ibkr_order_id_state` schema 和 `<canonical sqlite>.order-id-owner.lock`。因此 SQLiteOrderLedger、BrokerOrderIndex/shared callback spool 与 host allocator 可共享 manager 已要求的一个规范数据库路径；`BacktraderDispatchPort` 可直接接 `allocator.allocate`，并将 `allocator.observe_next_valid_id` 绑定到 callback pump。
- Host allocator 每个本地 runtime epoch 重新要求 broker `nextValidId`；broker candidate 只有在可信 reconciliation 后才能 `confirm_reconciled()`，分配时先原子递增持久 cursor。旧 ID 现在同时清除 `ready` 和 `candidate`，确认成功会消费 candidate；防止先前有效 candidate 在后续 stale callback 后被错误复用。
- SDK runtime 的同一 stale-candidate 边界同步修复。Host tests 覆盖无候选拒绝、确认前拒绝、cursor 持久递增、restart、新鲜 ID、stale callback 使旧 candidate 失效、无效类型和本机 owner 排斥；SDK 增加对应 stale-callback 回归。
- B63 host 定向组：`./.venv/bin/pytest -q tests/execution/test_ibkr_order_ids.py tests/execution/test_ibkr_callback_pump.py tests/execution/test_backtrader_dispatch.py`（从 `agent/`）：**46 passed**。SDK order-ID 组 `./.venv/bin/python -m unittest discover -s tests -p 'test_order_ids.py' -v`（从 `ibkr-runtime/`）：**7 tests passed**。
- **边界：** 本轮修补 order-ID readiness primitive，不等于完整 open-order/native mapping/account snapshot reconciliation。还没有 app runtime 工厂把进程、账本、allocator、pump worker、health monitor、service 和 Backtrader run 绑定在一起；不得确认 allocator candidate 或激活派单。未连接 Gateway、未读取账户、未发送 paper/live 订单。

## PAPER execution manager 生命周期根切片（2026-10-21）

- 新增 `agent/src/execution/paper_execution_manager.py::PaperExecutionManager`：V1 只接受 `IBKR/PAPER`，manager 同一时刻只允许一个 scope runtime；具体 runtime 必须由可信 host 注入，manager 不猜账户、Gateway profile、合约、授权或风控数值，也不会自行连接网络。
- `start()` 要求组合完成 recovery 后持有 owner lease 并停在 durable PAUSED；失败时保留 runtime slot/lease 并提供 `retry_recovery()`，防止第二个进程内 runtime 趁对账不全启动。`activate()` 与带撤单/静默语义的 `stop()` 要求可归因人类 Principal 且通过逐 action 授权器；shared-key/loopback Principal 被拒绝。安全 pause 可不依赖提权动作，并立即要求 durable PAUSED。
- Stop 未证明 quiescence 时保留 STOPPING 和 lease；只有 service 已 STOPPED 且 lease 释放才清除 manager slot。测试用真实 `PaperExecutionService`、SQLite ledger 和 owner flock 验证 manager 状态，不以 fake 替代账本/lifecycle 原语。
- B55 定向 manager/lifecycle/lease 组：`./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_execution_manager.py agent/tests/execution/test_execution_service.py agent/tests/execution/test_owner_lease.py --disable-warnings`：**18 passed**；包含拒绝 ledger 与 callback/index/allocator 分库的 runtime composition。
- **边界：** 这是生命周期/授权 composition root，不是已部署 execution service；FastAPI 尚未注册 manager，当前 auth 没有可归因身份提供者，`runtime_factory` 未绑定 SDK client/Cerebro/facts provider。不会启动 IBKR，也不允许 paper dispatch；真正的 app wiring 需要先完成官方 SDK 专用 runtime subprocess 与账户 reconciliation。

## B56 manager 后端回归（2026-10-21）

- 命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**249 passed、563 warnings**，退出码 0。覆盖 B52–B55 code、Docker worker integration、真实 Cerebro/BackBroker 离线报告与既有受影响后端子树；不含前端构建、SDK runtime 独立 venv 测试或 Gateway。

## B58 SDK runtime 子进程与私有管理 IPC（2026-10-21）

- 新增主应用 `src/execution/ibkr_runtime_process.py::IbkrRuntimeProcess`：用明确的独立 venv Python 可执行文件启动可信 SDK 子进程，环境变量采用最小 allowlist；父子通过私有 stdin/stdout NDJSON 通道通信，协议 V1 拒绝未知字段、命令与超长/非有限 JSON，单命令串行并校验随机 request_id。请求超时、协议错帧、管道故障均视为 lifecycle uncertain 并停止接受新命令。stderr 单独 drain 且保留量有界。
- 新增 worker 子进程：构造 `EClient` 和 SQLite callback spool 不连接；只有显式 `start(host, port, client_id)` 命令才会调用 SDK connect。独立 queue drainer 防止 callback queue 无消费者而耗尽；spool 提供有序 bounded `pending()` 与单事件 `mark_processed()`，可信宿主需先提交业务账本再 ACK。策略 worker 没有该管理通道；这条 stdio 管道只由可信 app parent 创建。
- B58 SDK runtime 命令 `ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：退出码 0，**30 tests passed**；host subprocess 命令 `./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_runtime_process.py --disable-warnings`：退出码 0，**2 passed**。端到端由主应用 Python 启动 SDK 专用 venv，子进程保持 NEW/未连接并正常 shutdown；覆盖未知命令/字段、actor/账户注入、非法端口和 unknown payload，以及 callback spool 有序读取/ACK。
- Host callback pump 兼容回归 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings`：退出码 0，**35 passed**。包含原有“同一 SQLite spool→pump→ledger/native delivery”恢复用例，确认新 SDK spool schema 可继续被 host pump 消费。
- `compileall`（SDK runtime 与主应用 subprocess supervisor）退出码 0；`git diff --check` 通过。
- **限制：** 本轮未发 start 命令，未连接 Gateway；process seam 尚未接入 `PaperExecutionManager`、FastAPI 或账户恢复编排。现有 host callback pump 能消费共享 spool，但尚未由 app-managed runtime 驱动；账户/订单 reconciliation、启动配置锁定、跨机器唯一 owner、认证管理 API 和真实 paper 仍未完成。端到端只证明 SDK 子进程可在断连状态安全创建和关闭。

## B59 PAPER 订单边界与 SDK 生命周期故障保持（2026-10-21）

- V1 私有 IPC 增加严格的 `place_order`、`cancel_order` 和 `confirm_reconciled_stop` 命令。订单仅允许 PAPER、SMART/USD/STK、整股 BUY/SELL、MKT/DAY、regular-hours、transmit=true；撤单只接受精确正 broker order ID。宿主 transport 在调用子进程前核对冻结 intent，合约映射由宿主可信 loader 提供。该接缝不负责最终账户/订单 reconciliation，也不表示具备派单 readiness。
- SDK `connect()` 改由独立线程执行，父进程控制循环不被连接调用阻塞；`RUNNING` 与 SDK API 握手状态分离，`ready_for_dispatch` 保持 false，直到未来接入完整 reconciliation。run loop 异常或意外退出锁存 FAILED；host 必须完成恢复后才能显式确认停止，不能把异常路径误报为 STOPPED。关闭活动子进程会先尝试有界 stop，再 shutdown。
- 验收：SDK runtime `unittest discover` **38 passed**；主应用 supervisor **4 passed、9 subtests passed**；callback pump/dispatch 回归 **35 passed**；`git diff --check` 通过。初次 supervisor 回归发现管理方法误置于 cancel adapter，已修复后全数通过。
- **限制：** 没有发送 start、没有连接 Gateway、没有真实订单。SDK API readiness 与 `ready_for_dispatch` 均不足以替代 open-order/account/position reconciliation；本轮未接入 `PaperExecutionManager`、FastAPI 或 app callback pump 驱动，账户 facts、认证管理 API 和故障恢复仍未完成。

## B60 独立 callback spool consumer 与账户握手校验（2026-10-21）

- 新增 `IbkrCallbackPumpWorker`，在可信 host 独立非 daemon 线程持续消费共享 SQLite callback spool。它只驱动 `IbkrCallbackPump` 的 ledger/inbox 路径并把累计回报送入 thread-safe callback sink，不触碰 Cerebro 状态；pump fatal 会被 worker 锁存，可经 `failure_sink` 请求持久 pause，原始 durable callback 保持未 ACK，stop 有限 join。
- 补齐 `managedAccounts` 处理：消费前要求已配置账户精确出现在逗号分隔账户列表中；空列表或不匹配触发 fail-closed，且 durable event 不 ACK。PAPER transport 另外核对可信合约映射的 symbol/conId/SMART/STK/USD 与冻结 intent 一致，避免错误标的映射到达子进程。
- 验收：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_paper_execution_manager.py agent/tests/execution/test_execution_service.py --disable-warnings`：**52 passed**；`./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_runtime_process.py --disable-warnings`：**5 passed、9 subtests passed**。后台线程用无 Gateway 的 SQLite fixture 验证无 Cerebro bar 时处理持久事件，以及账号不匹配时暂停且不确认原 spool event。
- **限制：** consumer 尚未由 app-managed runtime 创建/停止；account_summary/positions 仍不是恢复 facts。没有启动 SDK 子进程、没有 start 命令、没有 Gateway 或账户操作。

## B61 SDK 子进程 health monitor 与 durable pause hook（2026-10-21）

- 新增 `IbkrRuntimeHealthMonitor`，可由可信 composition 定时读取 SDK child status。runtime 非 RUNNING、socket 断开、SDK API handshake 未完成、callback overflow/状态未知、run loop error 或 IPC status 失败都会一次性调用 durable pause hook；错误和 pause hook 异常留在 monitor failure。`ready_for_dispatch=false` 被明确视为当前预期值，不会被当成 health success，也不会阻断 health 本身。
- 专项用例覆盖 `ready_for_dispatch=false` 的健康连接仍可保持 PAUSED、overflow 触发 pause、status channel 超时触发 fail-closed pause。`agent/tests/execution/test_ibkr_runtime_process.py`：**7 passed、9 subtests passed**；同期 callback/dispatch/manager/service 组：**52 passed**。compileall 与 `git diff --check` 通过。
- **限制：** monitor 尚未被 app runtime 启动，故当前应用不会自动观察它；没有 broker account/open-order/position snapshot reconciliation，也不能开放 dispatch。下一步 composition 必须把 monitor pause 接到 `PaperExecutionService.pause()`，并确保回调泵在 pause 后继续消费回报。

## B62 将生命周期 recovery 拆成 lease 与对账两步（2026-10-21）

- `PaperExecutionService` 新增 `begin_recovery()` 与 `complete_recovery()`：前者先取得本机账户 owner lease 并将 durable control 状态置为 RECOVERING；trusted runtime 可以在此期间启动 SDK child 并消费 callback；只有 recovery provider 明确返回真，后者才置为 PAUSED。既有 `start()` 保持兼容，由两步组合而成。
- 增加 runtime recovery gate 测试：SDK/spool 启动阶段 lease 已持有且状态仍 RECOVERING；执行恢复检查前不得进入 PAUSED。manager 的失败保留 slot/lease 语义继续覆盖。
- 验收：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_execution_service.py agent/tests/execution/test_paper_execution_manager.py --disable-warnings`：**15 passed**；较宽 callback/dispatch/manager/service 回归为 **53 passed**。
- **限制：** 尚无实际应用 composition 去调用分阶段 API；recovery facts provider 缺 broker open-order/account/position snapshot 与结束标记，所以不会对任意配置报告恢复成功，也不会连接账户。

## B57 官方 SDK client thread lifecycle（2026-10-21）

- 新增 `ibkr-runtime/ibkr_runtime/lifecycle.py::IbapiClientOwner`：构造时仅建立官方 `EClient(EWrapper)` 对象、WAL callback spool、bounded normalized callback queue 和 overflow latch；显式 `start(host, port, client_id)` 才建立连接并启动独立 SDK `run()` 线程。模块加载和 app startup 不连接。
- `stop(timeout)` 先 disconnect，再限时 join；线程未退出时保持 STOPPING 并抛超时，断连异常保持 FAILED 并可重试，不能冒报 STOPPED。所有 SDK callback 仍先写 `SQLiteCallbackSpool` 再投 queue。
- B57 官方 runtime 组：`ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：**28 tests passed**；新用例覆盖构造零连接副作用、显式 connect/run、durable callback spool、timeout 保留、disconnect failure retry、endpoint/timeout 类型边界及官方工厂未连接。
- compileall：`ibkr-runtime/.venv/bin/python -m compileall -q ibkr-runtime/ibkr_runtime ibkr-runtime/tests` 退出码 0。没有连接 Gateway 或实际账户。
- **边界：** 这是 SDK venv 内的单进程 lifecycle primitive；尚无主应用启动的专用 SDK subprocess、版本化 management IPC、openOrder/account/position end 对账协议、callback pump consumer 或 app-level factory 接线。

## 离线 owned-order 撤单与 SDK 回报身份修复（2026-10-21）

- `SQLiteOrderLedger` 新增 `cancel_attempt` 持久记录。撤单只允许对 PAPER 下已 ACK/部分成交且已绑定本地 intent 的活动订单启动；在一个事务中核验精确 broker order ID、账户、环境、方向和数量，写入唯一 cancel ID 并将订单转为 `CANCEL_PENDING`，之后才调用 transport。
- `BacktraderDispatchPort.cancel_order` 只从 durable Backtrader-ref binding 解析 broker order ID，不接受调用方指定 ID。重复撤单在 transport 前拒绝；SDK 调用异常将撤单尝试冻结为 `UNKNOWN`，必须由回报/恢复对账解决，不自动重试。`CANCELLED` 与 `CANCEL_REJECTED` callback 分别持久确认/拒绝撤单。
- 官方 SDK transport 按锁定的 `ibapi 10.50.2` `EClient.cancelOrder(orderId, OrderCancel())` 签名构造取消请求。SDK callback adapter 的 `execDetails` 现同时规范化 `secType`，与 dispatch 持久化的完整 conId/symbol/security type/currency 身份校验一致。
- B52a 定向 execution/runtime 组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_order_ledger.py agent/tests/bt_runtime/test_broker_proxy.py --disable-warnings`：**57 passed、8 warnings**。覆盖 owned active-order 撤单、精确 ID、callback 确认、重复/不确定结果不重发。
- B52b 官方 SDK runtime：`ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：**21 tests passed**；覆盖 `OrderCancel` 对象及非法 ID 在 SDK side effect 前拒绝，以及 execDetails 合约身份字段。
- B52 完整相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：**243 passed、563 warnings**。
- **边界：** 仍是离线 fake transport/SDK client 测试；没有连接 Gateway，也没有真实 paper cancel acknowledgement、外部订单保护或受鉴权管理 API。撤单确认事实仍须从 callback/recovery 获得。

## IBKR order ID reconciliation 显式闸门（2026-10-21）

- 修正 `IbkrOrderIdAllocator` 的 readiness 语义：`nextValidId` callback 只更新候选 cursor 并保持 `ready=0`；可信应用完成 open-order 与本地持久 binding reconciliation 后必须显式调用 `confirm_reconciled()`，新连接 epoch 启动时依然会重新锁住分配。
- candidate 小于持久 cursor 时 allocator 清除 readiness 并拒绝；缺 fresh candidate、未显式 reconciliation 或乱序调用均不能分配。SQLite cursor 仍在每次返回 ID 前以事务递增。
- B53 SDK runtime 回归：`ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：**22 tests passed**，含 fresh candidate→explicit reconciliation→allocation 和缺 candidate 拒绝。
- **边界：** 目前还没有 app-level broker open-order/recovery service 调用该确认方法；因此真实 runtime 继续 fail-closed，不能仅凭 `nextValidId` 启动派单。

## T39 逐事件未来数据探针接入真实 Docker→Cerebro 路径（2026-10-21）

- 加强 `test_python_upload_runs_through_isolated_events_and_native_backbroker`：实际构建并运行固定 worker 镜像，由真实 `run_python_upload_backtest`/Cerebro 驱动上传策略；每个事件尝试读取 `bars`、`future_bars`、`feed` 字段和 `event["bars"][1]` 前向索引。若未来 bar 集合被注入，worker 会抛错并使验收失败；当前完整 runner 成功并完成 native BackBroker fill。
- B54a：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution/test_strategy_worker_docker_integration.py::test_python_upload_runs_through_isolated_events_and_native_backbroker --disable-warnings`：**1 passed、24 warnings**。
- **边界：** 证明真实 worker 进程拿到的 event 结构不含 series/feed/future collection，并通过既有隔离测试拒绝宿主文件与网络；没有逐项实测伪造游标、容器逃逸或所有 Python introspection 攻击。因此 T39 从 FAIL 更新为 PARTIAL，不能据此开放 paper/live。

## Python 上传真实 Backtrader 离线 runner（2026-10-20）

- 新增 `agent/src/bt_runtime/strategies/python_upload.py`：按 `StrategyVersionStore` 的不可变版本哈希加载源代码；校验冻结 `StrategySpec`、Backtrader/compiler 版本和 manifest；真实行情必须同时给出 importer 验证的 `Bar` 记录，OHLCV、可用时间、交易日和时区必须与 Cerebro feed 完全对齐。
- 上传代码仍只运行于 Docker `PythonSandbox`，每根 Cerebro 当前 completed bar 单独调用 `on_event(event,state)`；worker 只收到这一根 Bar 和 JSON 状态。Host 校验受限 Market/DAY 提案，显式整股数量原样提交给 native `bt.Order`，BackBroker 执行并由 host 生成订单状态、fill、费用、权益曲线、closed trades 和持仓报告。超卖、并发活动订单及越界/不完整 worker 输出 fail-closed。
- `agent/tests/bt_runtime/test_python_upload_backtest.py`：4 passed，覆盖实际 Backtrader 回测的本地确定性 fake-sandbox 测试、显式两股 next-bar-open fill、费用/报告、超卖拒绝、未来 bar 扰动不改变先前订单/成交，以及 1,001 bars 在不启动 worker 前被拒绝。
- B17 真实 Docker 集成：实际构建/运行 worker image 后通过 `run_python_upload_backtest` 跑完五根合成 bar；worker proposal 形成 BackBroker 原生订单并在下个 bar 以 `$11.00` 成交两股，佣金 `$0.022`，期末持有两股。与宿主文件/网络隔离集成共 2 passed。
- 新增 `python_upload_service.py::run_python_upload_service`：要求冻结 Python version、严格 CSV importer 返回的 `ImportedMarketData` 与原始 CSV bytes；核对 manifest/raw hash 后运行，并将 exact CSV、manifest、strategy version 和 host Backtrader report 原子写入 run artifacts。B19 artifact service test 1 passed。
- 最新受影响全组命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime -q --disable-warnings`：146 passed、284 warnings，退出码 0（B20）。
- 新增独立认证 `POST /runs/backtests/python-upload`：请求严格限定 Python 源、完成日线 CSV、逐日 session schedule 与 metadata；服务端强制生成 `explicit_quantity + long_only + Market/DAY` spec、创建 immutable version，调用 artifact service。要求 `VIBE_PYTHON_WORKER_IMAGE` 配置为固定 image ID/digest；未配置时 fail-closed 503。既有 SMA endpoint 保留原 grammar 和校验。
- B21 API 定向组 5 passed；B22 初次合并后端回归为 151 passed、292 warnings。加入 1,000-bar fail-fast 限制后的 B24 最新合并结果见下方。
- `/execution` 加入三文件上传表单（Python source、CSV、独立 session schedule JSON）及 provider/calendar/时区/复权/暖机区间/初始现金等元信息；页面展示 immutable version hash 和保存 run 链接。F3 页面组 3 passed，F4 `npm run build` 成功；有既存 Vite config 与 chunk size warnings。
- 单次 Python 回测限制最多 1,000 根 bar，因为每根会启动一次性隔离容器；超限在任何 worker 启动前拒绝，UI 显示该上限。
- 最新合并命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：152 passed、292 warnings，退出码 0（B24）。
- **边界：** Python upload 离线回测的 event/checkpoint 后由 B28 接入独立 backtest journal；paper/live `SQLiteEventJournal` 仍未连接到 Backtrader run lifecycle。每 bar 冷启动容器开销高。没有连接 Gateway 或下单，不能部署 paper/live。

## 下一步实现切片

- B160 已把 T12 从“只返回字符串错误”推进到受控的 UI 多轮缺项补充，并保留 fail-closed preview/token 边界。T12 下一步是明确持久 transcript、revision/idempotency 与修订/撤回语义，补跨轮 evidence provenance 和模型遗漏/否定表达对抗验收；真实 provider 的准确性也需单独验证。不要把本轮 UI 累积全文等同于服务端 durable conversation 或任意 NL 覆盖。
- B142/B143 已实现有限 failed-session recovery：manager 保留 runtime/lease，要求新 fresh snapshot/reconciliation，恢复支持的 native refs 与 durable callback inbox 后重建 Cerebro，并保持 PAUSED，之后须独立 activation grant。下一项应补完整故障矩阵：Docker timeout 且活动 SELL/多单、callback worker fatal、宿主/SDK 进程崩溃及 recovery 各阶段崩溃；遇到无法完整对账的状态必须保留 lease、暂停并拒绝派单。当前组合仍使用 fake IBKR transport，不能据此宣称真实 Gateway 连续性或完整 T41/T43。
- B153 将持久订单/成交/费用/恢复信息接到只读 API 与 Execution 页面，但 app 默认仍无生产 composition；下一步应实现可信 app root 对 builder、身份/grant、账户/行情/风险 provider 的显式接线，并以离线双 app 隔离与 no-Gateway 测试验收。真实 provider 授权、point-in-time/复权、broker contract mapping 与真实 PAPER 环境认证依然是外部门槛；不得由默认配置开放 PAPER 写入。
- B154 直接验证当前回测的 CSV、verified bars 和 event journal 路径在 Docker worker 内不可读。T39 仍需更广的 cursor、introspection 和容器边界矩阵；不把受限扫描写成形式化隔离保证。

## Python proposal 到订单意图的离线 handoff（2026-10-21）

- 新增 `agent/src/execution/outbox_dispatch.py::PythonOutboxIntentHandoff` 和 `RiskAwareProposalAuthorizer`：读取已提交 proposal、host run identity 和 staged bar；由注入的可信 context provider 提供 grant、账户/报价事实和 session window，服务端调用既有 `evaluate_order_risk` 并构造 `OrderIntentInput`。Host 核对 run identity、event sequence/action index、bar 时间、方向、股数、订单类型和标的，worker 不能设置账户授权字段。
- 在独立 SQLite ledger 写入前，journal 原子冻结已授权的 intent/decision；之后通过 ledger event/action dedupe key 创建 intent，再把 ledger intent ID 链回 outbox。跨库写入间崩溃时，恢复复用 journal 保存的原始 intent，避免新快照改写已授权内容。
- 风控拒绝会持久化 decision/reason 并将 proposal 标记 `REJECTED`，不会创建 intent。LIVE run 在 authorizer 前拒绝。授权时校验 run 的 grant、RiskFacts 账户/环境/版本绑定，并重验 staged event/result hash 及 outbox proposal 与 result 一致；完整性失败会暂停 run。通过时仅创建 `CREATED` intent；该 handoff 不预留资金、不调用 Broker、不发送订单。reserve/send 前仍必须刷新权威事实并执行第二次风控检查。
- 给旧 journal 增加 prepared intent、intent ID、decision 和 rejection reason 的增量列。非 RUNNING 的 run 不向消费者暴露待处理 outbox。
- B25 完整后端相关组 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：168 passed、292 warnings，退出码 0。新增用例覆盖真实纯函数风险判定、错 grant 身份和 LIVE 拒绝、worker/ledger audit field 绑定、event/result/outbox 篡改后暂停、ledger-link 崩溃恢复、两个并发 consumer 只创建一个 logical intent，以及决策级幂等键隔离不同 run。
- **边界：** 仅验证到 CREATED order intent handoff。grant/revocation store、权威账户/报价 provider、资金 reservation、派单前二次风险、PortBroker composition、IBKR send、券商 reconciliation 仍未接入；不可开放 paper/live。

## Python upload 报告第二套 BackBroker 复算（2026-10-21）

- 新增 `agent/src/bt_runtime/report_replay.py`：runner 从可信 host 捕获有序决策日志，然后在同一 host 进程内启动第二个真实 Cerebro/BackBroker，使用已验证 bars 和 host 决策重放；不执行上传策略代码。复算前核对 feed 与逐根 `Bar` 的时间、标的、OHLCV 和完成状态，decision seq 必须严格递增。
- 复算比较订单生命周期、逐笔成交/价格/费用、逐 bar cash/equity、closed trades、期末持仓与 metrics；任何字段不一致即抛出 `ReportReplayMismatch`，service 因而不会写出成功报告。报告附决策日志 hash 与 `execution_integrity.status=verified`。
- 报告核验将 initial cash、commission/slippage 声明、manifest 内容与 hash、冻结 strategy version、instrument、engine 和决策日志绑定到实际 replay 输入；对声明篡改、bars 错配、乱序决策及报告结果改写 fail-closed。
- B27 相关后端组命令 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：170 passed、474 warnings，退出码 0。
- **边界：** 这是同进程中的第二套 Backtrader/BackBroker 复算，仍可能有共同算法错误；不等于独立进程 artifact 签名、防篡改存储、券商对账或 paper 证据。T44 维持 PARTIAL。

## Python upload 离线事件恢复（2026-10-21）

- 新增 `agent/src/bt_runtime/backtest_event_journal.py::SQLiteBacktestEventJournal`，每个 run 独立保存策略版本/spec hash、输入与成本配置 hash、逐 bar 事件、worker result 和 JSON checkpoint；SQLite 使用 WAL 和 `synchronous=FULL`。该 schema 没有 account/deployment/environment 字段，也没有 `order_outbox` 或 dispatch 方法，与 paper/live execution journal 分离。
- `PythonBacktestEventCoordinator` 接入 `run_python_upload_backtest`：新事件先持久化并释放给 worker，worker result 与 checkpoint 原子提交。重新运行时从首 bar 重建全新的真实 Cerebro/BackBroker；已提交事件复用 journal result，未提交事件仅对同一 event id 和 byte-identical Bar 重试。变更版本、数据、资金/费用配置或 bar 会拒绝并 pause。
- `run_python_upload_service` 在 worker 启动前原子保存 CSV、manifest、冻结 version、verified bars、resume inputs 和 run config；输入文件初次保存后不可被恢复操作改写。相同 run 同时只能有一个本机进程持有恢复锁。认证 `POST /runs/{run_id}/resume` 从这些持久化输入重导入行情，完成 full Cerebro replay 和报告校验后才标记成功。
- B28 包含 `test_committed_result_is_reused_after_reopen_without_an_account_or_dispatch_surface`、`test_uncommitted_event_retries_same_bar_and_rejects_replacement`、`test_run_config_or_version_mismatch_pauses_existing_backtest` 和 `test_python_upload_api_resumes_from_committed_event_results`。API 故障用例在 event result/checkpoint commit 后、native BackBroker order submit 前注入 host crash；resume 重建同一订单/成交/持仓/权益报告，并与不中断运行逐项比较。首次已提交事件 worker 不重跑，order/fill 只出现一次；另覆盖未提交 worker failure 的相同 Bar 重试和输入/config 篡改暂停。
- B28 当前相关后端组 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：174 passed、516 warnings，退出码 0。同步改动后的 journal/service/API 定向组另为 10 passed、82 warnings。
- **边界：** 这是本机同步 API 的离线恢复；调用者需要显式 resume。它通过全量重放重建 BackBroker，不恢复 paper/live broker、外部订单、reservation 或 IB 回报。worker 暂用测试注入覆盖崩溃窗口；B17 独立证明实际 Docker worker→BackBroker 的离线运行。P4 整体仍未通过。

## Execution 报告展示（2026-10-21）

- `/execution` 最近完成的入口现在以返回报告为准显示 engine、strategy version、execution mode、bar 数与数据来源；未运行时仍显示明确标成样例的默认合成证据。开始其中一个入口时清除另一入口的旧结果，避免顶部显示陈旧 run。
- 修复 SMA 收益摘要使用 `metrics.total_return_fraction`（兼容旧 `total_return`）；Python 回测也显示期末价值、收益、费用和 `execution_integrity` 独立复算状态。
- F5 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：3 passed，退出码 0；F6 `npm run build`：退出码 0。Vite 保留已知大 chunk/config-loader 警告。
- **边界：** AI NL2 缺项澄清和显式规则确认流仍未实现；当前 SMA 仍是受限文本语法解析，T12 PARTIAL。

## 可信 IBKR 回报泵切片（2026-10-20）

- 新增 `agent/src/execution/ibkr_callback_pump.py`，将官方 SDK adapter 的不可变回调值消费到可信执行侧：持久 broker order ID → intent/BT order ref 绑定；绑定前核对已发送的 durable intent 的 broker/account/environment/方向/数量；校验 configured account/environment、执行方向、整股数量、execId、价格和 IB 时间；先持久化 callback inbox，再向 SQLiteOrderLedger 提交事件，然后将账本累计成交交给 `PortBroker`。exact transport replay 按规范化 envelope 去重；同一 `execId` 的新 envelope 交由 ledger 检查 fill facts 一致性。未知订单/状态、错账户、坏数值、SDK error/commission 未支持、账本冲突、sink 失败和 SDK 队列溢出都会锁存 pause。
- SDK adapter 必须提供 `durable_sink`；官方 runtime 提供 `SQLiteCallbackSpool`，以 WAL 和 `synchronous=FULL` 先将原始规范化 callback 持久写入共享 SQLite spool，再尝试内存 queue。pump 从同一数据库消费 spool，将 callback 写入 inbox 后记账并通知 Backtrader；进程在 spool 提交后、queue 入队前退出时，重启可重放。queue sink 接收后 `delivered=1` 仍只代表 `PortBroker.on_callback` 入队，不证明 BT 线程处理成功；其后崩溃的安全恢复仍依赖 consumer ack + native order 重建。
- 回报泵 + Backtrader runtime 定向组：`./.venv/bin/python -m pytest agent/tests/execution/test_ibkr_callback_pump.py agent/tests/bt_runtime -q --disable-warnings`：本轮 37 passed，182 Backtrader 内部弃用警告。覆盖授权 intent binding、账户/映射/数量/方向拒绝、execId 重复及冲突复用、延迟 ACK 保留撤单状态、Filled 无 execution facts fail-stop、callback durable spool 后 queue 崩溃重放、部分成交/取消/晚到 fill、queue overflow、ledger commit 后 sink fail/restart replay，以及真实 Cerebro proxy no-bar/native Order 路径和 fill-first 生命周期。
- 本轮完整受影响 execution + Backtrader runtime 子树见下方逐事件 worker 章节 B15：141 passed、208 warnings。此前 sandbox bounded-pipe 2 秒超时曾单独复跑通过，后续完整子树复跑通过；不放宽断言。官方 SDK runtime `ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：8 passed。源码 `compileall` 与本轮 touched-file whitespace 检查通过。
- IB 的 `orderStatus` 不带发生时间，pump 为此类状态使用受信任服务的接收时钟；`execDetails.time` 按配置的市场时区解析。需要审慎解释两类事件的时间精度。
- 最后一轮只读复核发现 IB `Filled` 状态本身没有 `execId` 和逐笔价格事实，不能单独把账本置为已成交。当前只在 ledger 的 execution 明细已完整达到委托数量时接受该状态作为终态重复标记；否则 fail-stop 并要求 reconciliation。此保护是在上列回归命令之后加入，尚未重跑测试。
- 该切片把 P3 SDK 队列→账本→native broker 从“互不相连原语”推进到“离线消费/回报集成”。没有 Gateway 连接。error 分类、commission/correction 账本、原子同库映射与 intent 绑定、账户级 reservation/owner、完整 consumer ack、断线/重启 reconciliation、可信风险/合同校验与 paper 券商回报仍缺；不能宣称 P3a 或 P4 完成。

## 可信逐事件 Python worker 切片（2026-10-20）

- 新增 `agent/src/execution/event_worker.py`，作为 SQLite journal 与 sandbox 的可信协调器。它只从冻结的 Python upload version store 读取源码；run 中的 version hash 来自 journal 权威记录。每次向隔离 worker 发送单个 schema-validated 完成 bar 和当前 JSON checkpoint，不发送账户/环境/deployment、DataFrame、路径、下一事件或可写 cursor。worker 结果只允许 event_id/state/order_proposals；journal 校验当前 event_id 并在一个事务内提交 state、游标与订单 outbox。
- 未提交事件崩溃恢复时，coordinator 优先重放 journal 中同一个 staged/released bar；即使调用方同时给了新 bar，也不会替换未完成事件。下一 bar 必须等本事件结果提交后才能 stage。未完成 bar 和模板版本进入 Python worker 均在发送前拒绝。
- 新增 `agent/src/execution/strategy_worker_image/{Dockerfile,worker.py}`，使用 digest 固定的 Python 3.12 slim 镜像，在 worker 容器内执行 `on_event(event, state)`；上传代码只能返回 JSON state 与受限 Market/DAY 整股提案。新单测 `test_event_worker.py` 与 event journal 共 13 passed。实际容器 `test_strategy_worker_docker_integration.py` 1 passed，断言上传代码无法读临时宿主文件或连接宿主 loopback；源码成功看到该次事件当前收盘价。
- 真实容器测试首次因 pytest 的 HOME 沙箱遮蔽 Docker context 元数据失败；测试将 Docker CLI 配置明确设到用户既有 `.docker` context 后复跑通过。生产代码未更改 Docker context 或主机设置。
- 完整受影响子树 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime -q --disable-warnings`：退出码 0，141 passed、208 warnings。基线 46 项也本轮复跑通过，148 warnings；逐事件新测试组 13 passed。
- **边界：** 该 coordinator 还没有接进 `Cerebro`/`run_backtrader_backend`、API 或策略 UI，因此没有将 durable event/checkpoint 恢复接入 Python 上传回测/运行 lifecycle；每个事件目前是一次性容器调用，正确性优先但开销高。新增 outbox handoff 可将获准提案写成 ledger intent，但尚无组合后的 execution_service 或 IBKR send。T39/T37/T40/T42 均仅部分证据；Python 不开放 paper/live。

## 用户回测入口增量（2026-10-20）

- `/execution` 从静态只读样例升级为受限创建流：选择代码/自定义 instrument ID、yfinance 或 Stooq、日期区间、allocation，并用中英文完整句式描述双均线策略；调用后端真实 provider-backed `run_backtrader_backend`，展示保存的执行报告摘要、原始报告和 run detail 链接。页面明确告知行情来源声明、历史时点可用性、日历、复权/公司行动与代码映射未经核验。没有加入 Python 上传或 paper 操作。
- 新增认证 `POST /runs/backtests`：仅支持一个股票代码、十年内区间和白名单 `sma_cross@1.0`；严格解析快慢均线周期和上穿买入/下穿卖出句式，附加条件或不支持语句会拒绝；symbol/instrument 输入均有约束。后端先持久化请求配置/运行状态，再在线程池执行并写入现有 `artifacts/execution_report.json`、manifest、冻结合成行情快照和 immutable strategy version。测试 runner 是 fake，没有发起外部行情请求。
- provider 路径现在同时保存用于本次 Cerebro 的规范化 OHLCV 快照 `artifacts/market_data.csv`；其原始字节 SHA-256 与 `DataManifest.file_sha256` 一致。这个快照让结果可复核/重复分析，但未认证 provider 声明、授权、历史可得性或复权口径。
- B9 `./.venv/bin/python -m pytest agent/tests/test_backtrader_backtest_api.py -q`：2 passed；F1 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：2 passed；F2 前端生产构建退出码 0（已有 Vite config/chunk warnings）。完整后端套件未跑。
- B6 + B9 合并回归：`./.venv/bin/python -m pytest agent/tests/execution/test_execution_backend_routing.py agent/tests/test_backtrader_backtest_api.py -q`：9 passed，26 Backtrader 内部弃用警告。
- 这交付了**受限 SMA 语言句式的用户发起回测路径**，还不是 AI NL2 缺项澄清/规则审阅流程；也未验证 yfinance/Stooq 的实时在线抓取、来源/授权、市场日历或真实报告准确性。

## 续做增量（2026-10-20 晚间）

- 重核基线：HEAD 仍为 `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` / `main`；原有 dirty 工作和本项目新增文件均保留。当前依赖读数和原始差异见 [BASELINE.md](BASELINE.md)。
- 原交接命令复跑：46 passed，退出码 0；见 [ACCEPTANCE.md](ACCEPTANCE.md) B1。
- 新增 immutable version store：`agent/src/execution/versions.py`，模板/上传分入口，版本哈希绑定完整 spec/source hash/Backtrader/compiler 版本，上传源码受字节数限制、内容寻址、读取时校验 hash，冲突拒绝覆盖。
- 新增 SQLite event journal/outbox 原语：`agent/src/execution/event_journal.py`。authority 与 cursor 留在可信 SQLite run 记录中；worker 返回模型拒绝额外账户/环境/版本/actor 字段；一个事件在结果/状态/outbox 同事务提交前不能发下一事件；相同重交幂等、冲突重交暂停 run。
- 新增纯函数 `agent/src/execution/risk.py`，将明确 grant 与 bound order/facts 比对：账户/环境/版本/标的、撤销/时效、行情/账户新鲜度、完整快照、数量/单笔/策略预算/总敞口/现金/预留/日亏损/每时段订单数，并生成输入 hash 风险快照 ID。已增加 durable order ledger 和 Cerebro BrokerBase 代理原语，但授权事实和派单端到端集成尚未完成。
- 命令 `./.venv/bin/python -m pytest agent/tests/execution/test_event_journal.py agent/tests/execution/test_strategy_versions.py -q --disable-warnings`：21 passed，退出码 0。事件通道仍未接真实跨进程 IPC；这两个模块是持久化/协议原语，不代表 P3/P4 全链路。
- 新增 [ACCEPTANCE.md](ACCEPTANCE.md)，按 DELIVERY_PLAN T01–T44 列状态、测试名、探针证据和限制。T39 现为 PARTIAL（B54a 仅证明真实逐事件 worker 输入结构，不覆盖所有 cursor/逃逸攻击）；T31/T37 为 BLOCKED；未运行项不作推断。
- 本机创建专用 Colima `vibe-execution` profile（2 CPU / 4 GiB / 20 GiB / 禁用宿主目录 mount），Docker 29.5.2 Ubuntu 24.04 ARM64 可用；Docker CLI 默认 context 已恢复 `default`。拉取并记录 `python:3.12-slim` digest：`sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`。sandbox worker/负向试验尚未完成，隔离仍为未通过。
- 只读接入审计确认现有 Backtest runner 固定要求 `signal_engine.py` 并只看 `engine=daily/options`；要加独立 `execution_backend` 分支，不改变旧 engine 语义，也不能把 Backtrader 策略伪造成旧 signal engine。前端需独立 `/execution` 页；已有 `/runtime` 是只读控制台。

## 续做增量（2026-10-20 夜间）

- 定向回归最新扩展到 B3：执行契约、数量规则、风险、事件 journal、策略版本、订单账本、合成 SMA 与导入行情测试共 **93 passed**，退出码 0；精确命令与边界见 [ACCEPTANCE.md](ACCEPTANCE.md)。
- 模板源码与 Python 上传源码现在都按 SHA-256 内容寻址保存，版本记录指向不可变 blob，加载时重新校验字节 hash。文件：`agent/src/execution/versions.py`；新增模板 blob tamper 测试。
- 新增 `agent/src/bt_runtime/market_data.py`：严格 CSV 字段、大小/行数上限、Decimal OHLCV、完成时刻、显式版本化 session schedule、精确交易日覆盖与原始字节 SHA-256。行情索引采用 `available_at`，DataManifest 标明日历、公司行动/复权声明和 `caller_declared_unverified`。本轮只用本地测试夹具，**没有真实供应商数据**。
- `run_verified_backtest` 将受信任 `sma_cross` 编译进真实 Backtrader Cerebro + BackBroker，并输出与输入 DataManifest 绑定的报告：逐 bar equity curve、DrawDown/Returns、closed-trade 摘要、订单/fills/fees/cash/期末持仓。报告明确标记 `provider_import` 与 `caller_declared_unverified`；真实数据来源/授权、权威交易日历、完整规则/费用模型仍未验收。内部非确定性 Backtrader trade refs 不写入 report，以保持重复运行业务结果稳定。
- 上传 Python 沙箱的真实 Docker probe 通过：`test_python_sandbox_integration.py` 1 passed；加入订单账本后 execution 子树复跑为 99 passed。已验证网络/文件访问被拒、非 root、资源及容器配置和正常结束清理；T31/T37 升为 PARTIAL。此 runner 还没有逐事件策略 IPC/账户风险授权集成，不能进入 paper。
- 已新增独立 `/execution` 页、路由/侧栏接入和两项页面测试；页面显示清晰标记的离线样例与状态边界。执行页测试 2 passed，`npm run build` 退出码 0（有 Vite config/chunk warnings）。
- 后端 `execution_backend=backtrader` 已从旧 `engine=daily/options` 独立分流，在旧 `signal_engine.py` 校验前进入可信 SMA 路径；报告和版本写入 run artifacts，GET run detail 可读。该 runner 初次接入时直接从原 provider DataFrame 建 manifest；B30 后已改为使用 XNYS 日历与严格 importer，历史 provider/PIT/contract mapping/source attestation 限制仍适用。

## 当前阶段

**P0 离线探针已完成；P1 受信任 SMA 与 Python-upload Docker→Cerebro/BackBroker 合成离线回测、artifact service、独立认证 API 和上传 UI 已实现；P3a/P4 已覆盖完整的离线 event→intent→native order→dispatch→callback→fill 组合；paper 写入仍受阻。** Python 源不会进入 Backtrader feed 进程；每根已完成 bar 通过版本化单次 stdio worker 请求，响应关联 ID 和字段严格校验；容器有内置硬 deadline，已用真实 Docker 验证宿主 supervisor 被强杀后的 worker 终止和自动清理。`PaperExecutionService` 生命周期原语持有单机 owner lease，启动恢复完成后仍 PAUSED，显式 readiness 检查通过才 RUNNING，quiescence 未确认时保持 STOPPING 并持有租约。`PythonPaperEventBridge` 在 worker 前匹配 durable/current completed Bar、拦截旧 outbox 和 scope ledger 未结工作；一事件最多 handoff 一个 intent，再由 one-shot resolver 绑定到 native order。B67 将冻结版本、隔离 worker、completed-bar feed、真实 Cerebro/PortBroker、dispatch 与 callback 离线组合，并新增 PAPER snapshot→broker 初值 hydration；相关后端子树 301 passed、581 warnings、15 subtests，SDK runtime 43 passed。**这些仍是显式构造的离线 primitive，尚未接入 app-managed runtime。** IBKR client/callback 生命周期在真实 app 的 factory/manager 内、授权账户/报价 facts、管理 API、生产 provider 和完整重启 reconciliation 仍未实现，paper 写入仍受阻；本轮未连接 Gateway 或读取账户。

## 本轮完成

- 重读上级 `/Users/yaozekun/strategy_verification/AGENTS.md`、本目录架构/契约/交付/复审文档。
- 记录 Vibe 与 Backtrader HEAD、分支、既有 dirty 工作区及 Python 包元数据：见 [BASELINE.md](BASELINE.md)。
- 新增 Pydantic 严格契约：StrategySpec、BarPolicy、PositionPolicy、ExecutionPolicy、Instrument、OrderIntent、EventEnvelope、Bar、DataManifest；加入 run、local submission、broker order 状态枚举、规范 JSON 与 SHA-256（包括 date/datetime/Decimal）：`agent/src/execution/contracts/`。
- 新增核心契约测试：`agent/tests/execution/test_contracts.py`。
- 契约测试：`./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py -q`，16 passed。
- 纯数量规则：`agent/src/execution/position_sizing.py`，包含 Decimal 目标比例 sizing、现金缩量记录、卖出可用量保护、显式数量全量拒绝/接受校验；focused suite 23 passed。
- 受信任 `sma_cross` 合成回测：`agent/src/bt_runtime/strategies/sma.py`，通过冻结 spec 编译器后走真实 Cerebro + BackBroker；提供订单/成交/费用/现金/剩余持仓报告，并调用同一 sizing helper。报告附规范化输入 CSV SHA-256 和合成 `DataManifest`，有明确 warmup/evaluation 边界。
- 合并 focused 命令 `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q`：退出码 0，46 passed；Backtrader 报 148 条内部 `datetime.utcnow()` 弃用警告。
- `uv lock --check`、`git diff --check` 与新增运行时代码/定向测试的 `compileall` 均退出码 0。
- 为 Backtrader 加入可选固定版本 extra `backtrader-runtime`，更新与现有 pyproject 匹配的 `uv.lock` 并在 Vibe `.venv` 安装；`uv lock` 和 `uv lock --check` 均成功。
- 官方 `ibapi` 离线来源已固定：`docs/backtrader-system/ibapi-source.lock.json` 锁定 1050.02 下 `setup.py + ibapi/**` 的 237 个文件和树 SHA-256；`ibkr-runtime/` 独立 lock 固定 protobuf 5.29.5 与安装工具，避免主项目 channels extra 冲突。`scripts/install_ibapi_from_official_source.py` 的源码验证与独立 Python 3.12.13 安装成功，最终导入版本 10.50.2。`IbapiCallbackAdapter` 把官方 SDK 回调规范化到有界队列并在溢出时 fail-closed；`IbapiOrderSubmitter` 在再次风控通过且 durable `SEND_STARTED` 返回后才调用注入的 `placeOrder`，SDK 异常会持久标成 UNKNOWN。5 SDK runtime 单测通过；没有 `connect` 调用或网络测试。树 hash 不等于下载包 archive hash。
- 新增 `SQLiteOrderLedger`：intent/action 幂等、资金预留、持久发送边界、UNKNOWN 冻结重发、broker event/exec ID 去重和重启恢复视图；部分成交、撤单竞态与晚到 fill 有离线用例。新增 `PortBroker`：真实 Cerebro 原生订单映射、空 bar 回调、部分成交/撤单、重复/冲突回报、发送或撤单未知时 fail-stop。它目前由 fake trusted port 驱动，不连接账本或官方 SDK。
- 最新定向回归：核心组 B3 **93 passed**；含 Docker 隔离与 API 分流的 execution 子树 B5 **99 passed**；Backtrader runtime B8 **18 passed**；独立官方 SDK/fake transport B7 **5 passed**。具体命令见 [ACCEPTANCE.md](ACCEPTANCE.md)。完整后端套件未运行。
- 当前决定与未冻结点：见 [DECISIONS.md](DECISIONS.md)。
- BrokerBase 空 bar 回报泵：`experiments/broker_probe/README.md`，真实 Cerebro 探针退出码 0；需 `quicknotify=True` 才能在无新 bar 时 prompt notify_order。
- SQLite 事件结果/状态/意图提交：`experiments/transaction_probe/README.md`，提交前/后崩溃与重复/冲突重交探针退出码 0。
- macOS 上传策略隔离：`experiments/sandbox_probe/RESULTS.md`；Seatbelt 子进程试验能拦截网络/文件，但当前 runner 未启用、`RLIMIT_AS` 失败被吞掉；Python paper 部署保持阻断。
- 逐事件数据/报告复核：`experiments/data_replay_probe/README.md`，独立真实 BackBroker 重放通过；PandasData 和试验 Feed 都可由策略反射读取完整未来输入，所以隔离验证未通过。
- 有状态策略 restart/restore + 指标重建对比：`experiments/state_recovery_probe/README.md`，20 个续跑事件逐项一致；账户现金/持仓和跨进程持久化未覆盖。

## P3a 续做（2026-10-21）

- 新增 `agent/src/execution/backtrader_dispatch.py`，把 Backtrader native order 与 host resolver 选出的冻结 intent 逐字段核对（PAPER、IBKR、标的、方向、整股数量、Market/DAY），再调用既有双阶段风险/账本 dispatch。风险拒绝在发送前返回；发送状态不明时沿用 UNKNOWN/reconcile 边界。
- 账本最终风险事务现在同时提交 `SEND_STARTED`、submission attempt 和 broker order ID/account/environment/intent ID/Backtrader order ref/数量/方向映射；唯一性或字段绑定失败会回滚整笔事务。sender 入口再由 `IbkrCallbackPump.bind_order` 核验持久映射后调用 transport，所以同步回调也能找到映射。
- `PortBroker.next()` 现在在 Cerebro 线程先运行 durable callback pump，再应用累计状态；组合模式将 inbox ACK 后移到 Backtrader native order 更新成功之后。队列入队后若进程中断，未 ACK inbox 可重放。单次 Cerebro 进程内的部分成交、取消/晚到回报原有保护仍保留。
- B34 定向验证：`uv run --project agent pytest -q agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_dispatch_service.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_order_ledger.py agent/tests/bt_runtime/test_broker_proxy.py --disable-warnings`：退出码 0，**60 passed、8 warnings**；覆盖真实 Cerebro native BUY → 两阶段风险/账本原子提交 → fake transport → durable mapping → execution callback → native Completed/ledger FILLED/cash/position，以及错误数量和 broker ID 冲突时零发送。
- 首次用 `uv run --project agent` 执行整个相关后端子树时，204 项通过、1 项因项目导入根目录不含仓库根而失败；按仓库既有 B33 入口从 workspace root 复跑后 B35 **205 passed、563 warnings**，退出码 0。随后加入 binding conflict 回滚与边界处理后，B36 同一组相关后端 **206 passed、563 warnings**，退出码 0；B35/B36 命令见 [ACCEPTANCE.md](ACCEPTANCE.md)。
- **边界：** 这是单进程离线组合测试，transport/order ID allocator/事实 provider 仍为注入依赖；尚未构造官方 SDK Contract/Order、接 IBKR Gateway、处理跨进程 owner lease、在重启后重建 Cerebro/native order 或取消命令。没有 paper/live 写入。

## P0 待完成

- [x] 五项实验均形成可复现证据：隔离（当前 macOS runner 阻断）、BrokerBase、事务/重交、状态恢复、逐事件数据/报告（Python 输入隔离阻断）。
- [ ] 冻结并实现市场日历/provider/rules/cost 接口；数量规则已有离线纯函数与合成手算回测证据，费用及日历仍需更多验证。
- [x] 建立可重复安装的 Backtrader 依赖声明与独立官方 IB API 来源树安装流程；Broker adapter 和 paper 验收仍未完成。
- [x] 完成初版 P0 contract tests 与实验复核；当前允许进入受信任模板离线 P1，不允许宣称 Python 上传可部署或进入 paper。
- [x] P1 受信任 SMA 合成 Backtrader 切片、数量模式纯规则与定向测试已实现；合成数据 manifest/hash 已验证。真实市场数据源与复权/日历契约、全面报告导出、API/UI、上传策略隔离和 paper 集成仍未完成。
- [ ] paper 环境仍需用户配置与明确写入授权；当前没有访问或下单。

## 单笔在线 intent dispatch 状态机（2026-10-21）

- 新增 `agent/src/execution/dispatch_service.py::PaperOrderDispatchService`：只接受 PAPER BUY intent；SELL 在股数 reservation 未实现前 fail-closed；从 ledger 重读冻结 intent；发送前两次调用 host 注入的 grant/账户/报价/deployment owner facts provider 并执行纯风险规则；第一次通过后按估算费用 reservation，第二次拒绝或读取异常时，仅在尚无 send attempt 的情况下释放 hold。
- 只有 `SQLiteOrderLedger.start_send` durable commit 返回后才调用注入的 sender；start-send 异常时 sender 不可达。sender 异常将 ledger 标为 UNKNOWN；后续 dispatch 只返回 RECONCILE。该服务不创建 EClient、不连接 Gateway，sender 由 fake transport 测试。
- `SQLiteOrderLedger.release_before_send` 增加 RESERVED→CREATED 原子释放，仅限 submission_attempt 不存在且 reservation ID 相符。`RiskFacts` 现在拒绝负价格缓冲和负手续费，避免风险估值被人为调低。
- B29 完整相关组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，**192 passed、516 warnings**。新增用例覆盖二次检查 revoke/stale quote/cash decline 的 fake sender=0、risk provider 失败释放 hold、事务化 reservation 汇总拒绝超预算买单、intent 在事务内过期 sender=0、direct ledger SELL 拒绝、start-send 失败 sender=0、发送不确定后绝不重试、同一 intent 双线程 dispatch 只有一个 sender、不同超预算 intent 并发最多一次 sender、LIVE/deployment/account identity mismatch fail-closed 及负成本输入。最终事务会独立校验 grant、facts 与持久 intent 的 deployment/broker/account/environment/version/instrument 绑定。
- **限制：** facts provider 未接真实 grant/account/market stores；reservation 原子性仅在共享的本地 SQLite ledger 内成立，跨数据库和唯一 active deployment 未实现；Backtrader order_ref 未可信绑定到 intent；未组合官方 IBKR client、回报泵或恢复 reconciliation。B29 是 offline composition 证据，不可用于真实下单。

## XNYS 日历与 SMA provider 严格导入（2026-10-21）

- 新增 `agent/src/bt_runtime/calendars.py`：固定 `exchange-calendars==4.13.2` 生成 XNYS regular session schedule，输出开收盘、半日市标记及稳定 schedule SHA-256；运行时检查已安装版本与 manifest 版本一致。
- `run_backtrader_backend` 不再直接用 provider DataFrame 构造未验证日历 manifest。它按日线 session-date 解释 provider 索引（保留 UTC 午夜日期标签），生成严格 completed-bar CSV，要求 provider bars 与覆盖区间内 XNYS session 完全匹配，再调用 `import_completed_daily_csv`；CSV 以 XNYS 收盘作为模型可用时点，明确 provider 的实际发布时间仍未验证。
- importer 从实际 session schedule 重算 hash 并与声明 hash 比对，schedule 与 bar 的 session date、开收盘、available_at 和 OHLCV 不匹配时拒绝；manifest 分开记录 package version/hash 与未认证 provider/复权来源。
- B30 命令：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/bt_runtime/test_market_data.py agent/tests/execution/test_execution_backend_routing.py agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**58 passed、228 warnings**。覆盖 DST 切换、Juneteenth、感恩节半日市、同日范围、hash 一致与篡改、缺 session fail-closed、UTC 日线标签，以及真实 Cerebro/BackBroker runner。
- 当前 venv 已安装 `exchange-calendars 4.13.2`；`pyproject.toml` 的 `backtrader-runtime` extra 与 `uv.lock` 固定该版本及其三项依赖。没有安装/导入 IBKR SDK，也没有连接 Gateway。
- **限制：** 该库只作为确定性的离线 session 实现，不是交易所官方授权日历；没有真实冻结行情、provider 授权/点时可得性/复权证明、迟到或修订数据处理，也没有验证在线 dispatch 的 next-session expiry。T07 仍为 PARTIAL。

- B31（B30 后）相关组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，**199 passed、563 warnings**。包含请求区间缺失 session、午夜 session-date 标签与非 XNYS timezone 在 fetch 前拒绝。这是 execution、Backtrader runtime 与 backtest API 子树，不是全仓后端或前端构建。

## 未做

没有运行全仓后端子树；相关 execution + Backtrader runtime + backtest API 子树由 B31 验收。没有启动应用，没有连接任何 IBKR Gateway，没有读取账户、发送 paper/live 订单或更改 Gateway Read-Only。



## SMA 策略预览与显式确认（2026-10-21）

- 新增认证 `POST /runs/backtests/preview`：对完整请求规范化 `StrategySpec`，返回可支持状态、规则摘要、数据边界警示及完整策略输入 hash。预览不创建 run 或 strategy version；unsupported 描述会返回明确的澄清提示。
- 服务端把完整输入、规范 spec、运行预算/费用默认值、Backtrader 版本、实际 SMA 模板源码 SHA-256 和 compiler version 绑定后签发随机一次性 token，保存于 runs 目录下的隐藏 SQLite 授权表，十分钟过期。runner 校验模板身份，并用同一冻结源码字节创建不可变版本。创建回测必须消费未使用且与预览匹配的 token；没有预览、修改输入/模板、过期或重放均在 provider fetch 和 run 创建前拒绝。该本地授权表只记预览确认，不是运行/订单账本。
- `/execution` 先显示从规范 spec 驱动的规则卡，再让用户显式确认。卡片展示周期/交叉、标的和区间、资金比例、long-only/整股/等待交叉入场、completed-bar→next-session、Market/DAY、RTH/gap/expiry，以及固定预算、费用预留、缓冲、佣金和现金。
- B32 API tests：`./.venv/bin/python -m pytest agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，**9 passed、58 warnings**。覆盖预览无 run/version、unsupported 澄清、token 缺失/输入更改/重放拒绝和确认后保存。
- F7 前端用例 `npm run test:run -- src/pages/__tests__/Execution.test.tsx`：退出码 0，**3 passed**；F8 `npm run build` 退出码 0，Vite 报既有大 chunk warning。
- B33 完整相关组：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py -q --disable-warnings`：退出码 0，**202 passed、563 warnings**。
- **限制：** 只解析完整双 SMA 固定语句；目前不是 AI 多轮澄清或自由表达转换。页面与 API 仍只执行受限离线回测。未做全仓后端套件；没有真实行情请求、Gateway 连接、账户读取、paper/live 发送或 Read-Only 修改。


## 官方 IBKR SDK 命令构造与 durable order ID（2026-10-21）

- 新增 `ibkr-runtime/ibkr_runtime/order_factory.py`：仅接受 PAPER IBKR、配置账户一致、整数股数、Market/DAY 的冻结 intent；只从宿主传入的 verified mapping 构造官方 `ibapi.contract.Contract` 和 `ibapi.order.Order`。V1 映射限定 SMART/USD/STK，要求正 `conId`、来源说明、时区时间戳且默认不超过 30 天。它不做合约搜索/账户读取，映射可信度仍由外部宿主负责。
- 新增 `paper_transport.py`：将 Backtrader native command 与冻结 intent 逐字段核对后才调用 SDK submitter 的 precommitted seam；不会连接 Gateway。`IbapiOrderSubmitter.submit_precommitted` 校验正 broker ID 并再次做注入式 pre-dispatch check，`EClient.placeOrder` 异常进入 UNKNOWN，不自动重发。
- 新增 `order_ids.py::IbkrOrderIdAllocator`：SQLite 持久化 cursor，`nextValidId` 未在本进程 epoch 到达时拒绝分配；收到小于已分配 cursor 的 stale 值会锁定 allocator，必须人工 reconcile；分配前在事务中递增 cursor。对同一规范化 DB path 使用 POSIX `flock` 限制单一本地 owner；不构成多机/账户级唯一 owner。
- `IbkrCallbackPump` 新增 `next_valid_id_sink` 注入点，先校验回调为正整数，再交由宿主调用 durable allocator observer；缺 observer 时 fail-stop。`BacktraderDispatchPort` 现在要求显式提供 `allocate_order_id` 和 `observe_next_valid_id`，并在构造时将 observer 绑定到 callback pump；app service 启动代码还未正式实现这组依赖的创建和生命周期管理。
- B37 独立官方 SDK/runtime：`ibkr-runtime/.venv/bin/python -m unittest discover -s ibkr-runtime/tests -v`：退出码 0，**19 tests passed**。覆盖 SDK 类型/订单字段、paper/account/order shape、映射新鲜度、native intent 比对、precommitted boundary、ID 持久递增/新 epoch、stale/invalid ID 与 local owner lock。
- B37 callback hook：`./.venv/bin/python -m pytest -q agent/tests/execution/test_ibkr_callback_pump.py`：退出码 0，**21 passed**，包括 nextValidId 注入与非法 ID fail-stop。`compileall` 与 `git diff --check` 均通过。
- B38 相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**209 passed、563 warnings**。
- **边界：** 仅本地 fake client/callback 和官方 SDK 对象构造，无 Gateway 连接、账户读取、纸面或实盘订单。跨进程分布式 owner、fresh IBKR connection lifecycle、合约来源审核、app service wiring、账户事实/风险 provider、重启后券商 reconciliation 仍未完成。


## P3a 本机账户/环境 execution owner lease（2026-10-21）

- 新增 `agent/src/execution/owner_lease.py::ExecutionOwnerLease`：对规范化 broker/account/environment 生成稳定锁文件，用 POSIX `flock` 做非阻塞独占，锁文件同步写入 owner ID/PID 诊断元数据。账户与环境分别隔离；租约对象需由可信 host service 持有到运行结束。
- `BacktraderDispatchPort` 现在必须注入有效租约；每次派单读取 intent 后逐项核对 broker/account/environment scope，不匹配或租约已关闭时无法进入 risk dispatch/send。既有 Cerebro 组合测试改为使用真实租约对象。
- B39 专项：`./.venv/bin/python -m pytest -q agent/tests/execution/test_owner_lease.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py`：退出码 0，**28 passed**。包括同一 scope 第二 owner 拒绝、不同账户或环境可独立持有、scope mismatch fail-closed，以及另一 OS 进程争用被拒绝。
- B40 相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**212 passed、563 warnings**。
- **边界：** flock 仅在相同主机和同一 lock directory 的共享文件系统语义下提供本地互斥；不同机器、不同路径、绕过该 dispatch port 或 app 未统一 lock directory 的情况不覆盖。没有实现 distributed lease/heartbeat/管理授权，也未接 app 启停 lifecycle；不连接 Gateway、不读取账户、不发送订单。


## P3a owner lease scope 与权限加固（2026-10-21）

- 新增拒绝测试：合法的 paper intent 在不同账户的有效 lease 下必须于风险检查和 transport 前拒绝。Owner lock directory 现强制 `0700`，包含 owner ID/PID 的 lock metadata 文件强制 `0600`，避免本机其他用户读取账户 scope。
- `ExecutionOwnerLease` 的跨 OS process 争用测试使用独立 Python 子进程；账户/环境不同可并行持有同一锁目录里的不同锁。旧 owner lease 和 SDK order-ID allocator lock 都仍是单机/同文件路径保护，不替代远端租约或 IBKR client 管理。
- B41 专项：`./.venv/bin/python -m pytest -q agent/tests/execution/test_owner_lease.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py`：退出码 0，**30 passed**；包含锁目录/文件权限以及租约 scope mismatch 的零发送路径。
- B42 相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**214 passed、563 warnings**。`compileall` 与 `git diff --check` 通过。
- **限制：** app service 尚未创建、持有和释放 lease，也没有受鉴权的启停控制；各 deployment 必须先统一 lock directory 才能形成该主机上的唯一账户 owner。未实现心跳、过期 lease recovery 或跨机器分布式锁；不接 Gateway/账户、不下单。


## P4 持久运行控制与派单闸门（2026-10-21）

- `SQLiteOrderLedger` 新增按 broker/account/environment 隔离的 `execution_control` 表，持久记录 `PAUSED`、`RUNNING`、`STOPPING`、`STOPPED`、`RECOVERING`、`ERROR`；没有控制记录的账户范围默认 `PAUSED`。`PaperOrderDispatchService` 在读取风险快照之前检查为 `RUNNING`。
- 最终 `start_send_after_risk` 的 `BEGIN IMMEDIATE` 事务内再次读取同一账户范围的控制状态；暂停/停止与派单争用时，非 RUNNING 状态会清除未发送 reservation、回到 CREATED，不写 submission attempt，也不调用 sender。该提交之后的在途发送仍按 UNKNOWN/对账规则处理。
- B43 定向组 `./.venv/bin/python -m pytest -q agent/tests/execution/test_dispatch_service.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings`：退出码 0，**23 passed**。覆盖新 scope 默认暂停、暂停时不加载风险事实、暂停发生于二次检查阶段时事务闸门阻止发送/释放预留，以及原有 Cerebro dispatch。
- B44 扩展相关组 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**216 passed、563 warnings**。
- **限制：** 这是本地 SQLite 的可信 host gate，不提供认证管理 API、自动生命周期、在途撤单或恢复 reconciliation；调用者需显式将经验证的 scope 设为 RUNNING。未连接 Gateway、未读账户、未发送 paper/live 订单。


## P3a 策略专用 IPC 协议与边界（2026-10-21）

- 将逐事件 worker 的 JSON-over-stdio 请求/响应定为版本 1 协议。host 每次生成随机 request_id；响应必须回显 protocol_version、request_id 与当前 event_id。请求只含冻结 source、一个 completed bar、JSON checkpoint 和 event_id；不传 run/account/deployment/actor/grant/sequence/management 命令。worker 返回外层协议字段后，host 只将 `event_id/state/order_proposals` 交给严格 `WorkerEventResult`。
- B45 定向组 `./.venv/bin/python -m pytest -q agent/tests/execution/test_event_worker.py agent/tests/bt_runtime/test_backtest_event_journal.py agent/tests/bt_runtime/test_python_upload_service.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**27 passed、284 warnings**。负例覆盖旧协议、过期 request_id 和伪造账户/管理字段，拒绝后 journal checkpoint/outbox 无写入。
- 关联 ID 是帧关联与重放保护，不作为策略授权身份。它仍是一次性容器 stdio 通道，不是长期服务 socket，也没有管理通道认证。未连接 Gateway、未读取账户或发送订单。


## P4 容器内策略硬期限与宿主失联清理（2026-10-21）

- `PythonSandbox` 从 digest 固定 image 读取 exec-form command，并以容器内 `/usr/bin/timeout` 作为 PID 1 包裹 worker。每次调用的 timeout 参数不可由上传代码改写；GNU timeout 在期限后发 TERM 并于 2 秒后发 KILL。Docker `--rm` 让 daemon 在容器进程结束后自动删除，正常路径清理对 daemon 确认的已删除容器幂等。
- worker 镜像固定 300 秒默认硬上限；sandbox 按 invocation 设置更短上限。B46 使用真实专用 Docker/Colima daemon：启动含死循环的 worker 后强杀宿主 Python supervisor，验证 worker 在镜像 deadline 后终止、容器自动消失。
- B46 命令 `./.venv/bin/python -m pytest agent/tests/execution/test_python_sandbox.py agent/tests/execution/test_python_sandbox_integration.py agent/tests/execution/test_strategy_worker_docker_integration.py -q --disable-warnings`：退出码 0，**17 passed、24 warnings**。
- **限制：** 仅证明 worker deadline 与 Docker 容器清理；不证明 SDK/执行服务在策略挂死时继续接回调、授权撤单、告警或拒绝未派发订单。容器内截止上限也不是执行服务的 heartbeat/恢复协议。未连接 Gateway、未读账户、未发送订单。


## P4 版本化 IPC 与 sandbox 故障基线（2026-10-21）

- B45 定向组 `./.venv/bin/python -m pytest -q agent/tests/execution/test_event_worker.py agent/tests/bt_runtime/test_backtest_event_journal.py agent/tests/bt_runtime/test_python_upload_service.py agent/tests/bt_runtime/test_python_upload_backtest.py agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**27 passed、284 warnings**。
- B46 sandbox/Docker 专项 `./.venv/bin/python -m pytest agent/tests/execution/test_python_sandbox.py agent/tests/execution/test_python_sandbox_integration.py agent/tests/execution/test_strategy_worker_docker_integration.py -q --disable-warnings`：退出码 0，**17 passed、24 warnings**。真实 Docker 用例强杀宿主 Python supervisor，worker 在镜像硬 deadline 后退出并自动删除容器。
- B47 相关后端子树 `DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**224 passed、563 warnings**。
- 原交接基线 `./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings`：退出码 0，**46 passed、148 warnings**。
- 当前工作区仍含之前的既有 tracked 修改和 Backtrader 新增文件；本轮只调整 Backtrader system 文档与本目标新增 execution/runtime/tests 代码，无 reset/stash/清理。没有连接 Gateway、读取账户或发送 paper/live 订单。


## P4 paper execution lifecycle primitive（2026-10-21）

- 新增 `agent/src/execution/execution_service.py::PaperExecutionService`。该对象只接受 PAPER scope，持有 `ExecutionOwnerLease` 到安全停止；启动时写入 RECOVERING 并调用 host 注入的恢复检查，恢复成功后转为 PAUSED，需显式 `activate()` 且 readiness 检查严格返回 True 才转 RUNNING。
- `pause()` 立即把 durable gate 设为 PAUSED；`stop()` 先写 STOPPING，只有 host 注入的 quiescence 检查证明没有未处理发送/订单后才写 STOPPED 并释放 lease。未静默或检查异常时保留 STOPPING/lease。`close()` 调用同一 stop/quiescence 路径，不能在静默未确认时释放 lease。
- B48 定向 lifecycle/owner/dispatch 组：`./.venv/bin/python -m pytest -q agent/tests/execution/test_execution_service.py agent/tests/execution/test_owner_lease.py agent/tests/execution/test_dispatch_service.py agent/tests/execution/test_backtrader_dispatch.py --disable-warnings`：退出码 0，**33 passed**。
- B49 完整相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**230 passed、563 warnings**。
- **边界：** `PaperExecutionService` 仍是注入检查的 lifecycle primitive；没有由应用创建 IBKR client/callback pump、facts provider、Backtrader run、管理 API 或 deployment registry。测试用布尔 fake 证明状态迁移和租约持有次序，不证明券商恢复/静默本身真实完成。不连接 Gateway、不读取账户、不发送订单。


## P4 Python paper event → Cerebro bridge（2026-10-21）

- 新增 `PythonPaperEventBridge`，在真实 Cerebro `Strategy.next()` 的 host 调用点串联 `PythonEventCoordinator`、`PythonOutboxIntentHandoff`、SQLite ledger、`PaperExecutionService` 与 `OneShotLedgerIntentResolver`。进入 worker 前校验 scope 为 PAPER/RUNNING、lease 持有、账户一致、无旧 dispatchable outbox、无该 broker/account/environment 的 CREATED/RESERVED/活动订单；存在 durable outstanding event 时先对照当前完整 Bar，避免过期 event 触发 worker。
- 每事件只允许一个 proposal；handoff 链接后核验 intent 的 seq、bar end、instrument、side、数量、type、TIF 与当前 event 一致，再让当前 native `buy/sell` 同步消费 one-shot intent。任意不匹配/风险拒绝/不确定结果都会持久暂停 run，并在可能时暂停 execution service。
- `SQLiteEventJournal.peek_next_event` 只读 authoritative cursor 并验证 payload hash；`pause_run` 将运行闸门置为 PAUSED。`SQLiteOrderLedger.recovery_snapshot_for_scope` 只筛选指定 broker/account/environment 的恢复队列。
- B50 定向真实 Cerebro、event worker/journal 专项：`./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_event_bridge.py agent/tests/execution/test_event_worker.py agent/tests/execution/test_event_journal.py --disable-warnings`：退出码 0，**20 passed**。覆盖单 proposal native order、多 proposal 在 intent/send 前拒绝、durable event 与当前 Bar 不符时 worker=0/order=0，以及 worker 既有 crash retry 语义。
- B50 完整相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**235 passed、563 warnings**。
- **边界：** bridge 测试中的 native port 在提交前验证 one-shot resolver 并记录真实 Backtrader native order；它没有调用 `BacktraderDispatchPort`/IBKR transport。发送与回报仍只有分散离线测试，没有 app runtime 组合、外部账户事实源、订单/现金/持仓恢复、管理 API、Gateway 或 paper 证据。


## P4 bridge → dispatch → callback 离线组合与持久合约身份（2026-10-21）

- `test_paper_event_bridge.py` 的真实 Cerebro path 现注入 `BacktraderDispatchPort`：host handoff 的 intent 经 one-shot resolver、native order 校验、持有中的 PAPER owner lease、两阶段风险检查和 SQLite ledger 后进入 fake transport；同步的 normalized execution callback 经 durable callback pump 更新 ledger，并在 Backtrader 消费后完成 native fill。
- `BacktraderDispatchPort` 要求可信 host 提供 verified `ContractIdentity`。发送事务和 callback index 都持久化 conId、symbol、security type 与 currency；pump 在 openOrder/execution 处理前将回报合约逐字段匹配绑定。intent symbol 与合约 symbol 必须相同，dispatch 只接受 STK。缺字段的迁移旧 mapping 无法解析；不完整或不匹配回报不会进入 fill ledger。
- `PaperExecutionService.close()` 现在走 stop/quiescence 路径。静默检查失败时保持 STOPPING 并继续持有 owner lease；只有 quiescence 明确成功才可 STOPPED 并释放 lease。生命周期用例覆盖失败后再完成停止。
- B51a 定向回归：`./.venv/bin/python -m pytest -q agent/tests/execution/test_paper_event_bridge.py agent/tests/execution/test_backtrader_dispatch.py agent/tests/execution/test_ibkr_callback_pump.py agent/tests/execution/test_execution_service.py --disable-warnings`：退出码 0，**43 passed**。包含错误 conId、symbol、security type、currency 逐项拒绝并证明 ledger fill 数仍为零。
- B51 相关后端子树：`DOCKER_CONTEXT=colima-vibe-execution ./.venv/bin/python -m pytest -q agent/tests/execution agent/tests/bt_runtime agent/tests/test_backtrader_backtest_api.py --disable-warnings`：退出码 0，**241 passed、563 warnings**。
- **边界：** 完整的是本机离线对象组合和 fake transport；没有实例化官方 SDK client、没有 Gateway lifecycle、权威账户/报价 facts、管理 API、跨机器 deployment owner、重启 broker reconciliation 或 paper ack/fill 证据。verified contract resolver 必须由未来可信 app service 接入正式合约来源；该 resolver 目前由测试注入。
## P1 离线成本模型加入显式滑点比例（B77，2026-10-21）

- 受信任 SMA 与 Python upload runner 都将 `slippage_fraction` 传给真实 BackBroker 的 `set_slippage_perc`，配置为下一根 bar 市价单开盘滑点，启用撮合高低价范围约束。报告 `provenance.cost` 声明固定模型名称和比例；Python upload 的第二套 Cerebro/BackBroker replay 使用相同参数，并核对报告声明和成交结果。
- SMA preview 默认参数含 `slippage_fraction=0.0`，并进入 preview review hash 与保存 run config。Python upload 请求限制为有限的 0–0.25，参数进入 preview summary/hash、run config、immutable `backtest_run_config.json`、resume inputs 和 Execution 页确认卡；编辑/篡改后须重新 preview。默认 0 保留既有无滑点输出。
- 新增实成交价偏移与独立重放断言、SMA provider-import 对比、preview token 成本参数篡改拒绝、前端 payload/显示断言。滑点比例是显式模拟假设，未以真实 provider、IBKR 成交或订单规模做校准。
- B77a 正确上下文定向组 **21 passed、1 deselected、331 warnings**；B77b service 持久化组 **1 passed、24 warnings**；B77c Execution UI **3 passed** 且 Vite build 退出码 0；B77d `git diff --check` 与相关 `compileall` 通过；B77e 相关 execution/Backtrader/API/PAPER 子树 **330 passed、1 deselected、693 warnings、15 subtests passed**。一次包含全子树的运行暴露了 SMA provenance 完整字典断言需要随新增滑点字段更新，已修正并通过复跑。唯一排除项是与本轮无关的旧测试 `agent.tests...` 导入布局问题。
- 没有连接 Gateway、读取账户或发送 paper/live 订单。

## P0/P1 伪造未来游标触发持久暂停（B78，2026-10-21）

- `SQLiteBacktestEventJournal.get_or_process_event()` 对 `seq != next_seq` 的运行中请求不再只回滚并返回错误；现在同一写事务持久将 run 标记为 `PAUSED`，不插入 event，不推进 cursor。对未提交既有 event 的游标错位也采用同一 fail-closed 处理。PAUSED 状态后续正确 seq 仍被拒绝，需显式人工审查路径。
- 单测构造“seq=2 + Bar2 的正确 available_at”越过 seq=1，断言 worker 0 次、next_seq 仍为 1、无 event 记录、状态 PAUSED，之后 seq=1 仍拒绝；正常顺序在独立 run 中继续验收。
- 真实 Docker worker + 真 Cerebro 集成测试在可信调用 seam 注入未来 seq 和匹配时间戳，再通过实际 Python upload service、BackBroker/journal 观察到 PAUSED；验证无 event/worker，重新从正确 host cursor 发起仍被 journal pause 拒绝。同一测试随后用新 run 完成真实 worker/Cerebro 正常路径。
- B78a journal **6 passed**；B78b Docker+Cerebro 组合 **1 passed、20 warnings**；B78c 相关 execution/Backtrader/API/PAPER 子树 **330 passed、1 deselected、697 warnings、15 subtests passed**。`T39` 仍 PARTIAL：本轮补强可信 host 游标篡改，不覆盖完整 introspection/逃逸矩阵，也不认证行情供应商。
- 未连接 Gateway、未读取账户、未发送 paper/live 订单。
