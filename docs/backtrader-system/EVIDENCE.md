# 评审证据与已知边界

检查日期：2026-10-09。区分当前只读观察、旧文档记载和新方案推断。本次没有执行回测测试套件、没有连接 Gateway、没有发送订单、没有改变 Read-Only 设置。源码结论只适用于下列本地版本及工作区状态。

用户已明确需要两种策略入口：自然语言生成策略（NL2Strategy）与上传 Python 模板策略。因此设计稿从“V1 只允许受控模板”更新为“两种来源，共用 Backtrader 运行与交易安全链”。这是新增产品要求，不是已有代码能力。上传代码在目标平台能否可靠隔离仍须 P0/P3a 做负向测试；未验证前仅开放离线回测。

后续 v1.2 复审见 [REVIEW.md](REVIEW.md)。本地 `backtrader/cerebro.py` 的 63–79、275–276、1062–1079 附近明确包含默认 preload/runonce 以及切换逻辑；`linebuffer.py` 暴露底层 buffer。由此提出认证回测使用逐事件输入的设计要求，不将“事件驱动”本身视为阻止上传代码读取预加载数据的证明。复审只增加设计与验收要求，没有运行这些故障/隔离实验。

## 1. 仓库基线

| 仓库 | 本地路径 | HEAD | 说明 |
|---|---|---|---|
| Vibe-Trading | `/Users/yaozekun/strategy_verification/vibe-trading` | `7f6908b7e1b95d971f4151bfaf2382e2ce9485cf` | main；2026-10-06 提交；本地另有未提交修改 |
| Backtrader | `/Users/yaozekun/strategy_verification/backtrader` | `b853d7c90b6721476eb5a5ea3135224e33db1f14` | master；2023-04-19 提交；版本 1.9.78.123；有新增本地工具 |

**文档创建前已有的修改：**

```text
Vibe:
 M pyproject.toml
?? PROJECT_GUIDE.html
?? uv.lock

Backtrader:
 M .gitignore
?? IBKR_SETUP.md
?? PROJECT_GUIDE.html
?? backtrader/feeds/ibapidata.py
?? examples/
?? tools/ibkr_connectivity.py
?? tools/ibkr_order_whatif.py
```

本系列交付新增 `docs/backtrader-system/` 七份文档（含后续复审记录），并在 **Vibe 的** `.gitignore` 中仅对该目录增加例外：原规则忽略全部 docs，若不处理，交接文件不会出现在常规 Git 差异中。未触碰上面列出的用户修改，未提交或推送。

已阅读上级 `/Users/yaozekun/strategy_verification/AGENTS.md`；本方案不改变 Kelly 研究材料、基准策略、凭据和数据刷新流程。执行时仍应重新检查最新 AGENTS。

## 2. 当前 venv 元数据

通过各仓库 `.venv/bin/python` 和 `importlib.metadata` 读取包版本，没有导入券商模块或连接账户。

| 项目 | Backtrader venv | Vibe venv |
|---|---|---|
| Python | 3.12.13 | 3.12.13 |
| backtrader | 1.9.78.123 | 未安装 |
| ibapi | 10.50.2 | 未安装 |
| IbPy | 未安装 | 本轮未单独检查此项 |
| ib_async | 未安装 | 未安装 |
| protobuf | 5.29.5 | 本轮未单独检查此项 |
| pandas | 未安装 | 2.3.3 |
| pytest | 未安装 | 未安装 |
| vibe-trading-ai | 不适用 | 0.1.16 |
| fastapi | 本轮未单独检查此项 | 0.142.2 |

“未安装”是当前该 venv 的 metadata 查询结果，不是项目无法安装或测试已经失败。Vibe `agent/tests` 当时发现 755 个 `test_*.py` 文件；这是文件数量，不是测试用例数量或通过数量。不能作为质量结论。

Vibe `pyproject.toml` 要求 Python >=3.11；pytest 指向 `agent/tests`，pythonpath 为 `agent`。`frontend/package.json` 要求 Node >=22.22.0，声明 `test:run=vitest run`、`build=tsc -b && vite build`。本次只读取脚本，未执行前端构建。

## 3. 本地源码事实与设计影响

下表的路径链接是仓库相对路径，换目录/版本后应重新定位；数字为本轮观察时参考行号。

| 事实 | 源码证据 | 设计影响 |
|---|---|---|
| 现有 backtest tool 要求 signal_engine.py | [backtest_tool.py](../../agent/src/tools/backtest_tool.py)，64、100–106 附近 | 先按 backend 分流，再验证对应产物；不要伪造文件应付旧校验 |
| engine 已表示 daily/options | [runner.py](../../agent/backtest/runner.py)，78、88 附近 | 新增 execution_backend；不抢占旧字段 |
| SignalEngine 批量产生 Series | [策略生成 skill](../../agent/src/skills/strategy-generate/SKILL.md) | 旧策略与原生 BT 事件策略需要定义转换语义，不能只改类名 |
| 旧引擎有信号延迟和自有撮合/导出 | [base.py](../../agent/backtest/engines/base.py)，shift 注释 328、执行流程及 2234 导出附近 | 新 BT 路径不能再次沿用 shift；独立确认报告字段/成交口径 |
| 原报告消费既有 CSV/run_card | [backtest_summary.py](../../agent/src/tools/backtest_summary.py) | 导出适配与新账本分开，不随意换列名 |
| 回测有 AST/子进程/容器执行机制 | [runner.py](../../agent/src/core/runner.py) | 需核实部署平台实际隔离强度；不能把子进程当网络隔离证明 |
| 原 LiveRunner 通过 SessionService.send_message 运行自动回合 | [live/runtime/runner.py](../../agent/src/live/runtime/runner.py)，22、108 附近 | 新确定性 BT worker 必须独立于聊天 AI 决策循环 |
| 已有 mandate、订单 gate、停止与对账 | [sdk_order_gate.py](../../agent/src/live/sdk_order_gate.py)、[mandate/model.py](../../agent/src/live/mandate/model.py)、[reconcile.py](../../agent/src/live/runtime/reconcile.py) | 复用概念/合适纯函数，逐项检查账户范围和持久化，不推定直接可用 |
| 当前 IBKR profiles 只读 | [profiles.py](../../agent/src/trading/connectors/ibkr/profiles.py)，15、26、37 附近；[local.py](../../agent/src/trading/connectors/ibkr/local.py) | 增加明确的新 BT write profile；不翻转所有旧 connector 的 readonly |
| TradingService 有既有 connector/gate 路线 | [service.py](../../agent/src/trading/service.py)，place_order 附近 | 新 paper/live 都走 BT/账本/风险路径；不能照抄绕过 mandate 的旧 paper 分支 |
| 会话事件在内存中，满队列会丢 | [events.py](../../agent/src/session/events.py)，159–169 附近 | 必须新增持久事件和补传；SSE 仅传输 |
| 已有策略存储与报告/组合页面 | [strategy_store](../../agent/src/strategy_store)、[RunDetail.tsx](../../frontend/src/pages/RunDetail.tsx)、[Reports.tsx](../../frontend/src/pages/Reports.tsx)、[Portfolio.tsx](../../frontend/src/pages/Portfolio.tsx) | 产品能力留在 Vibe；可部署版本做明确扩展 |
| BT 有 BrokerBase 扩展点 | [broker.py](../../../backtrader/backtrader/broker.py)，63、142–165 附近 | 可在 Vibe 包内实现外部 Broker 子类 |
| BT 即使空 live bar 也有 broker 通知路径 | [cerebro.py](../../../backtrader/backtrader/cerebro.py)，1360、1628 附近 | 无需把所有回报处理绑在日线 next；仍须用真实循环测试适配 |
| 内建 BackBroker 有 Market/cheat/成本处理 | [bbroker.py](../../../backtrader/backtrader/brokers/bbroker.py) | 锁定实际撮合语义和成本配置，禁止随意启用 cheat |
| 上游旧 IBBroker/IBStore 使用 IbPy | [ibbroker.py](../../../backtrader/backtrader/brokers/ibbroker.py)，30–31；[ibstore.py](../../../backtrader/backtrader/stores/ibstore.py)，33–34 | 与当前官方 ibapi 不是即插即用关系 |
| 本地已有官方 SDK 新 Feed | [ibapidata.py](../../../backtrader/backtrader/feeds/ibapidata.py)，89、245 附近 | 证明已有行情 POC，不能当作已完成可交易 Broker |

### 新增 IBAPI 文件的实际范围

`IBAPIHistoricalData` 获取历史数据；`IBAPIRealtimeData` 把价格 tick 送入 BT Feed。当前实现使用自身 SDK 客户端，实时价格可映射到 OHLC 相同的一条记录；没有完成本方案的日线 finalization、共享 broker 连接、账户账本和恢复体系。迁入 Vibe 时要按新契约重构和测试，不能只复制文件宣称“实时日线完成”。

`tools/ibkr_order_whatif.py` 通过 `whatIf=True` 做预检查，不等于普通订单成交。`IBKR_SETUP.md` 是先前操作的记录：官方 SDK 10.50.2、Gateway 10.51、live 4001、Read-Only、what-if 被 321 拒绝、AAPL 行情权限 10089/2188 等。这些**不是本轮重新连接核验的结果**，也不能据此断言当前账户在线或 paper 就绪。

## 4. 原 HTML 的证据

用户提供文件：`/Users/yaozekun/Downloads/Vibe-Trading_Backtrader_开发者项目说明书.html`，文末版本为 2026-09-28。

- 155–166 附近：Vibe 保留研究/信号/入口，Backtrader 做可选执行后端，建议先各留回测。
- 259–267：说明 SignalEngine、IBKR 只读 connector 和待集成项。
- 275–336：桥接架构、时点/目标敞口、幂等和断线不盲重发。
- 347–369：A 先执行、B 可选回测后端、C 双引擎策略；推荐 A 后评估 B/C。
- 402–415：环境、适配、paper、实盘评估阶段。

本方案采用其产品/执行分层思路；因用户进一步明确要求统一 BT 回测与运行，开发顺序调整为“原生策略+BT 历史回测先成立，再接 paper”，不先交付一个需要第二轮策略迁移的双引擎交易主链。

## 5. 本轮新增的产品说明

- Vibe `LiveRunner` 面向 agent 会话/AI 回合执行；它不因此具备按行情事件运行固定 Backtrader 策略、接单和维护券商成交状态的能力。保留 AI agent 作为规则生成/解释助手，另建 Backtrader worker 作为固定版本策略运行时。
- NL2Strategy 输入 `StrategySpec` 后由可信模板生成策略；Python 上传输入 `.py` 和声明式参数。两者冻结为策略版本，走相同 Backtrader 回测、订单风控、账本和报告。
- 用户 Python 的上线隔离实现尚未验证；将它作为 P0/P3a 门槛。若网络/文件/资源隔离和 Broker IPC 风控未通过，功能降级为只支持离线回测，不向策略进程开放 IBKR 凭据。

## 6. 外部一手资料

以下链接在本次评审中通过网页工具检索/打开；使用官方来源，具体 SDK 行为仍要以锁定版本实测为准。

- [Backtrader IBKR 文档](https://www.backtrader.com/docu/live/ib/ib/)：官方旧集成使用 IbPy，并描述 feed/store/broker 及实时账户值更新限制。
- [IBKR 下单注意事项](https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order/order-placement-considerations)：监控 error、orderStatus、openOrder、execDetails 等回调；请求可能被拒绝/取消。
- [IBKR execution 对象](https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details/the-execution-object)：订单/成交字段与关联资料。
- [IBKR 较早版本订单回报说明](https://interactivebrokers.github.io/tws-api/order_submission.html)：说明状态消息可能重复、不能只依赖每个中间状态；作为设计参考，不视为新 SDK 完整兼容证明。
- [IBKR 较早版本成交/费用说明](https://interactivebrokers.github.io/tws-api/executions_commissions.html)：说明成交、佣金与更正；查询历史窗口和具体回调名需核对安装版。
- [IBKR paper 账户限制](https://www.ibkrguides.com/clientportal/aboutpapertradingaccounts.htm)：paper 有模拟成交限制，不能替代实际市场成交验证。
- [IBKR paper 延迟行情](https://www.interactivebrokers.com/en/trading/papertrader-delayed-data.php)：未订阅数据可能延迟；paper 不等于自动获得实时权限。
- [IBKR 账户权限模型](https://www.interactivebrokers.com/docs/web-api/account-management/schema/accounts)：按产品/国家地区授权；系统和账户能力都需匹配。

许可事实来自本地 [Vibe LICENSE](../../LICENSE) 和 [Backtrader LICENSE](../../../backtrader/LICENSE) 及源码许可头。本文不提供确定的分发合规结论。

## 7. 已做与未做

**已做：** 阅读项目及上级约定、用户 HTML、关键源码和依赖配置；读取 HEAD/status/venv 元数据；查阅官方文档；撰写本交接目录；检查文档链接/结构与 Git 可见性。

**未做：** 实现 runtime/Broker、安装或更新依赖、运行 pytest/前端 build/真实回测、启动服务、访问账户、发送 paper/live 订单、修改 Gateway、提交或推送代码。

因此本轮结论是“架构可行、开发边界已定义、现有缺口有源码依据”，不是“系统已经可交易”。执行 agent 以 P0 补上实际可运行基线和契约实验。
