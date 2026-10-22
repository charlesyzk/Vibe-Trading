请建立并持续推进以下 goal：在已有实现基础上，完成 Vibe-Trading + Backtrader 的双入口策略系统，让用户通过自然语言生成策略，或上传符合模板的 Python 策略，完成真实 Backtrader 回测，并将同一固定版本授权运行于 IBKR 模拟盘，看到订单、成交、持仓、风控和恢复状态。

主工作目录：
/Users/yaozekun/strategy_verification/vibe-trading

这是实施任务。请直接检查代码、补齐实现、运行验收并更新进度；不要停留在重新写方案。优先级为交易正确性与账户边界 > 功能完整性 > 开发速度。断点续做，避免覆盖已有工作。

1. 已定方向与默认范围

Vibe 为主应用，Backtrader 为固定版本引擎依赖；首期美股普通股票/非杠杆 ETF、IBKR、USD、整股、只做多、常规时段日线、单标的、单用户。同一账户/环境只运行一个主动策略。先完成 SMA，再按方案扩展 RSI。

两种入口共用版本、Backtrader 运行、风控、账本和报告。NL2 生成受控规则；Python 上传允许模板接口内的自定义指标和交易条件。明确区分目标比例与指定股数，不重算用户明确给出的股数。运行中固定版本，修改须生成新版本。

为 IBKR 其他市场、QMT 和加密货币留适配接口，本次不实现这些市场。live 能力默认禁用；真实资金交易不在本 goal 的授权范围。

2. 先读资料并确认续做起点

读取适用 AGENTS.md，再读 docs/backtrader-system/ 中：
PROGRESS.md、BLOCKERS.md、DECISIONS.md、BASELINE.md；
README.md、REVIEW.md、ARCHITECTURE.md、CONTRACTS.md、DELIVERY_PLAN.md。

设计 v1.2 是产品/执行契约，进度记录与实际代码决定从哪里继续。AGENT_START.md 和 EVIDENCE.md 的早期环境读数是历史信息，不应据此覆盖当前实现。

2026-10-20 交接核对：已有执行契约、Decimal 数量规则和真实 Cerebro/BackBroker SMA 合成回测切片。以下命令刚复跑通过，46 passed、148 条警告、退出码 0：

./.venv/bin/python -m pytest agent/tests/execution/test_contracts.py agent/tests/execution/test_position_sizing.py agent/tests/bt_runtime/test_sma_backtest.py -q --disable-warnings

先记录 HEAD、dirty 文件、依赖和现有测试结果，核对上述基线。差异要定位，不倒退代码来凑旧数字。给出不超过 10 行开工说明后继续实现。

P0 探针已做，但并非全部安全门槛通过：macOS runner 隔离未成立；关闭 preload/runonce 仍可从同进程 Feed 读取完整未来数据；跨进程 Broker、账户状态恢复尚未完整验证。quicknotify=True 的空 bar 通知探针可用。官方 ibapi 10.50.2 来自本地官方源码目录，不能直接用同名 PyPI 包替代。

3. 修改边界

主要修改 agent/src/execution/、agent/src/bt_runtime/、相关 backtest/API/策略生成入口、frontend execution 页面与必要路由、对应测试、依赖声明/锁文件和本交接目录。允许完成任务必需的依赖安装、实现修复和有意义的测试。

工作区有其他任务的 provider、聊天/设置、多语言、Electron 等未提交改动，先识别归属；必要接入做最小修改，禁止 reset、stash、批量覆盖或清理。不得整包暂存并提交他人修改。新 worktree 不自动包含这些未提交实现，优先在当前工作区有边界地续做。

/Users/yaozekun/strategy_verification/backtrader 只读参考。优先在 Vibe 内通过 Feed/BrokerBase 扩展；若确需改 BT 核心，记录公开扩展无法解决的复现与最小补丁方案。Kelly 研究资料、凭据和无关数据不属于修改范围。

4. 按依赖完成剩余实现

A. 补齐 P0 未通过项和 P1：逐事件数据由可信进程释放；落实上传代码的网络/文件/CPU/内存/超时隔离；完成日历、数据/provider/rules/cost 契约和官方 SDK 可重复安装方案。复用现有 SMA，实现真实数据导入、manifest、费用、滑点、完整报告；真实数据缺失要明确报告，合成数据继续用于离线验证。

B. 完成 P2：自然语言→规则确认，以及 Python 模板下载/上传→校验，两者进入不可变版本与真实 BT 报告。新增 execution_backend，保留旧 engine=daily/options 含义。不为过旧校验伪造 signal_engine.py。页面显示实际后端、版本、运行模式和数据限制。

C. 完成 P3 离线执行链：官方 ibapi 适配、真实 BrokerBase 代理、native Order/notify_order、SQLite 账本/outbox、账户独占、授权、资金预留和派单前复查。可信 execution_service 独占 SDK、账本和权威风控；strategy_worker 只能通过策略 IPC 提交当前事件结果。管理与策略通道分离，不信任代码自己填的账户、版本、actor 或事件序号。

D. 完成 P4：回调输出的状态/checkpoint/订单意图原子接收；发送结果未知时对账而非盲重发；JSON 状态恢复与影子重放；重复/乱序/部分成交/取消竞态；策略卡死时可信服务仍能处理成交、授权撤单及告警；持久事件补传；暂停、撤单、停止、清仓明确区分。用独立真实 BackBroker 重放订单日志核对上传策略报告。

E. 完成可以独立推进的代码和离线验收后，再处理真实 paper 配置。需要用户明确 paper 环境、账户、合约、预算、风控、有效期和清理动作，不能猜值。既有 live 4001/Read-Only 不是 paper 配置；不得自动关闭 Read-Only、连接未确认账户或发送真实资金订单。缺账户条件不阻止上述工程工作。授权齐备后执行真实 paper 验收，并记录遗留持仓/委托及收尾情况。

5. 验收与持续推进

按 DELIVERY_PLAN 的 T01–T44 建立证据矩阵，写实际测试名、命令、退出码和证据路径。运行与改动有关的后端回归、前端测试/构建及故障用例；不要仅凭数量宣称覆盖充分。

禁止删测试、放松断言、增加 skip、伪造 CSV 或把真实被测引擎换成 mock 来达标。外部 SDK transport 可用于故障注入，真实 Backtrader 必须执行，真实 paper 必须有券商回报。至少证明错误条件会被阻断，而非只测正常路径。相关检查通过后继续交付，不无理由重复跑测试。

每个阶段更新本目录 PROGRESS.md、BLOCKERS.md、DECISIONS.md，保留已完成和未通过的区别。同一问题反复失败三次先重查假设/换验证路径，推进其他无依赖工作。常规实现选择自行决定并记录；真正缺外部权限或账户参数时集中列最小待配置项，不反复索要通用“继续”确认。

6. 完成条件

一：两个入口均可在 UI 创建固定策略版本并获得真实 BT 报告；数量、费用、未平仓、时序与恢复正确，相关回归/构建及 T01–T44 有结果与证据。

二：两个入口分别具备明确授权的 IBKR paper 订单、成交、持仓与对账证据，并通过撤单、断线/重启不重复下单、事件补传和停止演练；交付启停、备份、恢复及已知限制说明。

没有真实数据、隔离未通过、paper 配置缺失或跨交易日观察尚未完成时，如实报告部分完成与剩余条件，不把离线 mock、what-if 或 NL2 单入口成功当成整体完成。不得因为预算/上下文将尽而宣布目标已完成。最终给出变更文件、实际验收摘要、阻塞、账户操作记录和精确续做位置。
