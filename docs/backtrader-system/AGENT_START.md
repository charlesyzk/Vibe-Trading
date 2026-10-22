# 开工任务书：Vibe 产品入口 + Backtrader 回测与 IBKR 模拟盘

## 1. 这项工作为什么做

让不会写代码的用户描述策略，也让懂 Python 的用户上传我们提供的 Backtrader 模板；两种入口都获得真实 BT 回测，再授权同一版本运行 IBKR 模拟盘，并看到订单、成交、持仓、风险和恢复状态。主开发仓库是 `/Users/yaozekun/strategy_verification/vibe-trading`，Backtrader 作为固定依赖，通过公开 Feed/BrokerBase 扩展接入。

先读上级 AGENTS 和 `docs/backtrader-system/README.md`、ARCHITECTURE、CONTRACTS、DELIVERY_PLAN、EVIDENCE。它们是设计约束，不是已实现功能。收到任务后先给不超过 10 行开工回执，再按 P0→P4 推进；P5 只做默认禁用的 live 能力与准入材料。普通任务即可执行，无需假设任何斜杠命令存在。

## 2. 已确认约束与建议默认

用户确认：美股+IBKR 优先；为 QMT/crypto/IBKR 其他市场留接口；单用户；系统自动交易但预设授权、风险与停止；先模拟盘；真实使用 Backtrader 回测和 paper/live 运行；支持 NL2 策略与上传 Python 策略。

设计默认：普通美股/非杠杆 ETF、USD、整股、只做多、RTH 日线、单合约、先 SMA 后 RSI；同账户环境一个主动策略；NL2 经固定模板生成原生 BT Strategy；上传 Python 必须符合 `bt.Strategy` 模板接口。两者冻结版本并共用 BT、风险、账本和报告。上传代码不能获得 SDK 凭据/任意网络；OS 沙箱和负向测试通过前只准离线回测。用户未提供金额、paper 账户或通知渠道，不能用演示参数代替授权。

## 3. 修改边界

可以修改 Vibe 的 `agent/src/execution/`、`agent/src/bt_runtime/`、相关 backtest tool/API/策略生成入口、frontend execution 组件、对应测试、依赖锁和本设计目录。复用现有策略目录与报告功能。共享 schema、pyproject/lock、API 总注册、前端导航由同一集成负责人维护。

Backtrader 仓库只读参考；若必须改核心，先给公开扩展无法解决的最小复现与补丁方案。不得动 Kelly 研究材料、用户凭据、无关数据、用户既有修改。不得 reset/stash/覆盖工作区、自动改变 Gateway Read-Only、自动部署 live 或向他人发送消息。

当前 Vibe HEAD `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf`，BT HEAD `b853d7c90b6721476eb5a5ea3135224e33db1f14`。两个工作区均有用户修改，先看 EVIDENCE 再重新取基线；新 worktree 不自动带上未提交代码或本交接文档。

## 4. 实施顺序

P0：核对环境和命令，记录基线；读 REVIEW.md，冻结公共 schema；完成 DELIVERY_PLAN 五项实验（沙箱、Broker 代理事件泵、事件/订单/checkpoint 事务、有状态恢复、逐事件数据与独立 BT 报告复核），之后再拆分开发。

P1：受控 SMA 和模板式上传 Python；冻结数据/manifest、时间和数量规则、真实 Cerebro+BackBroker、费用/滑点与报告导出。明确 target_fraction / explicit_quantity，不能覆盖用户显式数量；Python JSON 状态钩子、逐事件输入和独立 BT 报告复核必须落地。

P2：自然语言→规则卡及 Python 模板下载/上传→不可变版本→BT 回测报告的 UI/API。用 `execution_backend`，保留现有 `engine=daily/options` 含义；BT 分支不伪造 signal_engine.py。旧 native 回测功能明确标签。

P3：官方 ibapi BrokerBase、native order/notify_order、唯一账户 owner、SQLite 账本/outbox、资金预留、两次风控、授权/环境检查、受限 IPC。先 fake transport 故障测试，再在用户已确认具体 paper 账户/额度/有效期后做真实纸面账户验收。缺授权信息时记录最小待配置项，继续其他离线工作，不猜值、不碰 live 4001。

可信 execution_service 独占 SDK、授权/账本写入与风控；strategy_worker 只有隔离的 BT 和 Broker 代理。用户管理通道与策略通道分开，策略提供的 actor/account/事件序号不能当可信身份。每个回调正常结束后才原子提交状态与订单意图；策略卡死不能妨碍可信服务撤单/对账。

P4：重启对账、暖机抑制订单、未知状态冻结、重复/乱序/部分成交/取消竞态、SSE 补传、健康度、停止与清仓区分、部署备份恢复手册及 paper 观察。

LiveRunner 是调度 Vibe AI agent 继续对话/调用工具的运行器；Backtrader worker 是加载固定策略、逐事件更新指标并管理订单回报的交易运行时。两者不是一回事：不得让 AI 每次被唤起临时决定自动单。

现有 IBAPI Feed 是只读行情 POC，what-if 不等于成交；现有会话 SSE 会丢内存事件，不能作账本。子进程和 AST 不能证明上传 Python 无法访问交易 socket；必须做 OS 网络/文件隔离和 Broker 二次授权，否则上传策略只支持回测。

## 5. 验收与继续工作

以 DELIVERY_PLAN 的 T01–T44 建测试及证据矩阵。真实引擎不可 mock；外部 SDK transport 可注入故障；真实 paper 必须独立证据。不能伪造产物、跳过失败、放宽断言或拿回测/what-if 代替 paper。普通 CI 不自动连接账户。

环境实际读数：两 venv Python3.12.13；BT venv 有 backtrader1.9.78.123、ibapi10.50.2、protobuf5.29.5，无 IbPy/pytest；Vibe venv 无 backtrader/ibapi/pytest。P0 重新确认并建立依赖，不把未安装写成测试失败。本次设计评审没跑测试，755 是现有 test 文件数而非通过数。

每阶段写 `PROGRESS.md`；缺配置/权限/行情时写 `BLOCKERS.md`，记录已试方案与所需最小外部动作。后续会话先读进度继续。同一问题连续失败三次先重查假设/换可验证路径，继续不依赖阻塞的工作；不无期限重试，也不擅自宣告完成。

## 6. 完成条件

一：UI 自然语言到规则确认、真实 BT 可重现报告完成；相同数据版本下过去决策不受未来数据改变；未平仓和成本口径准确；相关回归、前端构建和故障矩阵有真实命令与结果。

二：明确授权的真实 IBKR paper 产生可对账 ack/fill/position，完成撤单、断线/重启不重复下单、持久事件补传与停止演练。paper 未就绪只能报告离线完成及阻塞，不能称系统全部完成。live 默认禁用，真实资金启用单独授权。最终交付文件/命令/证据/限制/账户副作用和恢复手册。

两个入口分别记录回测、恢复及 paper 证据。Python 只有离线能力时算部分完成，不能把 NL2 通过当成双入口已完成。
