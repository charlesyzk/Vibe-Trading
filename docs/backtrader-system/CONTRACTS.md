# 实现契约：策略、时间、订单、状态、API

设计版本 1.2。以下文件、类和路由除标记“已有”外均为拟新增；JSON 示例原为说明格式。P0 已将首批严格契约与状态枚举落入 `agent/src/execution/contracts/`，并有 `agent/tests/execution/test_contracts.py` 覆盖；市场日历/费用/风险授权及 API 持久化模型仍待实现和冻结。

## 1. 目录与接入点

```text
agent/src/execution/                  # 新增：应用域，不依赖 ibapi
  contracts/                          # spec、run、instrument、risk、event、API schemas
  strategy_versions.py                # 对接已有 strategy_store；不可变执行版本
  compiler.py                         # NL2 规则或 Python bundle -> 可重现 BT 产物
  uploads.py                          # 上传包校验、hash、依赖/资源限制
  capabilities.py                     # 按市场/品种/订单类型/模式声明能力
  service.py                          # 回测、部署、授权、运行控制的应用接口
  supervisor.py                       # 管理子进程、账户独占、命令与心跳
  reports.py                          # BT 产物 -> Vibe 报告适配
agent/src/bt_runtime/                 # 新增：真实 Backtrader worker
  main.py                             # 有限命令入口；不接受任意 Python 路径
  strategies/                         # trusted SMA / RSI + 用户策略 contract
  sandbox/                            # 隔离启动和受限 Broker IPC
  feeds/                              # 冻结数据、完整实时 bar 与补齐
  brokers/ibkr/                       # BrokerBase 子类、SDK连接、事件转换
  risk.py                             # 纯规则 + 账户事实与预留
  ledger/                             # SQLite、迁移、事务、事件、去重
  recovery.py                         # 对账、暖机、游标恢复
  exports.py                          # fills、equity、trades、metrics、manifest
agent/src/api/execution_routes.py     # 新增；与现有 live_routes 分开
agent/tests/execution/                # 新增：规则/API/报告/生成器测试
agent/tests/bt_runtime/               # 新增：真实引擎和故障恢复测试
frontend/src/features/execution/      # 新增：NL2 规则卡、Python 上传、授权、运行、订单和风险组件
docs/backtrader-system/               # 设计、证据、阶段记录、运行手册
```

复用已有 `agent/src/strategy_store`、`backtest_tool.py`、报告页面和 API 认证设施。公共依赖/路由注册/前端导航由集成负责人维护。新增目录位于现有 `src*` 包发现范围，避免另建未打包的顶层目录。

后端入口必须先分流 `execution_backend`，再应用对应文件/数据要求。现有 native runner 要求 `code/signal_engine.py`，BT 模板流程不应为了过验证生成无用的假 SignalEngine。已有 `engine=daily/options` 保留其原含义。

## 2. 身份与不可变版本

| 对象 | 最小字段 | 不变量 |
|---|---|---|
| StrategySpec | schema_version、strategy_id、source_kind、template_id/version 或 source_artifact_id、instrument_ids、bar_policy、parameters、position_policy、execution_policy | 严格 schema；未知字段拒绝；自然语言入口不接受代码；上传入口使用隔离验证 |
| StrategyVersion | version_id、canonical_spec_hash、artifact_hash、source_kind、compiler_version、dependency_lock_hash、created_at、review_record | 内容不可变；参数、模板、代码、依赖、时间语义变更生成新版本 |
| ValidationRecord | version_id、data_manifest_hash、engine_version、checks、status、artifact_uri/hash | 只证明所列检查和数据；不代表收益保证或已获交易授权 |
| Deployment | deployment_id、version_id、broker/account/environment、grant_id、runtime_config_hash | 部署固定版本；AI 修改草稿不影响它 |
| Run | run_id、mode、execution_backend、deployment_id 或 version_id、started_at、state、reason | run_id 只标识本次进程逻辑运行，不充当策略版本 |
| Instrument | instrument_id、asset_class、symbol、currency、venue、calendar、timezone、lot_size、tick_size、multiplier | symbol 非全局 ID；精度与步长来自合约资料 |
| BrokerContractMap | instrument_id、broker、conId/合约字段、verified_at、capabilities | 经合约查询确认，不能由 AI 猜 conId；更新需审计 |

hash 使用规范化 JSON（稳定键排序、确定数字格式、明确编码）和固定算法；hash 字段不包含自身。NL2Strategy 版本 hash 绑定规范化 StrategySpec、可信模板和编译器；Python 上传版本 hash 绑定源文件、参数/metadata、允许依赖锁、Backtrader 与 runtime 版本。策略产物只按注册 ID 从只读目录加载，路径解析不得逃出 artifact 根目录。依赖升级视为新的 execution bundle，需要重新验证。

上传包首期只接收单个 `.py` + 声明式参数文件；限制总字节、路径和文件个数，不安装用户自带 wheel/依赖。依赖来自固定 allowlist。策略定义唯一入口类 `UserStrategy(bt.Strategy)`；指标在 `__init__` 初始化，在 `next` 读取已完成 bar；按需实现 `notify_order`/`notify_trade`。只允许通过 `self.buy/self.sell/self.cancel` 和受限只读接口读持仓/现金。`os/subprocess/socket/requests`、IB API、反射执行、动态 import、任意文件读写不属于策略契约。静态拒绝列表不是安全边界，必须有 OS 隔离；Broker IPC 还要独立验证每个订单。

当前 Python upload event API 使用 `on_event(event, state)` 入口，允许 `collections/decimal/functools/itertools/math/statistics` 这些标准库模块；拒绝其他 import、相对 import、AST 中直接出现的动态执行/反射调用和源码中直接访问的 dunder 名称/属性。validator 在 upload preview/create 的 shared preparation、恢复冻结版本前以及在线 Python PAPER coordinator/session 的可信边界运行；通用 offline event journal / worker invoker 不运行该策略，保留底层 adversarial runtime probes。此 AST 筛查仅定义兼容 API 面并尽早拒绝不支持代码；它不能证明任意 Python 无法绕过，Docker 网络/文件系统/资源隔离仍是运行边界。

`source_kind` 只允许 `natural_language_template` 与 `python_upload`，参数清单均须声明类型、默认值、范围和单位。二者使用相同的市场/执行限制，但数量来源可不同：NL2 默认 `position_policy.mode=target_fraction`；Python 首期支持 `explicit_quantity`，由代码明确给出整股数量。上传模板可调用统一 target helper 采用前一种模式，不能同时隐式采用两种 sizing。模式写入版本和规则卡；系统只在规定模式下计算数量，不能覆盖 Python 明确提交的数量。自带 Cerebro/Broker/Feed、无限循环主程序、CLI 或其他框架脚本均不是可直接导入的策略。

首期新增策略订单只允许在完整日线的 `next()` 决策阶段提出，订单/成交回调可更新状态；回调中再买卖、盘中事件驱动加仓等属于后续新 capability。退出/取消也必须声明原因和归属。AI 对代码的解释仅作辅助，不能自动替用户修改上传代码或赋予交易资格。

用户状态须声明 `state_schema_version`，并提供 JSON 格式的 `snapshot_state()` / `restore_state(state)`；无自定义状态的模板返回空对象。计数器、上次动作、策略缓存等影响决策的自定义变量都必须覆盖，不使用 pickle 或序列化可执行对象。指标内部状态优先从原始完整前缀重放重建；不能假定重放最近 slow_period 根数据足以恢复所有指标。无明确状态契约或续跑验收失败的策略不能自动恢复交易。

### 2.1 首个策略 schema 示例

以下参数仅为离线说明，不能视为账户授权；`instrument_demo_us_equity` 为占位 ID。

```json
{
  "schema_version": "1.0",
  "strategy_id": "sma_demo",
  "source_kind": "natural_language_template",
  "template_id": "sma_cross",
  "template_version": "1.0",
  "instrument_ids": ["instrument_demo_us_equity"],
  "bar_policy": {
    "timeframe": "1d", "session": "regular", "completed_only": true,
    "calendar_id": "us_equity_rth", "timezone": "America/New_York"
  },
  "parameters": {"fast_period": 10, "slow_period": 30},
  "position_policy": {
    "mode": "target_fraction",
    "long_only": true, "whole_shares": true, "allocation_fraction": "0.20",
    "rebalance": "on_signal_transition", "initial_entry": "wait_for_cross"
  },
  "execution_policy": {
    "decision": "completed_bar_available", "dispatch": "next_regular_session",
    "order_type": "market", "time_in_force": "DAY", "outside_rth": false,
    "gap_action": "reject_and_pause", "missed_session_action": "expire_and_pause"
  }
}
```

SMA 定义：fast/slow 为完整 bar 收盘价简单平均；上一有效差值 ≤0、当前差值 >0 为入场；上一差值 ≥0、当前差值 <0 为退出。需要 slow_period + 1 根有效暖机 bar；无有效上一值不触发；进入样本时已经 fast>slow 默认等待新的交叉。零值比较不使用随意四舍五入。

### 2.2 RSI threshold template (`rsi_threshold/1.0`)

- 参数必须严格为 `{period, entry_below, exit_above}`；`period` 为整数 2–250，两个阈值为有限数且满足 `0 <= entry_below < exit_above <= 100`。不提供隐式 14/30/70 默认值。
- 指标为 Wilder RSI。使用 close-to-close 变化；前 `period` 个变化以正变化和负变化的算术平均初始化，之后分别递推 `(previous_average * (period - 1) + current_change_component) / period`。
- 两个平均值均为零时 RSI=50；平均亏损为零且平均盈利大于零时 RSI=100；平均盈利为零且平均亏损大于零时 RSI=0；其余使用 `100 - 100 / (1 + average_gain / average_loss)`。
- 第一项 RSI 在 `period + 1` 个 close 后可用。首个 RSI 仅建立前值，不追逐样本开始前已发生的超阈值状态。至少需要 `period + 2` 根 bar 才能拥有一次交叉判断。
- 空仓仅在前值 `>= entry_below` 且当前值 `< entry_below` 时买入；持仓仅在前值 `<= exit_above` 且当前值 `> exit_above` 时卖出。恰等于阈值不触发；RSI 与阈值绝对差不超过 `1e-12` 时视为相等，以消除二进制浮点误差。完成 bar 上决定，下一有效常规交易时段以 Market/DAY 执行，与 SMA 共用数量、费用和风险口径。
- preview 与不可变版本必须绑定模板源码 hash、compiler 版本、全部 RSI 参数和完整 StrategySpec；preview→create 复用同一冻结 spec。
- NL2 可使用严格 RSI intent JSON，但 intent 仅能填写模板 schema 中的参数与固定 rule enum。period、entry threshold 和 exit threshold 各需提供 exact evidence quote；host 校验 quote 原样出现在用户描述中、数字与字段相等，且 quote 语义分别关联 RSI 周期、向下穿越和向上穿越。缺参不得默认补值；附加规则进入 `unsupported_conditions` 并 fail-closed。该检查不保证模型没有漏提其他条件，UI 必须同时显示原描述和规则卡。

### 2.2 RunConfig

最少包含：schema_version、mode、execution_backend、strategy_version_id、data_manifest_id（backtest 必填）、起止区间/暖机段、currency、initial_cash（仅模拟）、commission_model/version、slippage_model/version、calendar_version、broker_profile_id（paper/live 必填）、grant_id（paper/live 必填）、seed（涉及随机模拟才有）、资源预算。

profile 通过安全配置取得 host/port/clientId/expected_account；不能让 AI 生成的 spec 指定任意 socket。paper/live 使用实际账户资产，禁止把 `initial_cash` 覆盖到账户快照。端口、账户名前缀只能作为检查信号，不能单独证明环境；必须同时有已确认的 Gateway 登录环境、预期账户白名单与连接后返回账户一致性。

## 3. 数据与时钟

### 3.1 Bar / DataManifest

Bar：instrument_id、session_date、start_at、end_at、available_at、timezone、open/high/low/close/volume、currency、is_final、source、adjustment、quality_flags。传输时间均为带时区 ISO 8601 UTC；session_date 是市场交易日，另行保存，不由 UTC 日期截取。

必须校验 OHLC 合法、时间顺序、重复、缺口、有限数值、负量、tick/lot 规则。价格可能因市场品种不同而有不同合法域，V1 股票禁止非正价格。尚未结束的日线不得送入策略。修订历史 bar 记录新数据版本，不能在运行中改变已批准的历史证据。

manifest：供应商、合约映射、字段说明、日期范围、数据可用时间规则、日历版本、复权和公司行动、下载参数、获取时间、文件 hash、质量报告、暖机/评价区间。合成测试数据明确标记 synthetic；真实数据权限失败不得自动换成合成数据生成“成功报告”。

历史供应商若没有原始 available_at，需保存保守可用时间模型及假设；不能宣称拥有逐时点修订历史。线上 bar finalization 延迟和补齐流程独立配置，来源迟到则延后决策/按过期策略暂停。

上传策略的认证回测必须由受信任数据服务按逻辑时钟逐事件释放输入：关闭 `preload` 和 `runonce`，策略进程不挂载完整历史文件/未来数据，不持有可任意请求未来 bar 的 IPC。仅关闭 preload 不够，PandasData 背后的完整 DataFrame 仍可能被代码直接访问；使用只接收已释放记录的 Feed。Backtrader 参数由可信启动配置固定，禁止策略自行创建替代数据源。测试覆盖正向索引、底层数组、文件/网络和伪造未来 cursor；报告说明这验证的是可访问数据边界，不可能识别策略源码里人为硬编码的历史答案。固定运行时、逻辑时钟和必要随机种子；依赖墙上时钟或重放不一致的代码不取得自动恢复资格。

市场扩展接口在 P0 冻结为小而完整的契约：`MarketDataProvider` 提供合约确认、历史快照、完整 bar/报价订阅和质量状态；`TradingCalendar` 提供有效会话/下一会话/会话边界；`MarketRules` 提供精度、交易单位、可卖规则、支持订单和交易时段；`CostModel` 提供估算费用与版本信息。返回值使用本文件领域对象，不把 IB SDK 原对象泄露到前端或策略规则。每个实现分别声明 history/realtime/backtest/paper/live capabilities；未实现的方法明确拒绝，不返回空列表假装成功。

### 3.2 数量计算与调度

V1 每个完整 bar 的 next 决策事件最多产生一笔新增方向订单；target_fraction 模式仅在目标状态转换时产生调整，explicit_quantity 模式按代码当前事件的明确指令处理。存在同标的未完成委托时不再创建第二个调整订单。取消后需等确认并重新对账再决定。

下面的数量公式仅适用于 `target_fraction`，进入持仓状态时，令：

- E：决策时已确认且足够新鲜的账户净值；
- B：用户给该策略的资金上限；w：规则中的目标比例；
- C：可用于本策略的已确认 USD 现金减已有预留；
- P：决策时可见的参考价格（V1 完整 bar close）；
- F、S：配置的手续费预留与价格缓冲，属于版本化模型；
- H：该策略已确认持仓数量。

买入目标金额为 `min(w × E, B)`；初步目标整股数为 `floor(目标金额 / (P × (1+S)))`。买入差额为 `max(0, 目标整股数 - H)`，再以 C、F 和单笔限额验证。资金不足可以在**决策阶段**按明确算法缩量并记录理由；不可在派单时偷偷重新优化原决策。退出信号目标为 0，卖出数量不得超过策略归属的可卖数量。

数量、参考价、E/C/H、预留、风险判定版本必须随 intent 持久化。此公式是 V1 建议，P0 冻结为精确纯函数；未来组合权重算法另建版本。

`explicit_quantity` 使用 Python 在当前决策事件明确给出的整股数，不再套用目标比例公式，也不自动缩量。超出现金、持仓或授权限制时整笔拒绝并给出理由。两种模式共享市场、时段、预算、未完成订单和派单前检查。Decision 在该模式下保存 side/qty/reason，`next_target` 可为空；不得为了符合字段伪造目标权重。同一个策略版本在回测和 paper/live 中保持自己的数量模式，不要求不同策略得到相同交易。

收盘生成 native BT Order，IB BrokerAdapter 接受并记录为本地 `QUEUED_FOR_SESSION`，尚不代表 IB 已接受。到下一有效常规时段，收到新鲜报价并通过 dispatch 风控再调用 SDK。若跨过派单窗口、报价未知或跳空越限，则不发送，并记录失败/暂停；默认不无期限保留昨日意图。

历史 BackBroker 的 Market 撮合使用下一 bar 开盘及配置成本；线上实际价格由券商回报给出。P0 需评估在线开盘门槛/缓冲与历史近似差异，报告明确写出；不得对历史进行未来价格筛选后声称完全等价。

## 4. 订单、成交和回报

### 4.1 领域对象

| 对象 | 必须字段 |
|---|---|
| Decision | decision_id、version_id、deployment_id、instrument_id、bar_end/available_at、input_hash、previous_state、next_target、reason |
| OrderIntent | intent_id、decision_id、dedupe_key、account/environment、side、integer_qty、order_type/tif、reference_price、eligible_at、expires_at、risk_snapshot_id |
| OrderRecord | internal_order_id、intent_id、bt_order_ref、client_id、ib_order_id、perm_id、order_ref、state、filled_qty、remaining_qty、last_event_seq、submission_attempt |
| Fill | fill_id、broker_exec_id、order_id、contract、account、side、qty、price、executed_at、received_at、commission_status、correction_of |
| AccountSnapshot | snapshot_id、account/environment、positions、currency_balances、net_liquidation、available_funds、as_of、completeness、source |
| RiskDecision | decision_id、scope、checked_at、result、rule_version、reason_codes、inputs、reservation_id |

金融数量/价格在持久化和 API 中用精确十进制字符串或约定的整数最小单位。Backtrader 指标计算可以用浮点；转换边界明确量化规则，不能用 float 全程存资金。V1 qty 必须为非负整数，方向单列。

幂等键包含账户/环境、部署、策略版本、可信服务分配的 input_event_seq 与动作序号。日线决策事件还绑定 bar_end。输入事件 ID、账户和版本来自已注册的进程通道绑定，不信任策略自行声称的值；重启不改变逻辑 deployment/decision。仅用新的 run_id 作为 key 会导致同一决定再次下单。

### 4.2 双层状态

本地发送状态：`CREATED → RISK_REJECTED`，或 `CREATED → RESERVED → QUEUED_FOR_SESSION → SEND_STARTED → ACKNOWLEDGED`；发送结果未知进入 `SUBMISSION_UNKNOWN`。状态字段保留本地与券商两个维度，不把“队列接收”显示成券商 Accepted。

券商状态聚合：`PENDING / ACCEPTED / PARTIALLY_FILLED / FILLED / CANCEL_PENDING / CANCELLED / REJECTED / EXPIRED / UNKNOWN`。对外显示同时带 filled/remaining。由于取消竞态和迟到成交，不做简单按枚举大小丢弃事件的状态机；订单即便显示取消也可能随后补到先前已发生的 fill，必须调整事实和风险。

在线代理映射到 Backtrader：回调内仅暂存的订单保持 Created；event_result 经可信服务持久化受理后才是 Submitted；Accepted 仅在券商确认后；Partial/Completed 按累计有效成交更新；Canceled/Rejected/Expired 按对应确认更新。`SUBMISSION_UNKNOWN` 在 runtime 挂起，不能伪造一个新的 Submitted 订单重试。内建 Trade/Analyzer 与重启后的实际账户分开处理，长期 P&L 以持久账本为准。

FastAPI 的 PAPER manager 与 readiness 是 app-scoped collaborator：status/pause 从 `Request.app.state` 读取，lifespan shutdown 必须使用同一个 app 的 manager；可信 composition root 调 `_set_paper_execution_composition(manager, readiness, app=app)`。不要用 module global 在多个 app 间共享活动 slot、readiness 或 shutdown owner。默认 app state 保持 inert，明确注册完整 composition 仍是单独的前置步骤。

不能依赖所有中间 `orderStatus` 都出现；同时处理 openOrder、execDetails、commission 与 error。券商官方要求监控这些回调。[IBKR 下单注意事项](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/order-placement-considerations)

IBKR `EWrapper.error()` 事件先按 primitive payload durable-spool。当前 pump 只将官方明确标注为正常状态通知的 2104/2106/2107/2108/2158 确认并忽略；1100/1101/1102/1300 必须 pause 并重连、重新对账；拒单、数据 farm disconnect 和未知 code 继续 fail-closed。不能仅因 callback `reqId` 数值与订单号相似，就将其视为 durable order binding 并写入 REJECTED；拒单入账需要单独验证身份映射、状态竞态和 late-fill 路径。码表参考 [IBKR TWS API message codes](https://interactivebrokers.github.io/tws-api/message_codes.html)。

### 4.3 BrokerBase 和 SDK 约定

BrokerAdapter 至少实现并按所锁定版本验证：start/stop、buy/sell/cancel、getcash/getvalue/getposition、next、get_notification，以及订单创建、clone notification 与成交更新所需行为。函数签名以实际 `backtrader/broker.py` 为准，不能把普通 async 下单函数冒充 Broker。

SDK Connection/Store 负责连接、clientId、合法 orderId 分配、合约确认、数据/账户订阅、回调队列和限流。客户端 orderId 依据握手与已见 ID 安全分配；permId 在券商分配后补充；orderRef 放可追踪内部标识，不承诺券商替我们去重。第一版不自动绑定、修改或取消人工/其他 client 的订单。

实时 DataBase 无新数据时 `_load()` 返回 None，结束时才 False。空数据轮询仍需运行 broker event pump 和控制命令；须用真实 Cerebro 做验收，不能只单测自己的队列。网络线程不直接执行 `strategy.next` 或写 BT position。

## 5. 持久化与恢复

### 5.1 所有权

应用数据库：策略草稿、版本索引、报告索引、用户界面记录。每个账户/环境的 runtime SQLite：授权副本、运行指令、意图、订单、成交、账户快照、预留、事件/outbox、游标、租约。唯一写入者与账户 owner 是**可信执行服务 execution_service**，持有 SDK 连接和权威风险配置；当前 `ExecutionOwnerLease` 只在本机同锁目录内为 Backtrader dispatch 强制单 owner，应用 supervisor/跨主机租约仍待实现；**strategy_worker** 只运行 Cerebro/Strategy/BrokerBase 代理，没有账本文件权限、账户写连接或管理通道。supervisor 负责隔离进程的启动和监控，不能由策略代码替换。本文历史表述中的 owner worker 均按 execution_service 理解。

最小表组：`deployments, grants, commands, decisions, intents, orders, submission_attempts, fills, fee_adjustments, reservations, account_snapshots, reconciliation_runs, events, delivery_cursors, runtime_checkpoints, schema_migrations`。可以合并物理表，但不能丢掉上述信息与约束。

唯一约束：decision/dedupe_key；account/environment + broker execId（支持更正关系）；command idempotency key；同账户环境一个 active deployment；event_seq 单调递增。权限文件、socket、DB 和 artifact 目录应属于对应受限用户。

### 5.2 事务边界

1. 将一个已排序 input event 发送给策略，附可信 event_seq、逻辑时间与可见账户快照。策略回调产生的 BT 委托先暂存在 BrokerBase 代理中，不即时外发；回调正常结束后提交 event_result（状态 JSON、全部命令、前一 checkpoint revision）。可信服务检查事件未过期/未处理、命令权限和风险，在一个事务中保存输入确认、checkpoint、decision、intent、风险预留与事件。没有订单的事件也提交 checkpoint。策略崩溃或超时且未提交完整结果时，不发送该批新订单；从最后确认点恢复。
2. 到派单时检查新鲜事实，在事务中记录 `SEND_STARTED`、确定 client/orderId 和发送尝试；提交。
3. 调用外部 SDK。同步报错若不能证明未送达，也按 UNKNOWN 处理。
4. 券商回报先由可信执行服务入账，在事务中去重、更新订单/成交/预留/持仓派生状态、写 events/outbox。即使策略挂起也正常进行；随后作为有序 input event 交给 BT 通知队列并更新 UI，策略状态的确认遵循第 1 步。相同 event_result 重交返回既有结果；同序号不同内容拒绝并暂停。
5. UI 推送失败不回滚已发生交易；outbox 可重试。

无法声称与券商之间 exactly-once。目标是本地幂等、检测不确定性、禁止未经对账的重发，并保留可核实的证据。

### 5.3 重启协议

恢复必须经 `RECOVERING/RECONCILING`，完成下列条件才能 RUNNING：

- 获得单机文件锁及有效租约；旧进程失去所有权后在每次 dispatch 前被阻断。租约过期不授权两个主机同时交易；V1 限单机。
- 加载已批准 bundle 和授权，核对 hash/有效期/环境；不一致暂停。
- 连接后核对 expected account；拉取 open orders、必要 execution 窗口、position 和资金，等待各自结束标记/完整性证明。
- 处理已知和未知订单，UNKNOWN 找不到也不能轻率视为不存在；券商可查询窗口不足时要求补充历史记录/人工处理。
- 对已有未终结订单重建 BT Order 与 broker 映射及累计成交，附着到已存在的券商订单；重建不是再次提交。新进程 BT ref 可以变化，持久 internal_order_id/intent_id 不变。若 adapter 不能可靠重建该类订单，则暂停等待其明确终结，不能带着空订单列表恢复策略。
- B158 已将该契约部分接入 managed Python event API：`initial_checkpoint_json` 固定新 run 的 seed；fresh reconciliation 后只读重放 `seq < next_seq` 的已提交前缀，核对事件与结果 hashes、逐条 WorkerEventResult、outbox proposal 及当前 checkpoint，且不写 journal 或调用 handoff/dispatch。历史 run 缺失 seed、数据损坏或重算不一致均持久 PAUSE。此切片尚未重建完整 Backtrader native indicator/strategy state，也未覆盖持仓/活动订单作为影子输入的通用协议。
- 仅从已持久化 decision cursor 之后处理新事件。缺失/过期决策按 schema 中 missed_session_action，不追下所有漏过的订单。
- 外部持仓/手动订单与策略归属冲突时暂停。恢复结果及证据 hash 落库。

恢复时若同一 run 的前序 committed proposal 留下唯一 exact-linked CREATED BUY，且 cursor 上只有一条 released-but-uncommitted 下一 bar，可以保留这两个持久身份并返回 PAUSED。显式 activate 先将 CREATED intent 交给现有二次风控/dispatch；在 ledger 到达 FILLED、CANCELLED 或 REJECTED 之前不重放 retry bar。SEND_STARTED/UNKNOWN、ACKNOWLEDGED 或 PARTIALLY_FILLED 均不允许推进策略事件；多 CREATED、额外事件和与其他活动单并存继续暂停。该离线状态机不替代真实 broker callback/reconciliation 验证。

成交重复去重，手续费晚到独立补充，成交更正追加修订关系。不能在去重时把不同 execId 的更正当新增净成交；具体 SDK 更正标识必须按锁定 API 验证。快照与成交时间不一致时进入 reconciliation，禁止多次累加同一仓位。

## 6. 授权、风控与 IPC

### 6.1 Grant

必填：grant_id、user_action_id、deployment/version hash、broker/account/environment、allowed_instruments/order_types、long_only/whole_shares、max_order_notional、max_gross_exposure、strategy_budget、max_daily_loss、max_orders_per_session、valid_from/until、quote/account_max_age、halt_action、liquidation_policy、granted_at/revoked_at。

paper 也必须走相同审批和风险代码。示例表单可以有未提交的建议值，但不能默认生成授权。已有旧授权若缺账户/版本/环境字段，不可静默升级使用。

风险定义：

- USD 总敞口按已确认持仓估值 + 尚可能成交买单的保守占用计算；拒绝过期估值。
- 日亏损基准为交易会话起始净值，使用已实现 + 未实现变化并剔除明确现金流。计算依赖不足时冻结新风险；不把 unavailable 当 0。
- 未完成卖单预留可卖数量；未确认 cancel 前不能释放。
- 订单数按发送尝试/已提交的约定计数，防止反复 rejected 无限重试；不以收到 Filled 才计数。
- 风险减少动作可在 grant 到期后根据原授权的 cleanup policy 执行；没有权限时仅监控并报告。所有自动清仓必须事前明确授权。
- 系统撤单、手动清仓、策略下单均经过同一个 owner 与审计入口，不新增隐蔽的直接 SDK 路径。

### 6.2 IPC

建议本机受权限控制的 Unix socket/等效平台 IPC，NDJSON 或明确帧协议。协议版本、request_id、command_id、issued_at、actor、scope、payload、expected_state_revision 必须显式；stdout/stderr 日志不混入机器协议。

必须分开两类通道/凭据：**管理通道**供经鉴权的用户控制层启动已登记 bundle、更新授权、暂停、停止、恢复或按权限清仓；**策略通道**只接收当前部署的已排序输入、返回事件结果和本策略订单/取消意图。上传代码可直接构造 IPC payload，因此管理通道不能挂载给它，`actor=user` 字段不是身份凭证。可信服务在启动时绑定策略通道的部署/版本/账户/环境，拒绝越界命令、伪造事件序号、重复 payload 和频率/字节超限。两类通道都没有 execute_code/shell/eval 或任意路径/端口接口。

当前离线实现使用一次一容器、JSON-over-stdio 的策略专用 request/response 通道作为受限 IPC：协议版本固定为 1，每条请求带 host 生成的随机 `request_id`；worker 只收到冻结源码、当前 final bar、当前 JSON checkpoint 与 event_id，并须回传相同 protocol/request/event identity。host 严格拒绝身份不匹配或携带账户/部署/actor/管理字段的响应。策略进程没有管理 socket、IB SDK、凭据或 ledger 文件挂载。该实现不等同于本机服务的管理认证、运行时 heartbeat 或持久双向 session；app service 与管理控制面仍待接线。

策略 heartbeat/事件超时和控制层 heartbeat 分别监控。Python 死循环、异常或失联时，可信执行服务停止新增风险、拒绝所有未派发策略单，并按已授权 cleanup 撤本系统在途委托；持续接收成交/对账和发事件。恢复运行需重新对账并确认策略状态；不能因为“进程还活着”就判定策略正常。

AI 工具和用户动作拥有不同调用凭据/能力；LLM 不能通过填写 `actor=user` 自行获得授权。单用户仍要有本地接口鉴权、浏览器 Origin/CSRF 防护、秘密脱敏；不能把“单用户”解释成允许任意网页调用交易接口。

## 7. API 与页面契约

路由前缀建议 `/api/execution`，按项目实际 API router 统一；以下为新设计。

| 方法/路径 | 行为 | 身份/前置条件 |
|---|---|---|
| POST /strategies/validate | 校验规则，返回可部署能力/缺项 | AI 可提出，不产生授权 |
| POST /strategies/{id}/versions | 冻结规则与 bundle | 严格校验；生成新版本 |
| GET /strategies/python-template | 下载版本化官方 Backtrader 模板和样例参数 | 给用户查看/编写；模板有 hash/version |
| POST /strategies/uploads | 上传 `.py` 和参数声明，生成隔离验证 job | 限大小/字段/依赖；上传本身不部署 |
| GET /strategies/uploads/{id} | 查看静态/动态验证结果、source hash、限制 | 动态导入只能在隔离执行器内 |
| POST /backtests | 返回 run_id，异步运行真实 BT | 指定 version/data/费用模型 |
| GET /runs/{id}、/artifacts | 状态、报告及 provenance | 成功/失败分明 |
| GET /capabilities、/accounts | 返回模式能力及脱敏账户/环境 | 不返回凭据 |
| POST /deployments | 建立未启动 deployment | 版本验证记录有效；Python 版本还要求隔离验收通过 |
| POST /deployments/{id}/grants | 用户审阅后授权固定 scope | user-only；schema 完整 |
| POST /deployments/{id}/start | 排队启动并对账 | 有效 grant + 独占 + 环境通过 |
| POST /deployments/{id}/actions | pause/cancel_owned/stop/resume/flatten | 独立动作语义；flatten 独立权限 |
| POST /grants/{id}/revoke | 撤销未来权限并进入 cleanup | user-only；返回实际处理状态 |
| GET /deployments/{id}/orders、/positions、/health | 读账本派生状态和 as_of | 目标/实际/未知分列 |
| GET /events?deployment_id=... | 持久事件 SSE 与游标补传 | 支持 Last-Event-ID 或 after_seq |

mutation 采用 idempotency key；同 key 不同 payload 拒绝。返回 202 仅代表受理，响应带 command_id；最终成功由命令状态/事件确认。HTTP 200 不能被 UI 翻译成“交易成功”。错误体包含 code、human_message、retryable、blocking_reason、correlation_id；敏感资料不回传。

事件 envelope：schema_version、event_id、seq、deployment_id、run_id、account_ref、environment、kind、occurred_at、received_at、persisted_at、entity_id、payload。SSE id 对应持久 seq；权限过滤后仍保持可恢复游标语义，不能靠内存数组索引。

UI 最少五个区域：规则卡/版本、回测报告、部署授权、运行总览、订单与风险记录。每个运行页面固定显示 backend、paper/live、账户、版本、连接状态、最后完整行情和最后对账时间。清仓按钮明确展示将处理的本系统实际持仓和估算，不承诺成交价。

## 8. 报告与口径

兼容原 Vibe artifact：`equity.csv`、`positions.csv`、`target_positions.csv`、`trades.csv`、`metrics.csv`、run_card 和 OHLCV。P0 对现有 reader 写 schema 测试后冻结映射；增加权威 `orders.jsonl/fills.csv/events.jsonl/data_manifest.json/execution_manifest.json`，报告索引记录 hash。

上传代码的回测成功只代表指定运行与检查通过，不称“收益正确/策略安全认证”。关键统计由可信导出器计算；要取得 paper 准入，须把受控事件边界记录的订单序列放入不加载上传代码的独立真实 BackBroker 回放，按相同数据/成本核对成交、持仓和权益，检测用户代码修改同进程 Broker/Analyzer 的结果。当前 Python upload verifier 将 OHLCV、importer Bar records、host decision log 和成本配置通过 hash-bound JSON 交给独立本地子进程运行第二套真实 Cerebro/BackBroker；bar/decision 最多 1,000 项，decision reason 最多 1,024 字符，请求与 stdout 各不超过 8 MiB、stderr 不超过 64 KiB，并受启动时限约束。子进程仅接收最小环境变量 allowlist，但仍与主应用使用同一 OS 身份，不是独立沙箱或签名工件服务。回放验证订单序列的模拟结果，不证明买卖逻辑没有人为硬编码或具备预测能力；不一致则该回测不用于部署验证。若 V1 暂未实现此复核，必须把上传策略的可部署状态标为待验证，而不是默认通过。

- equity 必须有初始基线，定义期初/期末计价与现金流；ret 为相邻评价点净值收益，drawdown 为相对历史峰值的比例。
- 原 trades reader 的 `return_pct` 为百分数，新字段不得把 0.05 和 5 混用。所有字段在 schema 标单位。
- 区分订单条数、成交笔数、完成往返交易数；不能拿三个名字表示同一行数。
- fills 保留所有真实成交，包括未平仓买入；往返交易统计另行派生。没有卖出时也应显示实际持仓和浮盈亏。
- 佣金按现金流计入；晚到费用保留 provisional 标记，生成报告 revision，不覆盖已归档结果。
- Sharpe 需声明频率、年化因子、无风险利率和样本不足处理；零波动/样本少返回 unavailable，不填假 0 或 infinity。
- 单币种 USD 为 V1；多币种账户的其他资产不自动纳入策略预算；账户整体与策略归属 P&L 分列。
- 所有收益展示绑定 execution backend、bundle、数据 hash、成本模型和 limitations。只验证能运行不代表样本外有效；参数搜索次数/样本内外区间应记录，用户自行判断策略价值。

## 9. 不可绕过的验收不变量

1. 未授权、错环境、错版本、行情/账户过期时，外部 placeOrder 调用数为 0。
2. 任一 logic decision 在重启、重复 bar、重复 API 请求后不会产生第二笔意图交易。
3. 券商回报不完整时保留 UNKNOWN，不补造成功、成交或取消。
4. paper 与 live 共用相同风险/账本/适配路径，区别为经验证的环境/profile/grant。
5. 本地结果标记 backtrader 时，实际调用 Cerebro/Strategy/BackBroker；paper 必须有真实 paper 账户回报证据。
6. 历史样本、编译器、策略版本不变时决策与订单可重现；在线事件回放保持决策、幂等和状态收敛一致，不要求两种市场成交相同。
7. 未通过任意代码隔离负向测试的部署形态，不能同时开放研究代码执行与可写券商连接。
