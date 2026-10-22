# B184–B192 离线 fail-closed 边界已加固；生产事实源和真实 PAPER 仍阻塞

- PAPER composition/start 现在拒绝缺失/错配策略版本 pin；Python worker 启动要求 daemon 报告 builtin/default seccomp 且显式请求 builtin。Importer 拒绝时区换算后跨交易日的 schedule。策略线程失败后，callback worker 仍持久化 broker callback、接收受授权管理动作，未 ACK delivery 等待显式恢复。
- 两入口的独立报告 replay 拒绝 order row 中 verifier 未知字段；NL2 的 target/accepted/requested size metadata 由独立决策时点 BackBroker 状态 + Decimal sizing 重算校验。两入口仍可能共享 Backtrader/会计算法错误，且 signal calculation 未从 report decision log 独立证明。
- B192 重新执行完整 execution/Backtrader/API/PAPER 离线组 **715 passed**；原有 recovery teardown 失败已定位为测试未区分预期的 STOPPING/unACKed callback 保留与线程泄漏，按设计补充 teardown 后合并组通过。B190 report/replay 核心组 **54 passed**，B191 Execution UI **14 passed**、build 成功。详见 `ACCEPTANCE.md`。
- seccomp 配置检查和单一攻击探针不是容器逃逸/跨平台隔离证明；session date 自洽不是官方日历或行情认证；失败策略的 native Backtrader notifications 在显式恢复前不会推进；T42 trigger failure 不是任意系统/存储 crash；T44 不证明完全独立的 verifier 信任域。生产 trusted app composition、可归因 identity/grant issuer、权威账户/行情/风险/合约 provider、真实 IBKR PAPER ack/fill/position/reconciliation 仍缺。T07/T14/T24/T37/T40/T41/T42/T43/T44 保持 PARTIAL/NOT RUN；本轮未连接 Gateway、读取账户或发送订单。

# B182–B183 补上精确 YFinance 获取与 Python upload PAPER 组合；生产事实源和真实部署仍阻塞

- 指定 `source=yfinance` 的 NL2 trusted snapshot 现在直接走 concrete Vibe YFinance loader，不再经过可回退的 provider registry；manifest 记录 host-observed 参数、cache hit/miss、时间与各层 hash，并标记来源认证、point-in-time、adjustment、broker mapping 全部 `UNVERIFIED`。这不是 licensed/attested 行情或 raw provider response 证据。
- 冻结 Python upload 现在有独立离线 PAPER builder/factory，使用真实 isolated Python event coordinator、共享 SQLite 与既有 managed session；version substitution、策略种类/政策和 DB scope mismatch 负例通过。此 factory 尚未由 app root 安装。
- 验收：定向组 62 passed 与 42 passed；完整 execution/Backtrader/API/PAPER 相关组 **687 passed、19 subtests passed**，详见 `ACCEPTANCE.md`。测试使用本地 fixture/fake transport，没有 provider 网络请求或 broker 操作。
- 仍缺 provider entitlement/authentication、point-in-time/公司行动样本、权威账户/报价/风险/合约 provider、可信 operator identity/grant provisioning、生产 app composition wiring，以及 IBKR PAPER ack/fill/position 对账。此前已有的 worker Docker 隔离回归不等于 Python 在线 PAPER 安全准入；不得据此启动 Gateway 或放开账户写入。T07/T08/T14/T24/T37/T40/T41/T42 仍按各自矩阵为 PARTIAL/NOT RUN。

# B179–B181 离线缝合已加固；可信行情与 managed recovery 仍未全验收

- NL2 回测版本现在存规范 Backtrader runtime ID 并能通过 PAPER frozen-version validator。DataManifest v1.1 绑定 currency，provider snapshots 检查完整区间、bar 可用时间和来源/调整/完成度。恢复 event 还要求同库 execution gate 为 RUNNING。定向组合 105 passed，完整 execution/Backtrader/API/PAPER 子树 673 passed，详见 `ACCEPTANCE.md`。
- 尚无真实 licensed/attested 数据与权威映射；snapshot 自洽不能证明来源真实。恢复方法仍依赖上层已完成 owner lease、grant 和 broker reconciliation；SELL/UNKNOWN/真实 Gateway crash 矩阵仍 PARTIAL。无 Gateway/account/order 操作。

# B178 备份恢复会同时暂停运行中的 journal，真实 broker reconciliation 仍缺

- 本地恢复现在使 execution gate=RECOVERING 且 RUNNING journal=PAUSED；普通 event stage/release 不可继续，exact released-event 恢复需要显式受信流程。回归 30 passed，详见 `ACCEPTANCE.md`。
- 这只闭合 SQLite 内部状态分裂风险。T34/T42 仍需异机备份/恢复演练及重启后真实 IBKR/Gateway 对账；本轮没有连接 Gateway 或账户。

# B177 已打通 Python upload API 的真实隔离 worker 与 BackBroker 回测；paper 账户和真实行情仍缺

- Docker integration 用例现在覆盖 Python preview→confirm→worker event→BackBroker fill→独立 replay→report detail sidecar。Execution UI 的定向 13 项与 build 通过；证据见 `ACCEPTANCE.md`。
- 仍无授权的 licensed/attested 行情源、point-in-time/公司行动样本、生产账号/风险 facts provider 或真实 IBKR PAPER 回报。当前测试的行情为 fixture，不提升 T07/T08/T14/T24。

# B174–B176 已补本地时间/币种约束与 NL2 API 真引擎路径；外部可信事实仍缺

- Importer 时间顺序、PAPER dispatch USD 合约门槛和 NL2 preview→confirm→真实 runner→报告读取现有离线证据，见 `ACCEPTANCE.md`。provider 时间仍是 caller-declared，确定性行情仍为 fixture。
- 仍无授权/许可 market-data provider、point-in-time/企业行动历史，亦无生产权威合约与账户/risk facts provider。没有连接 Gateway、读取账户或发订单；T07/T08/T14/T24 仍 PARTIAL/NOT RUN。

# B173 报告窗口已对齐；真实行情与 PAPER 证据仍未完成

- NL2 trusted SMA 与 Python upload 均以 manifest evaluation bars 计算年化收益；trusted report/replay 校验窗口和 bar count。定向 30 passed，合并组 92 passed，见 `ACCEPTANCE.md`。
- 同一回测入口的一致性已提高，但市场数据仍缺 licensed/attested provider、point-in-time 与调整策略证据，真实 Gateway/account/order 也未验证。T07/T09/T38 仍 PARTIAL；本轮没有 Gateway/account/order 操作。

# B172 explicit quantity 的现金不足语义已有离线证据；真实账户风控仍需验证

- Python Backtrader 测试现验证：200 股原始 proposal 在下一 bar 价格下超过模拟现金时不缩量，真实 BackBroker 返回 Margin 且不成交，报告独立重放通过。文件级 41 passed；B171/B172/importer/API/Docker 组合 75 passed。
- 该证据只证明离线 BackBroker 执行语义，不证明实际 IBKR 购买力、保证金、权限或拒单 callback。T06/T14/T24 真实账户相关项不升级；无 Gateway/account/order 操作。

# B171 manifest warmup/evaluation 日期被导入交易日约束；provider authenticity 仍缺

- importer 现拒绝不从首根 bar 开始的 warmup、未覆盖最后一根 bar 的 evaluation 及不在实际交易日的 warmup_end/evaluation_start。拒绝边界的 4 个用例与 upload/API/Docker consumers 组合通过，见 `ACCEPTANCE.md`。
- 这解决窗口声明和日历 session 不一致的本地契约缺口；市场数据仍来自 fixture/monkeypatched provider，licensed provider、point-in-time、adjustment/corporate action 与真实冻结行情证明未完成。T07/T09 PARTIAL，无真实行情下载。

# B170 启动故障会持久暂停；SELL 与真实 broker recovery 仍未完成

- managed-session start 现把启动各阶段异常汇入同一 fail-closed 路径：best-effort 持久 pause run，scope 回到 PAUSED，并保留 owner lease；晚期 hydration 后故障的回归证明零发送、不能直接 activate。execution/BT/API/PAPER 最新组 658 passed，见 `ACCEPTANCE.md` B170。
- 仍需真实 IBKR/Gateway 故障注入、跨进程 reconciliation 证据及 SELL 部分成交/费用语义；本地 journal/ledger 同时不可写时只能尽力暂停，不能证明持久状态成功。T41/T42 PARTIAL，无真实账户/订单操作。

# B169 BUY recovery 已逐笔保留 Backtrader executions；SELL 与真实 broker recovery 仍未完成

- 持仓/active-order 恢复会从 durable fill rows 重建每条 Backtrader native execution bit，并核对 ID 唯一、顺序、累计数量/均价/时间与 position snapshot cost basis。两笔 distinct fills + replayed duplicate + later final fill 的 managed failed-session 用例通过；execution/BT/API/PAPER 组 657 passed，证据见 `ACCEPTANCE.md` B169。
- 当前仍仅支持受限 BUY 恢复；SELL 部分成交成本基础、未确认费用的逐笔归属、Gateway/主机 crash 后真实 open-order reconciliation、跨进程恢复全矩阵仍未解决。T41/T42 PARTIAL，无真实账户操作。

# B168 Python upload 回测已尊重 evaluation window；真实数据与生产 PAPER 仍未验收

- Python upload runner 现在校验 manifest warmup/evaluation 覆盖范围，warmup worker 输出的订单提案由可信 host 丢弃，evaluation 区间才可提交 native BackBroker order；独立 replay 锁定相同 evaluation_start。完整 BT/API 组 123 passed，见 `ACCEPTANCE.md` B168。
- 完整 equity curve 保留 warmup bar 轨迹，但该段没有订单；真实供应商许可/来源/point-in-time 及官方市场日历仍未认证。NL2 模板当前报告范围另行定义，尚未将这一语义扩展到模板入口。T07/T09 及双入口一致性仍 PARTIAL。无 Gateway/account/order 操作。

# B167 加固 coordinator bar contract；行情 provenance 与完整容器矩阵仍未完成

- `PythonEventCoordinator` 现在在 stage 前、durable released-event 调用 worker 前，以及 shadow replay 中校验 frozen StrategySpec 的 instrument/currency/timezone/XNYS exact session open-close 与 trusted current time；不匹配会持久暂停且不调用 worker。execution + bt_runtime 597 passed，Docker worker 两项 2 passed，见 `ACCEPTANCE.md` B167。
- 这收紧了 B76/B157 的输入释放边界，但 pinned calendar 不能认证 provider entitlement、bar真实性、point-in-time/修订或 corporate action。T07/T09 仍 PARTIAL。任意 Python introspection/escape、daemon 与 forged cursor 完整矩阵仍未覆盖，T39 仍 PARTIAL。无 Gateway/account/order 操作。

# B166 transcript 已持久化，但 actor、保留策略和任意 NL 覆盖仍阻塞

- NL2 preview 现将每一轮原文/答复链写入本地 SQLite，并校验 stable turn ID 重放、父 revision、精确描述链和回测上下文。冲突或 stale turn 返回 409；不签发 token 的缺项/unsupported preview 也会记录在 transcript。定向后端 36 passed、Execution UI 13 passed、build 成功，证据见 `ACCEPTANCE.md` B166。
- transcript DB 目前按本机应用 RUNS_DIR 保存并设置 0600，但没有 actor/tenant 字段、用户删除/retention 机制或多用户隔离验收。稳定 ID 不是授权凭据；不可据此推断记录已获得生产级隐私隔离。T12 仍 PARTIAL。
- 有限 SMA/RSI parser、evidence/数值角色约束与已知 unsupported prefilter 仍无法证明任意描述中的所有 entry/exit/risk/filter、否定和撤销条件都被识别。真实 provider 的准确性和遗漏对抗集仍未验收；无 Gateway/account/order 操作。

# B165 只有 FILLED 才推进 CREATED retry；其他组合与生产 PAPER 仍阻塞

- B164 将 FILLED/CANCELLED/REJECTED 都作为 retry-bar 的可推进终态。现在只有 `FILLED` 会释放原 released event；`CANCELLED` 与 `REJECTED` 触发 managed-session 失败和持久暂停，需 fresh account/order reconciliation 后再恢复，避免迟到 fill 与策略后续决策竞争。
- 新增 fill/cancel/reject × 二次风控允许/拒绝矩阵 6 passed；cancel/reject 均保持原 event/checkpoint 且不再调用策略 worker。证据见 `ACCEPTANCE.md` B165。
- 这只是保守本地状态门，不证明 IBKR 的 callback 顺序或 broker 终态。活动订单与 CREATED intent 同场恢复、多单、多事件、Gateway/host crash、真实 account reconciliation 仍不支持；无 Gateway/account/真实订单操作。T41/T42 仍 PARTIAL。

# B164 支持唯一 CREATED intent 与一根 released-bar retry 的联合恢复；其他恢复状态与生产 PAPER 仍阻塞

- 新增受限顺序：同 run 的 committed prefix 留下一个 exact-linked CREATED BUY，同时 cursor 上唯一事件是已释放未提交的下一根 bar。fresh reconciliation、shadow replay、outbox lineage 与 native ref rebuild 后仍 PAUSED；显式 activate 先重风控派发旧 intent。B165 将推进条件收紧为 broker fill；risk deny、cancel/reject 与 unresolved/活动订单不推进策略事件。
- 该路径不证明 broker 真实终态通知、不支持多 CREATED、SELL、额外 released/staged event、unknown send、活动订单与 CREATED 并存的本次组合、Docker/host crash 或 Gateway reconciliation。T41/T42 仍 PARTIAL。
- 本轮的定向组合与相邻 managed-session/PortBroker/dispatch 验收见 `ACCEPTANCE.md` B164；所有发送只经 fake transport，无账户或 Gateway 操作。

# B163 支持保留唯一精确链接的 CREATED intent 安全停止；其他恢复状态与生产 PAPER 仍阻塞

- B162 的 CREATED BUY 现在可在显式 activate 前安全停止：先持久 PAUSE journal 并关闭 send gate，确认它仍是唯一 exact-linked CREATED 项后再停 session/runtime。该 intent 没有 broker order，不会被撤单，也不会被删除或标记为完成。
- 本轮没有重跑 B163 测试；此前 8 个状态组合的进程内 runner 通过，但独立 pytest 子进程未正常退出，见 `ACCEPTANCE.md` 的 B163 记录。没有将这次子进程行为记为业务断言失败或新通过。
- 未来恢复仍需要 fresh reconciliation、committed-prefix replay、精确 outbox link 与单独 activate。B164 仅为一个精确 CREATED BUY + 一根 released retry bar 加入联合恢复；其他 failed-bar retry 并发、多 CREATED、SELL/UNKNOWN/READY_TO_SEND、unresolved sends 与临时 native ref 跨重启仍不支持。
- 只用 fake subprocess transport 与本机 SQLite；没有连接 Gateway、读取账户或发送真实订单。T41/T42 仍 PARTIAL；生产 PAPER 仍缺 identity/root、grant provisioning、权威 facts provider、应用 runtime wiring 和 broker 端恢复证据。

# B162 支持唯一精确链接的 CREATED intent 恢复；其他恢复状态与生产 PAPER 仍阻塞

- B159 handoff 完成后，managed session 可以暂存唯一的同 run committed-proposal-linked CREATED BUY，并保持 PAUSED。只有显式 activate 后，Cerebro broker thread 才重建临时 native order 并进入现有 dispatch/二次风控。风险拒绝保持 CREATED、零发送并持久暂停。
- B162 原始路径不支持多个 CREATED、unlinked/cross-scope/version intents、SELL/未知/READY_TO_SEND/UNKNOWN 状态，也不跨重启保存临时 BT ref。B164 只增加一个 CREATED 与单根 released retry bar 的联合恢复；真实 gateway/open-order 和 partial-send 状态仍需 broker reconciliation 证明。
- 仅 fake subprocess transport 与本地测试 SQLite 证明闭环，未连接 Gateway、读取账户或发送真实 PAPER/live 订单。T41/T42 仍 PARTIAL；生产 PAPER 仍缺 identity/root、grant issuance、权威 facts provider、应用 runtime wiring 和 broker 端恢复证据。

# B161 已有持久 grant evaluator，grant 签发与身份可信根仍阻塞

- `PaperControlGrantStore` 可持久精确 action/scope/actor/auth method/tenant/有效期授权，撤销后立即失效；manager 注入该 evaluator 后会在 start factory 前检查。当前没有 grant 管理 API、可信身份 provider 或生产 app root，签发函数只能由可信部署代码调用；不能以 shared key/loopback 代替真实人身份。
- 因此此切片只提供本地 fail-closed authorization primitive。PAPER 仍默认 inert；T13 与 D102 的可归因身份/授权 provisioning 和生产装配未完成。无 Gateway/account/order 操作。

# B160 NL2 已能多轮补充受控策略缺项；任意语义识别仍缺

- preview 对已知 SMA/RSI 缺项返回静态、版本化字段清单；用户答复作为文本追加到原描述并重新运行完整校验。已知不支持条件不会被答复表单掩盖，完整 StrategySpec 与行情快照通过后才签发确认 token。具体离线证据见 `ACCEPTANCE.md B160`。
- B166 已将 turn revision/幂等和完整描述链持久到服务端；但策略参数/结论级 evidence provenance、actor 绑定及修订/删除流程尚未实现。模型和已知条件词典仍不能证明发现所有复杂表达中的附加约束。T12 保持 PARTIAL；后续为遗漏与否定/撤销表达建对抗验收，不得从静态缺项表单推断任意策略都可支持。

# B159 可恢复 committed outbox handoff；B162 已补唯一精确链接 CREATED intent 的受限恢复

- 已在 fresh reconciliation 与 shadow replay 后，幂等补全当前 run 的 PENDING 或已冻结 RESERVED outbox→ledger handoff。Journal 对 prepared intent/decision 持久保存 SHA-256；retry 时 hash、canonical JSON 与 proposal/run identity 均需匹配。该过程只创建/链接 CREATED intent；集成回归确认 cursor/result 不变且 reservation、sender 均为零。
- B162 对唯一精确链接的 CREATED BUY 添加 PAUSED staging；显式 activate 后由 Cerebro thread 通过现有 dispatch service 重新风控并生成临时 native ref。B164 只允许它与同 run 单一 released retry bar 并存，仍先发送旧 intent、待终态 callback 后才处理 bar；其他或多个 CREATED、额外 failed bar、SELL/unknown/unresolved 状态继续 fail-closed，临时 ref 不跨重启持久化。
- 当前只在 managed-session fake runtime + 本地 SQLite 组合验证，未做真实 Docker worker kill 后的 managed recovery，也没有 Gateway/IBKR 外部状态证据；T41/T42 继续 PARTIAL。

# B158 已对 committed Python event prefix 做只读 shadow replay；通用 PAPER/native state 恢复仍缺

- 新 managed-session 启动及失败 session 重建前，在 fresh reconciliation 完成、service 保持 PAUSED 时重算 committed Python event prefix。journal 验证事件/结果连续性和 hashes、输出与 outbox proposal 映射、seed 与最终 checkpoint；影子调用不会 commit 或派单，失败持久 PAUSE。
- 新 run 有独立 durable initial checkpoint。旧库已有历史事件但没有该 seed 时不可推算，恢复会 fail-closed；无已提交事件的旧 run 不需重放。
- 证据只覆盖 Python event API JSON state、fake sandbox/runtime、真实 SQLite 和 Cerebro session 接线。未实现通用的 Backtrader native indicator/order-ref shadow rebuild，也未覆盖 Docker worker/host/Gateway crash 或真实 PAPER 恢复；T41 仍 PARTIAL。

# B157 Python upload schedule 已由 pinned XNYS 代码校验

- upload API 首期只接受 `XNYS@4.13.2` / `America/New_York`。宿主从 warmup_start 到 evaluation_end 生成 schedule，逐项验证用户声明的 session，再以宿主 schedule 导入，因此错误 holiday、open/close、early-close、版本、timezone 或提供的 schedule hash 都会使 preview unsupported，且不创建 run/version。
- 该包版本可重复不等于交易所官方权威日历或其历史更正；provider entitlement、行情数据真实性/point-in-time、复权和公司行动仍缺。T07/T09 保持 PARTIAL，真实数据源验收另行完成。

## B156 已有离线 NL2 composition factory；主 app 与生产事实源仍未配置

- `build_frozen_nl2_paper_composition()` 将显式提供的冻结 NL2 版本、IBKR/PAPER config、session/risk/feed/contract/authorization collaborators 组装为 ready manager composition，并验证版本 provenance 与 scope/config account 一致。构造过程只建本地对象，callback sink 在 session router 绑定前拒收事件。
- 该 factory 仍需由可信部署 root 显式调用并经 `configure_paper_execution_app()` 安装；当前主 app 默认 manager 仍 inert。账户 grant、可归因控制授权、权威账户/行情/风险 provider 和实际合同来源都没有生产实现；不因 readiness 声明 ready 开放 PAPER。真实连接前仍需明确账户与操作授权。

## B155 已有 SQLite-backed activity SSE；端到端运行与真实部署验收仍缺

- activity sequence 与订单/回报/费用/控制记录同事务持久化，表为 append-only；SSE 从当前 trusted manager scope 拉取，使用 authenticated ticket/header 路径和 Last-Event-ID 补传。Execution 会在事件到达后刷新持久 activity snapshot。
- 当前 UI 会显示实时连接/中断重连状态，并保留 10 秒轮询；尚无活跃生产 composition 可提供 stream 样本，未验收长连接部署、背压、持续延迟、长时间断线/服务进程重启和 UI gap recovery 全链路。T29 仅部分推进、T30 仍 PARTIAL；默认 app inert，未连接 Gateway/account。

## B154 补了回测文件路径读取负例；T39 全面隔离矩阵仍缺

- 真实 Docker worker 已直接尝试读取本次回测已经存在的 `market_data.csv`、`verified_bars.json` 与 `backtest_events.sqlite3`，均不可读；launcher 参数不启用 host PID/IPC/network 或设备。
- 这仍是单一当前 Docker 配置和固定路径攻击样本。任意 Python introspection、namespace/kernel/container escape、host/daemon 故障与所有 forged cursor 组合未证明，T39 继续 PARTIAL。不得把对象图扫描和本次负例描述为形式化沙箱保证。

## B153 PAPER 活动只读投影已落地；生产组合与实时事件流未接

- 订单/成交/费用历史可在当前 manager 活动 scope 内读取并显示；账户身份字段从行项目裁剪，券商持仓无 fresh verified reconciliation snapshot 时为 unknown，风险限额为 unavailable。
- 页面目前轮询只读 API；T29/T30 的 seq 补齐 SSE、断线恢复、队列背压和展示延迟验收尚未实现。主 app 默认仍没有生产 `PaperExecutionManager` composition、可归因 operator/grant 与 account/market/risk providers，因此 API 不构成真实 PAPER 运行证据。

## B152 已新增 trusted NL2 session builder；可信 app root 和真实 provider 未配置

- `TrustedTemplatePaperSessionBuilder` 已将固定的 immutable NL2 template/run/deployment 与显式注入的授权/风险 facts provider、completed-bar feed 和 verified contract identity 组合为 managed paper session；构造期校验 IBKR/PAPER、完整策略政策、版本/artifact、run identity 和 shared SQLite。缺依赖或 mismatch fail-closed，构造不启动 IBKR SDK；离线负例和组合回归见 `ACCEPTANCE.md B152`。
- 该 builder 是 app composition root 可用的受信依赖接缝。当前 FastAPI 默认 manager 仍 inert，尚无 production root 将 version/grant/account ownership、真实 account/quote provider 和部署 feed/contract resolver 装配进去；策略版本的部署选择与可归因授权流程也未完成。真实 PAPER 前需明确账户与授权事实并独立验收；没有 Gateway/account/订单操作。T14/T16/P3b/T41/T43 仍 PARTIAL。

## B151 RSI native orders 与 trusted version pin 已离线实现；app composition/providers 仍缺

- frozen RSI 在 managed PAPER offline session 中已与 SMA 共用 host target sizing、durable journal/outbox/ledger、Cerebro/PortBroker 和 fake IBKR callback；oversold BUY 19 股、填单后 overbought SELL 19 股，最终 position=0。deployment factory 可固定一个 immutable version ID，API start 只接受回显相同 ID，错误 ID 不会调用 manager start。证据见 `ACCEPTANCE.md B151`。
- 版本 pin 只提供可信 app composition 的绑定接口；当前没有将真实 NL2 version store、deployment factory、scope、授权身份/grant 与 manager 装配起来，也没有权威账户/行情/风险 facts provider。当前默认 manager 继续 inert，未连接 Gateway/account。后续只能在这些依赖显式配置并经审核后讨论真实 PAPER；T14/T16/P3b/T41/T43 不升级。

## B149 Python long-only PAPER bridge 已可接受经授权 SELL；真实账户持仓归属仍未验收

- 先前 `PythonPaperEventBridge` 在风险/授权 handoff 之前固定拒绝 SELL，使 Python long-only 策略无法按信号退出。现在 BUY/SELL 均须通过 durable host handoff，并逐项核对 intent 后才进入 native Backtrader order；真实 Cerebro + fake transport 覆盖已授权 2 股卖出成交，以及只有 1 股策略持仓时申请卖 2 股的零 reservation/零 send 失败路径。
- 这只补齐离线 Python bridge 的方向路由：host risk facts、策略归属与生产 provider 仍为注入依赖；不代表真实 IBKR 持仓的 SELL 验收。当前更大的双入口缺口仍是 B148：可信 NL2 template / `target_fraction` 没有 managed PAPER session；T14 / T16 / P3b 继续 PARTIAL。

## B150 NL2 PAPER 的 managed native order 仅以 SMA 完成端到端验证

- `TrustedTemplateEventCoordinator` 与 `ManagedPythonPaperSession` 已组合 frozen SMA `target_fraction`，通过同一 journal/outbox/ledger/PortBroker/Cerebro 与 fake broker callback；host sizing 重用 Decimal 目标比例函数，并由普通 handoff 再做授权风险校验。模板来源/hash/compiler/engine 均在运行前校验，模板源码不进入 Python sandbox。RSI Wilder checkpoint 和 entry/exit transition 已有定向状态机测试，但还没有 RSI native PAPER order 组合证据。
- 目前没有生产 deployment composition/API 将用户确认的 frozen version 绑定到 session，也没有可信生产 market/risk facts provider。README 中的 NL2 confirmed backtest version 和 offline fake-runtime paper test，尚不是 UI→部署→IBKR PAPER 的完整链路。生产接入前需补 SMA/RSI 模板版本的跨重启 checkpoint 与活动单恢复、quote/account 与预算事实 provider、主应用 manager wiring 和可归因 grant 流程。
- 这是可离线推进的缺口，不依赖用户提供账户参数；当前无 paper 用户配置，且没有 Gateway/account/真实订单操作。T14、T16、P3b、T41/T43 仍 PARTIAL。

## B147 回测金额有独立手算证据；真实供应商数据及全报告口径仍未闭合

- 新的 12-bar 真实 Cerebro/BackBroker synthetic fixture 覆盖两次 BUY、一笔 SELL 平仓和末尾持仓：买入以 $10.05 high cap 成交；卖出由 $6.90 开盘按 10% slip 得到 $6.21，再由 $6.30 low cap；最后买入以 $13.20 high cap 成交。手算 closed net P&L=-$191.7615、final cash=$221.6305、末值=$749.6305、最大回撤=$291.995；subprocess replay 一致，篡改 total_fees 会被拒绝。
- 这提高 T10 对执行、费用、已实现交易、期末估值与本 fixture SMA 交叉时点的独立验证，但不认证数据来源/许可/点时可用性、提供真实行情或冻结完整统一报告统计口径。T09/T10 仍 PARTIAL，不能把合成验证称为真实市场回测。

## B146 已覆盖“模拟外部接受、ACK 前进程崩溃”；真实 Broker 对账仍缺

- 离线假 Broker 在接受确定性 attempt/client order ID 后将记录 fsync 到独立文件；策略进程随后被 SIGKILL，ledger 保留 `SEND_STARTED`，重启 dispatch 只进入 RECONCILE 且不二次调用 sender，假 Broker 接受记录仍恰一条。
- 外部接受账本只是故障注入替身，不能说明真实 Gateway 是否接受、IBKR 网络是否部分写入、callback/ACK 是否丢失或真实账户上如何唯一归属。未知结果需保持暂停并走受控对账；T42 继续 PARTIAL。

## B145 已覆盖 SEND_STARTED 提交后、transport 调用前的进程硬杀；T42 仍缺外部 Broker 故障证明

- dispatch service 先 durable commit `SEND_STARTED`，之后才可调用注入 sender。真实 SQLite 子进程测试在 commit 后精确暂停并被 SIGKILL；重启后状态保留，重复 dispatch 不调用风险 provider 或 sender，只返回 `RECONCILE/send_outcome_unresolved`。
- 这避免系统在该本地 crash window 中盲目重发，但 SEND_STARTED 可能对应“未调用 broker”“部分网络写入”或“broker 已接受而 ACK 丢失”；当前没有真实 Gateway/外部模拟器来判定这些状态。仍需明确 reconciliation 输入、未知状态人工处理和跨外部 broker 的运行证据；不能把本地 no-retry 转成 exactly-once 声明。T42 继续 PARTIAL。

## B144 验证 Docker worker deadline 会清理忽略 TERM 的后代；T37 仍 PARTIAL

- 更新后的真实 Docker 用例会等策略 fork 子进程、安装 SIGTERM ignore handler 并写 ready marker 后，再杀死宿主 supervisor；观察到容器内 2 秒期限触发及 `--rm` 删除容器。colima-vibe-execution 1 passed、launcher unit 15 passed。
- 仅证明当前容器配置下这一类 descendant timeout 清理；不证明 daemon/host/runtime 崩溃、跨平台隔离、网络/内核逃逸或多资源联合 OOM 清理。T37 的完整负向攻击与部署形态验收仍未完成，上传 Python 不能因此获得 PAPER 准入。

## B143 已联合验收受限 failed-session recovery 的 partial BUY + pending inbox；真实故障矩阵仍缺

- 一条真实 Cerebro/SQLite + fake IBKR 流程验证 exact-linked partial BUY/ref=77 与未 ACK 重复累计 execution 在失败会话恢复中联合存在：durable ledger 不重复记 fill，pending inbox 可在替换 native ref 后重放并 ACK；PAUSED 时的后续唯一 fill 完成旧单，独立 activate 后原 released event 仅提交一次。执行/API 组 181 passed，详见 `ACCEPTANCE.md B143`。
- 该测试在 service 已 PAUSED 时向真实 Cerebro/bridge 注入边界失败，以满足失败 session 的恢复前置条件；它没有复现“真实 Docker worker timeout 同时伴随活动单”，也未覆盖 callback worker fatal、SDK/Gateway 断连、daemon/host crash 或跨 epoch 恢复。账户快照/IBKR runtime 仍为 fake；生产身份/provider、SELL 与其它订单状态仍未验收。T41/T43 保持 PARTIAL，真实 PAPER 前置不变。

## B142 的 failed-session recovery 联合缺口已由 B143 补齐；真实故障矩阵仍缺

- manager 现有独立 `recover` grant 与 POST route 能在原 scope/lease 上恢复健康 callback worker 旁边的 dead strategy/Cerebro session；fresh reqIds/snapshot、原事件 identity、native broker shell 重建后保持 PAUSED，activate 仍独立。恢复中的 reconciliation 错误回到 PAUSED，lease 保留且 allocator gate 关闭。
- 截至 B142，端到端策略失败测试只含无活动订单/无 pending inbox；B143 已在单次 Cerebro/SQLite + fake IBKR 流程中联合验证 exact-linked partial BUY/native ref、未 ACK inbox rebind/replay 与原 event 重试。它只覆盖测试注入的 PAUSED bridge 边界失败，不等于真实 Docker worker timeout 与活动订单同场景。callback worker fatal、SDK/Gateway 断连或 daemon/host crash 不可通过此同进程入口恢复；生产应用身份授权和真实 PAPER 事实/provider 也未完成。
- 下一步优先补 Docker worker timeout + active order 同场景以及 callback 入站/快照切换和失败中途重试边界；再补 callback worker fatal、SDK/Gateway/host crash 与进程 epoch recovery。T41/T43 继续 PARTIAL。

## B139 stop 未完成时的 exact-intent cancel 仍可用；完整 failed-session recovery 仍不完整

- 对 failed managed session 的活动订单调用 safe stop 会返回 false；session 先发现 active order 而不调用 service.stop，所以 manager phase=STOPPING，但 durable service 仍为 PAUSED、callback runtime/lease 保持可用。B139 允许独立授权的 exact-intent cancel 在该保留 manager slot 上进行，回报可使 ledger 订单转为 CANCELLED。
- 如果故障 session 有未 ACK native inbox 或缺少可恢复的 Backtrader state，stop 仍不能完成，owner lease 与 manager slot 必须继续保留；目前没有安全替换 Cerebro/session、恢复 cursor/订单并完成 stop 的路径。若 durable service 本身已进入 STOPPING，runtime cancel 仍拒绝发送。T41/T43 继续 PARTIAL。

## B138 拒绝 dead strategy session rearm；后由 B142 接入受限 recovery

- `ManagedPythonPaperSession.activate()` 现在对已有策略线程异常 fail-closed；B137 实际 Docker 超时回归确认即使 attributable principal 有 activate grant，service 仍 PAUSED 且不发送策略订单。
- B138 当时 manager 仍持有 failed session 和 lease，原地 `activate()` 不可用。B142 后 `recover` grant 已提供受限同进程 recovery，复核原事件、fresh nextValidId/snapshot、callback quiescence、支持的 BUY/native ref 与 durable inbox，再构造新 session 并返回 PAUSED。它只接受策略/Cerebro 线程失败且 callback worker 健康；B143 已验证 PAUSED bridge 注入失败下的活动 BUY+pending inbox 联合 recovery；Docker worker timeout 同时活动单、callback-worker fatal、SDK/Gateway/host 崩溃恢复仍未验证。旧 `PortBroker` 通知/state 的通用迁移不被此 slice 支持；不支持的费用和订单状态仍 fail-closed。T41/T43 继续 PARTIAL。

## B137 真实 worker timeout 已暂停 session 并保留 callback/管理通道；真实 SDK/Gateway 与 crash 恢复证据仍缺

- 真实 Docker 无限循环经 SandboxTimeout 到达 managed session 后，service 与 SQLite run 均持久 PAUSED；released event 未提交、cursor 不前进、没有 result/outbox/send。B137 的 fake IBKR runtime 在 PAUSED 状态继续把部分成交写入 ledger；非归因撤单被拒，可归因 principal 的 exact-intent cancel 发到唯一 durable broker binding，取消回报收敛为 CANCELLED，见 `ACCEPTANCE.md B137`。
- 这证明 session 失败后当前进程内 callback/授权管理路径仍工作，不代表真实 SDK/Gateway 的 fill、cancel 和 reconciliation 行为。daemon/host crash 或恢复、生产管理身份/app wiring 仍缺。T43 仍 PARTIAL，真实 PAPER 前置不变。

## B132 callback worker fatal 已联动暂停 session 与账户 scope；T43 阻塞期间服务连续性仍待验

- session 组合时保留并链式调用 runtime 已有 callback failure handler，同时尽力暂停该 `run_id` 的 SQLite execution journal、标记本地 session failure；管理状态仍只呈现固定 health code。真实后台 worker + fake account mismatch 回归证明 journal 与 execution scope 都进入 PAUSED。
- journal pause 为 best-effort；持久层不可用时只保证进程内故障状态可见。该负例不证明 strategy/Cerebro 挂起期间正常 fill callback 仍可被消费、授权撤单成功、控制请求可调度或恢复/对账完成。T43 继续 PARTIAL，真实 PAPER 前置不变。详见 `ACCEPTANCE.md B132`。

## B131 已验证单样本 CPU quota 节流；隔离矩阵与 PAPER composition 仍缺

- 当前 Docker context 下半核 busy loop 的 cgroup CPU 用量满足 2 秒墙钟/不超过 1.75 CPU 秒；32 MiB memory pressure 与 PID=4 超额拒绝也有真实探针，见 `ACCEPTANCE.md B129–B131`。
- 这些是单机/单配置样本；CPU fairness 长测、更多 OOM/daemon 故障、跨平台容器限制以及 Python 策略 PAPER production composition 仍未验收。T37 继续 PARTIAL；不能据此开放 Python PAPER。

## B129 已验证 Docker cgroup 限额设置与 PID 上限；资源超限 kill/reap 矩阵仍缺

- 真实容器读取到 memory/CPU/PID cgroup v2 限值与 launcher 配置一致；PID=4 时派生进程超额被拒绝。文件/loopback 网络探针仍保留，证据见 `ACCEPTANCE.md B129`。
- 没有执行 memory OOM 与 CPU 饱和的真实策略 worker 测试，也没有覆盖每种资源强杀后的 daemon 回收/报告 fail-closed 路径。T37 保持 PARTIAL；Python PAPER 不开放。

## B127 只完成带精确 outbox 来源的 BUY 活动单 session 恢复；完整冷启动仍未验收

- `ManagedPythonPaperSession` 仅重建 ledger 为 `ACKNOWLEDGED` / `PARTIALLY_FILLED` 的 BUY，并要求同一个 RUNNING journal run 内存在 exact committed event/result 与 prepared/link handoff；同时验证策略版本/deployment、intent、binding、合约/feed、逐笔 durable fills、账户 open-order/position snapshot 和无相关 fee facts。其它状态、缺 link、仅由另一 run 链接、pending inbox/callback/outbox 和 scope 不一致保持 PAUSED 并保留 lease。无旧意图重发的 fake runtime/reopened SQLite 证据见 `ACCEPTANCE.md B127`。
- **T41/T42 继续 PARTIAL。** 仍不支持 SELL/PnL 重建、cancel/reject/unknown/uncertain-send 活动单恢复；没有真实 Gateway/进程崩溃故障矩阵，未证明生产 callback spool 的所有故障窗口或交易后续决策。错误策略线程后的 callback 连续性和授权撤单也仍未验收。
- 本轮测试同时发现：已有持仓且当前 live feed 尚无 bar 时，Backtrader strategy 收尾会因 broker 无法获得当前市值而失败；这影响无行情期间的 clean stop。此切片只记录边界，没有加入估值/清理策略。不得以 fake runtime 测试替代真实 PAPER 验收。

## B126 仅完成 BUY native Order 局部恢复原语；session 冷启动仍 fail-closed

- `PortBroker.restore_reconciled_order()` 能在已 hydration 的 reconciled PAPER account snapshot 之上重建严格 BUY 状态的 Backtrader Order/ref/execution bits；只注册本地 order、不调用 send port。broker-local ref allocator 会在最大恢复 ref 后继续分配。SELL aggregate fill、cancel/reject/unknown、缺少或不匹配的持仓快照均拒绝。
- 在 B126 时，此 primitive 尚未与 ledger binding、open-order reconciliation、pending callback replay、Python event provenance 和 `ManagedPythonPaperSession` startup 顺序连接。B127 后仅接入严格 BUY 活动单子集；其它活动状态仍 fail-closed。此节保留历史边界，当前增量见上方 B127；T41/T42 不升级。

## B125 加入策略线程异常健康码与尽力持久暂停；T43 服务连续性仍缺证据

- 未捕获的 managed Python/Cerebro session 异常会尽力将 run journal 持久置 PAUSED，并在 execution service 仍 RUNNING 时暂停该 scope；manager/status 只展示固定 `managed_python_session_failed`，不泄露异常文本。
- 这是进程内错误可见性和 fail-closed 改善。B132 后，callback worker 自身遇到 fatal 也会尽力暂停其 managed session run journal 并调用原 runtime scope pause；如果 SQLite 写入失败，journal 持久暂停仍不能保证。策略线程阻塞时 callback pump 正常接收成交、授权撤单及事件告警尚未验证，T43 仍 PARTIAL。
- T41/T42 cold start 中的 native Backtrader order/ref/indicator/state 重建还没有实现；现有流程仍拒绝无法恢复的 active order、pending callback/inbox/outbox 并保持恢复失败。后续离线恢复证据不能替代真实 PAPER 验收。B125 回归见 `ACCEPTANCE.md`。

## B113 preview authorization SQLite 已限制活动 payload；物理磁盘配额仍未建立

- 单个授权 payload 最大 80 MiB，活动未过期/未消费 rows 合计最大 256 MiB；事务内按 UTF-8 bytes 计算，超过时拒绝 preview，当前 run/version 不受影响。消费会清空 payload，下一次 preview 清理 expired/consumed rows。
- 此额度只限制当前 pending payload，不限制 SQLite 历史文件/free pages 或长期 run artifacts 总容量；部署仍需 disk quota、数据库备份/清理和保留政策。API 负例通过，但没有物理磁盘耗尽或高并发压测。

## B112 Python upload 已从确认 token 读取精确行情快照；本机 DB 防篡改不在此证据内

- Python upload preview token 现在绑定策略/成本/worker identity，并在本地 SQLite 保留 CSV、metadata 与 schedule。create 不使用确认时客户端重传的市场字段，先检查 snapshot schema/hash、重建 importer input 并复核 preview review hash；包括 snapshot payload hash 重算在内的篡改回归均在 run/version 创建前返回 409。一次性 token 被消费时清空 snapshot/spec payload；新 preview 也会清理已过期或消费的授权行。NL2 与 Python upload 现都以 token 内冻结行情数据执行。
- 这只建立同一主机的短期授权/复现边界；本地可写数据库攻击者仍可改写 token row 与摘要，没有独立 MAC 密钥/签名快照存储。普通上传用户不能直接提交 SQLite 行；数据库权限、快照保留/清理和大 payload 运维还需纳入部署控制。provider label、历史 point-in-time、许可、公司行动/复权与真实 broker contract mapping 仍不认证。

## B110 NL2 回测现在绑定精确行情快照；来源真实性仍未证明

- Preview 抓取一次 provider bars，经现有 XNYS completed-bar importer 校验，data/manifest hashes 纳入 review hash；create 从 token 取回同一 CSV/session schedule，runner 重验 hash 并写入 artifacts。确认阶段不会重新请求行情。
- 快照固定只保证同一输入可复核，不认证 provider 自述、许可、point-in-time availability、复权/公司行动策略或 caller-supplied symbol 与真实 broker contract 的映射。B110 用本地 deterministic fixture 验证。
- 后续要取得真实数据真实性证据，仍需经授权的 provider、其许可/历史口径和可核验的标的 mapping；本轮未访问真实行情服务。

## B109 PAPER manager app composition replacement 已拒绝活动 slot

- 当前 app manager 持有 active status 时，composition setter 不会把引用替换成另一 manager，避免旧 runtime/lease 脱离该 app lifespan 的 shutdown 路径。空 slot 可替换；同一 pair 重复安装幂等。
- 默认 app 仍使用惰性、无 factory 的 manager；没有 production composition root、可归因 identity/grant、account/reconciliation、market/contract 或 risk provider。T40/T43/P3a/PAPER 不因该保护升级。

# 当前阻塞与外部前置条件

更新：2026-10-22（Asia/Shanghai）

## B124 修复同一进程重复恢复时保留 RUNNING 派单闸

- `PaperExecutionService.begin_recovery()` 不再因“已有 lease + durable RUNNING”返回 RUNNING；现有 runtime 重入 recovery 会先写 RECOVERING，完成最新 reconciliation check 后回到 PAUSED，仍需显式 activate。定向与完整离线验收见 `ACCEPTANCE.md B124`。
- 这只修复 recovery 状态语义。B73/B98 等路径仍对含活动 native order/ref、待处理 callback 或未决 outbox 的新进程 attach fail-closed；要通过冷启动恢复，需实现并演练这些 Backtrader/ledger 关联的持久重建，当前 T41/T42/T43 保持 PARTIAL。

## B123 的可落地 app composition 接缝不等于 production PAPER 配置

- FastAPI lifespan 现在能安装显式的 `PaperExecutionComposition`；默认仍创建无 runtime factory、readiness 缺项的 inert manager。注册不会连接 Gateway。
- 仍缺可信身份 provider、可归因 grant、真实账户/行情/合约/风险 facts providers 和部署级 factory 配置；T40/P3a/PAPER 不升级。需要先实现并审阅这些可信依赖，再由明确批准的 PAPER 参数推动任何连接。
- B123 另修 T39 timestamp 子类 fail-stop 与 T44 报告验证/响应快照一致性；详细离线证据见 `ACCEPTANCE.md B123`。这两项不改变上述账户边界。

## B122 report sidecar 可见时持续强制摘要核对；同机共同改写仍未防护

- run detail 不再单独信任 config marker：sidecar 存在则必须核对；未知 schema marker 也拒绝。合法旧报告只有在没有 marker 和 sidecar 时才会标记 `unverified_legacy`。
- T44 仍 PARTIAL：报告、sidecar、config 都在本机可写信任域，不能防止同主体共同删除或重建三者；仍缺签名密钥隔离、WORM/远端可信存储或其他独立复算服务的部署边界。证据见 `ACCEPTANCE.md B122`。

## B119 报告完整性已在读取时复验；sidecar 仍处于同一信任域

- 新格式报告读取会核对同目录 sidecar 的 schema/path/size/SHA-256 和 JSON 对象结构，任何不符均返回 409；合法 legacy 报告标为 `unverified_legacy`，损坏 legacy JSON 也 fail-closed。
- 报告与 sidecar 可由同一主机主体共同修改，因此不是数字签名、防篡改存储或独立重放信任域。T44 仍 PARTIAL；完整离线回归见 `ACCEPTANCE.md B119`。

## B121 补齐 PAPER 管理路由与 feed timestamp fail-stop；生产身份与 composition 仍缺

- T39：非法 timestamp 类型、naive 值或异常 tzinfo 现在会持久 PAUSED；worker=0、cursor 不前移，合法 retry 仍拒绝。全面 Python 内省/容器逃逸矩阵仍未通过。
- T40：HTTP start/activate/cancel/stop 接入 manager 授权；PAPER start 需要 trusted app scope 与 attributable `start` grant，撤单仅收 local intent ID。当前 `require_auth` 返回共享 key/loopback principal 时无法归因，都会被 manager 拒绝；app 没有 production composition、真实 scope/provider，也没有 identity provider，故无法真实启动。
- **账户边界：** 默认 app 仍 inert，本切片未连接 Gateway、读取账户或发送订单。完整离线回归见 `ACCEPTANCE.md B121`。

## B114/B115 补强 NL2 周期来源与 PAPER readiness 诊断；总体门槛仍未通过

- B114 要求模型周期附带原文 fast/slow 角色引文，并扩展已知中英文 unsupported 条件预检。B160 后，受控字段缺项已有 UI 多轮补充；仍不能证明任意自然语言意图完整抽取，服务端 durable transcript/逐轮来源、非数值条件遗漏矩阵与真实 provider 仍缺，T12 继续 PARTIAL。
- B115 消除 app status readiness 与 manager 启动 gate 的对象漂移；它仍只对齐同一份声明，不认证声明中 provider 的真实性/质量。主 app 没有 production composition、授权/账户/行情/风险 provider；没有 Gateway/账户连接，也没有 PAPER 订单。
- 完整离线回归见 `ACCEPTANCE.md B114/B115`。这些切片不改变外部 PAPER 授权或真实行情前置条件。

## B116 已持久化 commission callback 证据；最终费用与报告更正仍需对账

- Fee facts 按 scope/execId 追加，exact replay 去重；未匹配成交先待关联，金额变化会留下新版本并标 `PENDING_REVIEW`。金额始终 `REPORTED_UNCONFIRMED`，没有覆盖既有值。
- 该证据不证明 IBKR 最终结算金额，不更新 Backtrader cumulative commission/report revision，也未接真实 Gateway/account reconciliation。T22 仍 PARTIAL；测试命令见 `ACCEPTANCE.md B116`。

## B117 非法 Backtest host cursor 已锁存暂停；完整前视攻击面仍未验收

- Coordinator 对非整数/布尔/非正/越界序号在 worker 调用前持久 PAUSED；worker=0、journal cursor 不前移，之后的合法重试仍拒绝。
- T04/T39 仍 PARTIAL：当前矩阵没有覆盖所有合法范围内 forged cursor、Backtrader introspection/底层对象访问与容器逃逸；本切片不认证任意 Python 隔离。完整回归见 `ACCEPTANCE.md B117`。

## B118 SQLite migration 有成功后恢复旧版证据；部署级恢复能力仍未验收

- 成功 migration 路径验证了 user_version/schema commit、pre-migration backup integrity/hash 与恢复旧 schema；恢复会将账户 scope 的 control gate 设为 `RECOVERING`。
- T34 仍 PARTIAL：没有异机/定期备份、加密密钥、保留策略、停机/滚动升级恢复 runbook 或恢复后的真实 broker reconciliation。回归见 `ACCEPTANCE.md B118`。

## B107 已修复跨 FastAPI app 的 PAPER manager/readiness 全局共享路径

- status/pause routes 从 `request.app` 读取 manager/readiness，shutdown 使用当前 lifespan app 的 manager；app composition setter 可绑定同 app 的对象。双 app route/readiness 隔离和独立 shutdown 见 `ACCEPTANCE.md B107`。
- 主 app 仍未注册可信 production composition root/provider，默认 manager inert；这个切片不解除任何 PAPER readiness 前置条件，也不产生 Gateway/PAPER 验收。

## B108 拒单映射已限于持久绑定；真实 broker 回调与完整分类仍阻塞

- 官方 `EWrapper.error()` 五参数 payload 已先进入 durable spool。Pump 忽略 2104/2106/2107/2108/2158 正常 farm 状态；1100/1101/1102/1300 要求暂停、重连与 reconciliation。error 201 只在 request ID 对应精确持久 order binding、scope 一致、零成交且非终态时落为 REJECTED，并释放剩余 reservation。
- 未映射、scope 不符、部分成交、终态冲突和未知/畸形 code 仍 fail-stop。离线测试不证明真实 Gateway 错误码/回调关联及 broker-side 状态收敛。
- T23 仍 PARTIAL；没有 Gateway/PAPER 连接。验收见 `ACCEPTANCE.md B108`、`B110`。

## B105 Python upload static policy 仅约束应用 API

- Preview/create shared preparation、resume 装载 frozen version 前、online Python PAPER coordinator 和 managed session composition 共用 AST 检查；仅接受一组标准库 imports 和唯一、恰好两个位置参数的同步 `on_event` 入口，拒绝 AST 可见的直接动态执行/反射调用与 dunder 访问。别名/字符串形式不作覆盖保证。
- 这不证明任意 Python 不可逃逸。通用 offline event worker 与 durable journal 不调用该 policy，底层 Docker worker 仍会执行 adversarial source 以支持隔离攻击探针；经 preview/create/resume 与 managed Python PAPER application path 的 source 才经过 policy。
- Paper/deployment 不可依赖此静态检查作为代码隔离准入。实际 OS/network/filesystem/descendant confinement 和 release-image 验收仍需独立覆盖。

## B104 已拒绝常见 LIVE/未知 endpoint；broker-side PAPER 身份仍未证明

- 配置只接受显式 IB Gateway PAPER 4002 或 TWS PAPER 7497 profile、loopback host 与单一 `DU` 数字账户；`4001/7496`、custom/unknown port、远程 host 和 profile/port mismatch 在创建本地账本前拒绝。Managed runtime 构造层也限制相同端口/host，避免绕过 factory。
- `managedAccounts` 必须精确匹配一个目标 account；额外、重复或格式损坏的账户项令 callback worker fail-stop，不完成 handshake、不请求 snapshot。测试见 `ACCEPTANCE.md B104`。
- **不能据此认为安全连线已通过。** IBKR 官方默认 PAPER/LIVE 端口可在 TWS 设置中改写；本轮选择不支持自定义端口，因此保留不同 deployment 的限制。配置与 `DU` 编号格式都是本机 fail-closed 规则，不是来自 Gateway 的环境认证；app 仍无可信生产 composition、Read-Only 保持未触碰、无用户 paper 账号/合约/预算/风控授权，未连接 Gateway。T14 保持 PARTIAL。

## B103 trusted SMA/RSI execution report 已增加独立复算；信号与数据真实性仍未证明

- Provider runner 的首轮报告现在必须通过分离本地进程中的 BackBroker replay，之后才发布 `execution_report.json`；订单/成交/accounting mismatch 失败关闭。SMA 与 RSI 成功和失败路径见 `ACCEPTANCE.md B103`。
- Replay 重用首轮策略捕获的 decision log，因此不证明独立 SMA/RSI signal calculation；主进程和 replay 共享同一 Backtrader 依赖与本机可信边界，也不提供签名 artifact、真实供应商授权/point-in-time 数据或账户事实。上述证据不足以升级 T09/T10/T13 或 PAPER 验收状态。
- Python-upload 独立复算兼容组已在 B103 复跑通过；仍未解决的是独立信号算法、独立部署/签名 artifact 和真实数据来源边界。

## B102 PAPER app composition 尚未接入生产能力；启动闸已 fail-closed

- `PaperExecutionManager.start()` 已要求完整 composition readiness 后才调用 runtime factory；缺一项都不创建 runtime/SDK child/数据库或 lease。FastAPI state 注册 composition 时把同一 readiness 实例绑定到 manager 与 status，manager slot active 时拒绝更换。
- 当前 app 默认 manager 仍无 factory，readiness 全缺；没有可信 session/facts/grant/data/contract provider 的 production composition。该静态 readiness 只证明受信任 app root 声明装配了角色，不证明账户/行情事实真实性，也不替代 `activate()` 与逐单风险检查。
- 不允许把某一测试构造的全 true readiness 接入默认 app。继续推进离线 composition 验证、trusted template 报告复算、数据/provider seam；真实 PAPER 仍需账户/权限/风险等用户确认和安全门槛。

## B101 SELL 本地股数预留已实现；真实 PAPER 仍需可信持仓归属与全部挂单事实

- B101 在单一 SQLite ledger 中新增独立 `share_reservation`：reservation 与最终风险复算/唯一 `SEND_STARTED` 一致性检查已接入；共享账本并发、部分成交、取消/拒绝、UNKNOWN 重启、迟到成交和旧活动 SELL 迁移冻结均有离线用例。外部事实源的挂单股数与本地 reservation 现在相加，两次风险检查都覆盖；时间戳与成交相等时快照按过期处理。
- **尚不能用于真实 PAPER 派单。** 生产 app composition 尚无可信策略持仓归属、IBKR open-order share facts 与稳定的 position snapshot watermark；外部挂单的语义仍由注入 facts provider 提供，当前没有生产 provider。多 ledger/跨主机 reservation 也未覆盖。因此 T16 保持 PARTIAL，Paper composition/readiness 不得因此切片开放。
- `D-016` 已修订以反映离线切片：BUY/SELL 都可通过注入事实源进入同一离线 dispatch service；生产运行仍必须先有可信账户/策略持仓归属和完整 open-order reconciliation。没有连接 Gateway、读取真实账户或发送订单。

## B99 app 关闭会尝试 PAPER 安全关停，宿主崩溃恢复演练仍缺

- FastAPI lifespan 已连接 manager 内部的 safe-stop 路径：不需要伪造人类身份，不取消或清仓；runtime 自己检查并执行 quiescence。只有 STOPPED + lease released 得到确认才清除 slot；其他情况保留 entry/状态并给出不含敏感 scope 的错误类别。
- lifespan 只是有序关闭，不解决宿主突然崩溃、机器断电、子进程 orphan、Gateway 重启、跨进程 Backtrader native order/ref 重建或 broker snapshot replay。未静默并退出进程后的恢复仍需 operator 处理和真实 reconciliation 证明。
- P4/T43 仍 PARTIAL；无真实 PAPER runtime 注入、Gateway/account 状态或订单。命令见 `ACCEPTANCE.md B99`。

## B98 runtime 与 managed Python session 可由 manager 组合，app/provider 接线仍缺

- `ManagedPythonPaperDeploymentFactory` 已把未启动 IBKR/PAPER runtime 和共享数据库的 frozen Python/Cerebro session 组合成 manager 可管理对象；manager 的显式 start 后到 PAUSED、授权 controls 和恢复失败保留 owner lease 均有离线组合回归。RECOVERING 下 stop 会走 runtime quiescence，测试退出时不遗留 callback worker。
- 尚未注册到 FastAPI app root/state 或生产启动配置；runtime/session builder 都需要可信应用注入。可归因人类身份/grant、PAPER account profile、真实账户/市场 facts、风险规则、contract/conId map 与真实数据能力没有接齐。Readiness 必须继续保持未就绪，不能由这个 factory 的存在推断账户或连接安全。
- T40/T43、P3a 仍 PARTIAL；没有 Gateway、账户活动或订单。测试命令与结果见 `ACCEPTANCE.md B98`。

## B97 T04 已覆盖 journal 重开后的前缀扰动，其他安全矩阵仍缺

- 两份独立 frozen run 在相同前三根 bar 后关闭并重开 SQLite journal/coordinator，再经真实 Docker worker 与 Cerebro 恢复不同未来后缀。重放已提交 prefix 不重复调用 worker；prefix checkpoint、proposal、决策、orders/fills 一致，full-run 终值可随 suffix 变化；不同 run config hash 使 journal 持久 PAUSED。
- T04 仍 PARTIAL：还未覆盖完整伪造 cursor、Python 内省、容器逃逸组合。测试只使用离线合成数据，不认证行情来源或 PAPER。命令见 `ACCEPTANCE.md B97a`。
- Execution UI 现在会解释 runtime composition 的五个缺项，但这是既有 readiness 的展示，不代表相应事实源/身份服务已注册或可以连接账户。真实账户、行情、风险与管理身份阻塞不变。

## B96 RSI NL2 已有严格 schema，意图完整性和 provider 验收仍缺

- RSI parser 现在能把非固定句式提取成受限 JSON intent，并要求每个数值参数附带可在用户原文逐字匹配的证据；缺值或模型未配置时不签发 preview token。创建阶段仍复用已审阅的冻结 spec。
- evidence 只证明 period/阈值数字来自原文，不证明模型未漏掉其他过滤/持仓意图。用户仍需核对原文和规则卡；完整遗漏检测与真实 ChatLLM provider 端到端验收未完成，未实现跨请求多轮澄清状态。provider 数据真实性和 PAPER 缺口保持不变。
- 专项命令与结果见 `ACCEPTANCE.md B96`；无 Gateway、账户或市场数据活动。

## B95 RSI 模板已进离线可信 runner，NL2/行情与 PAPER 仍不完整

- `rsi_threshold/1.0` 现有冻结算法与边界、真实 Cerebro/BackBroker、provider runner、preview/create spec 绑定和 Execution 规则卡；参数必须由用户明确给出，不使用 14/30/70 默认值。
- B96 已增加 strict model intent schema 和参数原文 quote 校验；B95 中“只接受固定句式”的状态已由 B96 部分解决。仍缺真实 ChatLLM provider 验收、多轮澄清以及意图漏项证明。RSI 的 provider runner 仅用本地 fixture 覆盖，provider source/时点/调整仍未认证。不存在 RSI 的 Paper/账户证据；app-managed composition 与账户/行情/风险事实源仍按后续阻塞执行。
- 后端与前端验收详见 `ACCEPTANCE.md B95`。未连接 Gateway/账户、未访问行情服务、未发送订单。

## B94 策略失败可定位，contract 与 UI 覆盖仍需扩展

- 策略 worker 的运行错误现在只携带白名单异常类型、源码行号与固定类别；API 初次运行及 resume 都以 host 生成的 message 报告 bar 序号和错误定位，并以相同安全字段写入状态。worker 消息、策略打印内容和 traceback 不进入响应/状态。
- 常见 Python 内置异常可定位；unknown/custom 异常只显示 `OtherError`。Execution UI 已回归确认可呈现安全的 bar/源码行反馈；尚缺更多 contract 错误类型矩阵。没有改变策略语言能力或真实执行边界。

## B93/B97 未来后缀扰动与 journal 重开已有组合验证，完整安全矩阵仍缺

- B93 验证不同后缀的独立完整运行；B97 再验证共同前缀提交后关闭并重开 SQLite journal/coordinator，恢复时仅调用 worker 处理后缀、重放事件不重复计算。prefix 状态、proposal、orders/fills 一致，后缀可改变最终估值。
- 尚未覆盖完整 forged-cursor、内省和容器逃逸变体。T04/T39 继续 PARTIAL；证据是合成离线数据，不是行情真实性或 PAPER 验收。

## B92 SMA 合成边界有证据，真实行情与 PAPER 仍未验证

- T03 的零差值、warmup、wait-for-cross 和 BackBroker 初始持仓切片现有真实引擎回归，详见 `ACCEPTANCE.md` B92a/B92b。
- 该结果不认证真实行情 provider、数据权限/point-in-time 质量，也不构成两个入口的真实 PAPER 交易证据。当前暂无需要新增的账户参数；真实 PAPER 前置条件仍按下方账户/授权阻塞项执行。

## B91 恢复快照内容已校验，真实对账仍未验收

- 采集完成和读取已 reconciled 快照时都会验证每条事实的规范 JSON/SHA-256；reconciliation proof 另绑定完整 facts 摘要。篡改后的事实不会用于打开 allocator 或 hydration。
- SHA-256 与 proof 均在同一 SQLite 中，不是带密钥签名；能同时改写数据库中事实和 proof 的本机攻击者不在该保护范围内。没有真实账户/Gateway 的断线恢复演练。

## B90 迟到成交会关闭派单闸门，但仍需实际对账

- CANCELLED/REJECTED 订单的后续有效 FILL 现在与 durable scope `RECOVERING` 状态原子提交，避免已释放 reservation 后仍按旧事实继续发单。新 intent 的最终风险/派单事务会拒绝该 scope。
- 该 latch 不会自行补取账户快照或调整在内存中的 broker position。需要由 managed runtime 的恢复协调器重新获取并核对真实持仓/活动单，再落到 PAUSED 并等待明确 activation。fake callback 覆盖不等于真实 paper 迟到回报证据。

## B89 紧急暂停不等于完整控制面

- `/api/execution/paper/pause` 是认证后的风险降低型操作；从现有 manager 解析 scope，不接受客户端账户/订单字段，只关闭 durable dispatch gate 并保持 callback/reconciliation 活跃。Execution UI 只在真实读到 RUNNING 时提供此按钮，并提示不会撤销现存订单。
- API key 和 loopback 不能识别人；因此目前只能开放这个对风险单调收紧的操作。activate、owned cancel 和 stop 仍无可归因 control API；不能把 pause 当作撤单、停止、清仓或授权撤销。真实 active runtime/app composition 仍未注册。

## B88 checkpoint 验证后的剩余恢复门槛

- 在线 Python execution journal 现会校验持久 JSON state 与上一条已提交、hash/event identity 均验证通过的 result.state 一致；检查失败先持久 PAUSED，不会启动下一次策略 worker 调用。
- 这不恢复 Backtrader 进程内 indicator、活动 native Order、order-ref 与待确认 callback consumer 的内存关联。`ManagedPythonPaperSession` 仍会对相关未决状态拒绝 attach 并保留 owner lease；下一步需单独设计可持久化 native-order reconstruction 和 broker reconciliation，不能把 checkpoint 校验当作 active-order recovery。真实 Gateway/账户演练仍未运行。

## B87 SQLite 恢复当前续做状态

- 共享 execution SQLite 现在有单文件一致性备份与验证原语，含 WAL 已提交内容；恢复拒绝覆盖现有路径，并把持久执行 gate 改成 `RECOVERING`，避免把旧的本地 RUNNING 状态当成可派单凭证。SQLite-only schema migration 可先留备份并在事务内回滚。
- T34 仍 PARTIAL：工具没有调度备份、远端副本、加密/密钥轮换、保留策略、实际应用停机/恢复操作手册或真实 IBKR open-order/position reconciliation。恢复文件不得直接接管 PAPER；须由可信 runtime 持新 lease，完成当前账户 scope 的完整 broker snapshot reconciliation 后再考虑 PAUSED/activate。

## B85 当前续做状态

- PAPER status 将 runtime factory 注册与完整 app composition readiness 分开，并返回策略 session、可归因控制身份、账户恢复事实、行情/合约与风险/成本能力的缺项码。默认 manager 与仅有底层 IBKR runtime factory 的配置都不能因此声称策略组合或派单就绪。
- `_set_paper_execution_composition()` 是显式注册 seam；它不启动 runtime。readiness 是 app 状态诊断声明，不是授权 gate 或 provider 真实性证明，生产 composition root 尚未实现/注册。`/execution` UI 已区分 composition incomplete 与 configured-but-stopped。
- 当前仍没有控制路由，也没有账户/恢复、行情/合约、风险/成本生产 provider 或可归因控制 principal。接下来需先实现并审阅真实可信 composition dependencies 与 app factory，管理 API 仍须等待人类身份及 grant 方案；绝不可在 lifespan 自动连接 Gateway。

## B86 managed cancel 当前续做状态

- `PaperExecutionManager.cancel_owned_order()` 与 `IbkrManagedPaperRuntime.cancel_owned_order()` 已提供 app 内可信管理撤单原语；取消使用 durable intent/order binding、独立 cancel grant、事务化 `CANCEL_PENDING`，结果未知则 `UNKNOWN` 且不重试，最终确认来自 callback。T25/T32/T43 的离线证据增加，但仍是 PARTIAL。
- 当前 API 的 `require_auth` 产生不可归因的 shared-key/loopback principal，manager 会拒绝这些主体。没有 route 调用该原语，也没有持久化人类身份/grant provider。不要为了让 UI 控件“可用”而放宽 attribution。
- `stop()` 不自动撤销活动订单；必须先有显式 cancel 授权，再等待真实 callback/reconciliation 收敛，最后由独立 stop action 检查静默并释放 lease。策略/native cancel 与管理员 cancel 的权限、事件展示和 app/UI 仍待接线。

## B70 当前续做状态

- FastAPI 已增加受认证的只读 `GET /api/execution/paper/status`；默认惰性 manager 没有 runtime factory，authorizer deny-all。返回不含账户 ref 或券商私有数据，且 app startup/lifespan 不启动 PAPER runtime。Execution UI 从此 API 读取状态；读取失败显示 unknown。
- B69 factory 仍未通过应用 profile/provider 接入 manager；只有显式受信任创建方可装配它。状态查询不会建立 Gateway 连接，也不能把 `NOT_CONFIGURED` 变成真实账户验证。
- 目前没有控制路由，故 API 不能启动、激活、暂停或停止 runtime。未来增加写接口前，仍需身份可归因、逐 action grant、显式配置审查、runtime 与 Backtrader/Python session/feed 组合、shutdown/quiescence 设计以及状态 UI/故障验证。

## B69 当前续做状态

- B69 新增显式 `IbkrPaperRuntimeFactory`，将 SDK subprocess、共享账本/index/allocator/snapshot store、callback worker、health monitor、snapshot coordinator/reconciler 与 `IbkrManagedPaperRuntime` 构造成一个 runtime；config 明确要求 Python/cwd、endpoint、client ID、account 和 SQLite/锁路径，依赖必须注入 recovery/activation/quiescence checks 与 callback/intent/contract handlers。构造只创建本地对象/SQLite，不启动子进程、获取 lease 或连接；只有显式 `start_paused()` 才连接，目标状态仍是 PAUSED。
- 目前 factory 是可测的离线组合 primitive，尚未注册为 app `PaperExecutionManager.runtime_factory`；没有已确认的 PAPER profile/account，也没有生产应用配置提供者、PythonPaperSession/Cerebro/market-data 接线或 attributable control principal。不要将“能构造 runtime”描述成应用已接 IBKR。
- 已知 live Gateway 端口 4001 被配置校验直接拒绝。真实连接仍需先获得已确认的 PAPER 登录 profile/account、授权与风险值、行情权限、可信 account/quote/grant facts、真实 market adapters，以及管理 API/身份授权设计；这些外部前置条件不阻止继续完善 offline session/composition 和故障验收。

## B68 当前续做状态

- B66 增加 bounded completed-bar queue feed，并用真 Cerebro/PortBroker 证明空 bar 等待期间可处理 IB callback；feed 只接 trusted host 的 Bar，仍未接 provider/calendar/manifest 校验器。
- B67 新增显式 `PythonPaperSession`，将冻结 Python StrategyVersion、逐事件隔离 worker、`PaperCompletedBarFeed`、真实 Cerebro/PortBroker、durable event bridge、dispatch 与 fake offline transport 组合；另新增完整 reconciled PAPER snapshot 的现金/持仓 pre-start hydration。两者目前都是可调用离线 primitive，尚未由 manager 使用，也未验证实际 broker 快照或 provider bars。
- B68 为 session 增加长驻 feed lifecycle，但仍要求 fake `offline=True` transport；新增市场 provider/calendar/rules/cost contract，所有 capability 默认为 false，当前没有生产 adapter。长驻逻辑也尚未接真实 IBKR SDK child、snapshot hydration 或 FastAPI manager。
- 当前没有 IBKR/行情配置、账户/quote/grant/risk facts provider 或可归因管理身份。下一步可继续做安全的 runtime root、同一账户快照/实时行情连接和 app/UI read-only 状态接缝；production activation/write API 必须依赖明确配置来源、账户事实来源和授权通道。未确认外部账户阻塞真实 paper 连接/写入，不阻塞离线工程。

- B65 已组合 SDK subprocess、callback worker、health monitor、快照协调/对账与 `PaperExecutionService`，并验证成功恢复落在 PAUSED、错账户保持 RECOVERING、不能证明停止安全时保留 owner lease。该编排对象由调用者显式构造/连接，构造本身离线。
- B69 已有可调用的显式 `runtime_factory` 实现，但 `api_server` lifespan、manager 单例和 API 仍未接入；缺 Gateway PAPER profile/账户声明、生产配置来源、已验证合约来源、账户/行情/风险 provider、策略 runtime 组合及可归因控制身份。不能在没有这些来源时从环境变量猜值，不能在应用 startup 自动连接。

- host callback spool consumer 和 `managedAccounts` 精确账户成员校验已实现并离线验证；这只解决可信消费线程和握手输入，不等于 app-managed runtime 接线。前文“下一步由 trusted runtime 消费 callback spool”现由 B60 部分完成。
- 当前最小工程缺口转为：一个 composition root 必须实际创建 process/ledger/order index/order-id allocator/pump worker/health monitor/service，并核实它们使用同一 canonical SQLite。B61 已提供可调用 status monitor 与 durable pause hook，但仍没有 app runtime 启动它。account summary、positions 和 open orders 尚无完整快照/end 标记与跨重启 reconciliation，因此该 composition 即使启动也必须停在 PAUSED，`ready_for_dispatch` 继续 false。
- B62 已把 service lifecycle 拆成先持 lease/RECOVERING、再显式完成 recovery→PAUSED，方便 runtime 在不开放 dispatch 时启动 SDK/spool worker；仍无 app composition 调用该 API，缺少完整 broker account/open-order/position facts。
- B63 加入与 SDK schema 一致的 host order-ID allocator。B64 已新增 SDK snapshot query/end markers、host durable snapshot store，以及将 broker open orders/positions 与本地 bindings/intents/fills 严格比较的 reconciler；证明先落盘，再按匹配的 fresh candidate 打开 allocator。
- B69 factory 已创建并管理 process、shared ledger/index/allocator/pump worker、health monitor、snapshot coordinator/reconciler 与 lifecycle service；尚未接入 Backtrader bridge/Python strategy 或 FastAPI manager。它可被测试构造但未用真实 Gateway 启动；部署配置和完整账户历史 repair 缺失。任何实际启动仍须通过明确控制动作、从 RECOVERING 回到 PAUSED 后再等待授权。
- 完整成交历史/执行查询与缺失 fills 的修复、开放订单差异处理、账户资金与报价 provider 仍未实现。B64 对账遇到任一本地无法解释的 broker order 或仓位差异会拒绝恢复，不做猜测式修补。
- 没有 paper profile/账户和授权数值；这只阻塞真实连接与下单验收，不阻塞离线 runtime composition、health latch 和拒绝派单故障测试。

## 真实 IBKR paper 验收尚不可执行

- 证据：当前交接文档没有已确认的 paper 账户、paper Gateway 登录环境/profile、预期账户、授权金额、风控边界或有效期；`BASELINE.md` 记录当前本地环境。既有 `IBKR_SETUP.md` 记载的是 live Gateway 4001/Read-Only，且不是本轮重新核验结果。
- 已尝试：只检查本地仓库状态和 Python 包元数据；没有连接账户。
- 最小外部动作：由用户在离线代码阶段完成后提供并确认 paper 环境和账户身份、合约/行情权限，以及策略预算、订单上限、总敞口、日亏损、授权期限和停止/清理策略。仅当所有离线安全门槛通过后再进行 paper 写入验收。
- 可继续工作：契约、真实 Backtrader 离线回测、风险纯函数、fake transport 故障注入、数据与恢复实验。

## 上传 Python 的 paper 隔离尚未成立

- 证据：见 `experiments/sandbox_probe/RESULTS.md` 及 ACCEPTANCE B4/B13–B18/B25–B28。旧 Vibe runner 在 macOS 未调用 Seatbelt、没有 `vibe-sandbox` 用户，且 `RLIMIT_AS` 失败时静默继续。新增 Docker worker 逐事件读取冻结 Python upload version，仅得到单个 completed bar + JSON checkpoint；真实 Python-upload Cerebro runner 已把 worker proposal 转为 native BackBroker order 并生成合成离线报告。B26/B27 已按 host decision log 用第二套真实 Cerebro/BackBroker 复算订单与报告；B81 将复算移至独立本地子进程，以规范化 JSON 请求摘要绑定 bars、Bar records、decision log、资金/费用、manifest 和 instrument，并核验响应摘要、协议与 PID。B28 将离线 event/checkpoint 接入独立 backtest journal，并提供 API resume，从首 bar 全量重放；B83 验证有状态 JSON checkpoint 重启续跑，B84 发现的 checkpoint 篡改、格式损坏或与最近已提交 result.state 不一致现会先持久 PAUSED 且 worker=0。该数据库没有账户身份或可派单 outbox。paper/live `PythonEventCoordinator` 尚未接入 Backtrader lifecycle；缺少可信在线账户风险与发送服务。新增 outbox consumer 只把授权 proposal 幂等写成 CREATED intent，不执行资金 reservation 或 broker send。
- 数据边界证据：见 `experiments/data_replay_probe/README.md`。关闭 preload/runonce 后，Backtrader line buffer 按事件扩展，但策略代码仍能通过同进程 Feed 参数或 `PandasData.p.dataname` 读取完整输入。
- 已尝试：在目标 macOS 用子进程真实测试网络/文件访问与 RLIMIT；用本地真实 Backtrader 测 Feed 的 line buffer、正向索引及底层数据可见性；用独立 Colima profile 真实构建并运行无网 worker 探针，拒绝宿主文件/网络并检查资源、mount 和结束清理。
- 当前决定：可信模板和 Python upload 的离线回测继续；B51 已将 event→intent→native order→dispatch→callback→fill 组合到真实 Cerebro 测试与 fake transport，B52 又覆盖已拥有活动订单的持久化撤单请求、精确 broker ID 与 callback 确认，B55 加入但尚未应用接线的单槽 lifecycle manager；B58 增加了断连启动的 SDK 子进程和私有 IPC，但还没有业务 recovery/runtime factory/app 集成。上传 Python 在接入官方 SDK runtime、app-managed IBKR lifecycle 和经鉴权管理面，并通过账户 reconciliation、故障矩阵及 T37/T39/T40/T42/T44 验收前，不获得 paper/live 部署能力。
- 最小工程动作：受信任 service seam、输入 artifacts、报告复算和离线 durable backtest replay 已有 B19/B26–B29 证据。B29 在共享 SQLite ledger 内完成单笔 intent 的 reservation→事务化本地账户/策略风险复查→SEND_STARTED→injected sender 状态机；B34–B42 已增加 BT order-ref 原子绑定、native order callback ACK、官方 SDK 合约/订单构造与 `nextValidId` 分配器切片，并要求 dispatch 持有 broker/account/environment 本机 owner lease；跨 OS process 同锁目录争用已验。B43 另加本地持久运行控制状态，新 scope 默认 PAUSED，最终 send 事务再次核对 RUNNING。B46 真实 Docker 故障用例现已证明宿主 Python supervisor 被强杀后，容器内硬 deadline 会终止策略 worker 且 Docker daemon 自动移除容器。B48 新增 PAPER lifecycle primitive；B55 增加单槽 lifecycle manager seam，验证 startup recovery 后 PAUSED、可归因授权控制、quiescent stop 和失败恢复时 lease 保留；B58 已实现但尚未 app 接线的 SDK 子进程及有界私有控制 IPC。仍缺真实 facts provider、跨数据库账户容量/跨机器唯一 deployment owner、FastAPI/app IBKR service wiring、业务 callback pump/账户订单对账和 paper/live 故障恢复。下一步由 trusted runtime 消费共享 SQLite callback spool，在 ledger 事务成功后 ACK，并把 SDK process 与 manager/service composition 对接；再做服务重启 broker cursor/open-order reconciliation、callback correction/error 语义、资源耗尽与后代进程故障验收。每 bar 启动全新容器，当前吞吐仅证明正确性切片，不是生产吞吐设计。

## 真实行情供应商与权威日历尚未冻结

- 代码已增加 CSV 导入边界：原始字节 hash、OHLCV/时区校验、显式 session schedule 精确覆盖，并将 `available_at` 作为 Backtrader 时间索引；`run_verified_backtest` 可生成实际 Cerebro/BackBroker 报告。
- `/execution` 现允许用户显式选择 yfinance 或 Stooq 并启动受限 SMA 日线回测；实际 provider 在线运行尚未验收。runner 保存规范化 OHLCV 快照并与 manifest hash 对齐，但 provider 返回内容、历史时点可用性、复权和符号映射仍未认证。
- B30/B31 将固定版本 `exchange-calendars==4.13.2` 的 XNYS 交易日程接入 SMA provider runner，并经严格 importer 核对 OHLCV、可用时间、交易日覆盖和 schedule hash；请求日期含首尾，首尾及中间缺失 session 都 fail-closed。离线测试覆盖 DST、感恩节半日市、节假日、缺失交易日和 UTC 午夜 session-date 标签。该 Python 日历包不等于交易所官方授权日历，也未核对实时/历史更正。
- 现有运行证据仍只使用本地合成 bars；manifest 将日历包版本/hash 与 provider 来源声明分开记录，provider attestation、点时可得性、证券代码到 IB 合约映射、复权和公司行动仍为未验证。没有真实供应商冻结文件和授权，不能声称真实行情验收完成。
- 最小外部动作：选定允许本地研究使用的数据源和权威 session schedule，并提供冻结文件/元数据供离线验收。实施不会自动下载数据或代替供应商授权。

## 官方 IB API 来源和 SDK 命令构造切片已完成，完整执行服务尚未集成

- 证据：官方 TWS API 1050.02 的本地 `setup.py + ibapi/**` 共 237 个文件，树哈希 `ff7a0e7aaf077042190426f08aaac11cbd9e4b548507e450f84ac961ba83ed7f`，版本为 `10.50.2`。`ibkr-runtime/` 用独立 `uv.lock` 固定 protobuf/build tooling；主项目可选 `channels` 与 IBKR 的 protobuf 固定版本不兼容，不能把 SDK 依赖并入主项目 lock。
- 已验证：`scripts/install_ibapi_from_official_source.py --verify-only` 检查版本、文件数和树哈希；随后通过独立 Python 3.12.13 环境成功从该源码安装 `ibapi 10.50.2`。这锁定的是解压后的源文件树，不是官方压缩包 archive hash；源码仍需由操作者从 IBKR 官方下载页取得并接受许可。
- 已实现离线切片：官方 SDK 回调 adapter 与 `SEND_STARTED` 前置持久化的 `IbapiOrderSubmitter`；SQLite order ledger；真实 Cerebro `PortBroker`；durable callback spool/inbox、累计回报应用与 native Broker 消费后 ACK；P3a dispatch 在最终风险写事务中原子写入 `SEND_STARTED`、attempt 和 broker-ID→intent→Backtrader-ref mapping。B37 新增官方 SDK Contract/Order factory、intent/command 对齐的注入 transport、durable `nextValidId` allocator 和相同 SQLite 路径的本地 owner lock。B45 增加版本化单事件 stdio 请求/响应、请求关联 ID 和严格响应字段校验；B46 以真实 Docker 故障用例证明宿主 supervisor 强杀后的容器内 deadline 与自动删除。独立 SDK/runtime 19 项和 callback pump 21 项通过；owner lease/dispatch/callback 专项 B41 为 30 项通过；最新 B47 相关后端子树 224 项通过。
- 剩余阻塞：仍缺 app-level execution service wiring、经授权的合约映射来源与账户/行情/grant/deployment facts provider、跨机器或账户级唯一 owner/共享 reservation、commission/correction ledger、错误/警告分类和恢复编排、管理通道的人类身份认证、服务重启后 broker/native order reconciliation，以及真实 paper ack/fill/recovery 证据。B45 的 request ID 只是关联字段，不是认证凭证；B58 的私有 subprocess IPC 只供可信 parent 使用，尚未连接 app manager，也不是管理 API 的人类身份认证。本机文件锁不是分布式 owner 保证，应用尚未管理 lease 生命周期/目录配置，人工合约映射与合成 callback 不是券商认证。paper 授权到位且服务集成/故障测试通过前，不连接 Gateway 或发送订单。

## Python upload paper lifecycle 与 online risk 尚未完成

- 新增 `PythonOutboxIntentHandoff` 代码切片：proposal 经可信 host authorizer、冻结到 journal 后幂等落入 order ledger；风险拒绝可持久终结。B25 相关后端组已验收，168 passed、292 warnings；覆盖 LIVE 拒绝、风险与 grant 身份绑定、完整性篡改暂停、ledger-link 崩溃恢复和双线程消费收敛。此代码不提供 grant store、权威账户/报价事实、reservation 或 broker send。
- ledger 去重键现包含 host 生成的 `decision_id`：同一决策重放仍幂等，不同 run 的决策不会因 deployment/version、sequence、bar 相同而冲突；同一决策内容不一致仍 fail-closed。B25 覆盖双线程消费，独立进程竞争/恢复仍未验收。
- B29 新增 `PaperOrderDispatchService`，通过注入的 facts provider 在 reservation 前与 `SEND_STARTED` 前分别刷新风险；假 sender 故障用例验证拒绝时 0 次调用、未知结果冻结重发。此 service 仍未连接可信 run/deployment owner、grant/account/quote provider 或 IBKR/PortBroker。
- 未完成的重点是**跨 ledger/数据库**账户容量与唯一 active deployment/owner、Backtrader order-ref 到已冻结 intent 的可信映射，以及 reservation→风险复核→start_send→IBKR client 的单一运行服务。B29 已在共享 SQLite ledger 的 `BEGIN IMMEDIATE` 发送事务中汇总同账户/策略的其他本地 reservation，并以并发测试证明同一现金预算不会被两笔 intent 同时消费；这不覆盖多个 ledger 或券商外部挂单事实。

## B80 后仍未通过的 T39 与 provider provenance 门槛

- B76 让离线 Python upload event coordinator 持有 importer 输出的固定 Bar 序列，按 1-based durable journal cursor 取事件；它不再接收单次调用方 Bar，journal 拒绝跳过当前 cursor。SMA 与 Python upload 的真实 importer 路径会将实际消费的规范 OHLCV/timestamp digest 与 manifest 比较。
- B76b 已在同一条真实 Docker worker + journal + coordinator + Cerebro 路径中验证单-bar request、前向索引失败和顺序持久化；B76 也将当前 Backtrader feed UTC timestamp 与固定 Bar 的 available_at 对齐检查接入 coordinator。B78 进一步验证“合法未来 timestamp + 跳过 seq”会在 journal 内持久 PAUSED，worker 调用为 0，cursor/event 不推进；重试正确 seq 仍被拒。B80 增加真实 Docker/importer/Cerebro future-only canary，用随机数值作为第二根 CSV Bar 的 OHLCV；第一事件 worker 对受限调用栈、模块、GC 对象图及 `/proc` 环境/命令行/maps/fd 可见面扫描，未发现 canary，整个回测仍正常完成。单独真实 Docker worker 路径另有宿主文件/网络负例。仍缺更多 host cursor 变体和完整 introspection/逃逸攻击矩阵。T39 保持 PARTIAL，Python 不开放 paper。
- 输入 digest 只证明回测使用的 bars 与 manifest 一致；manifest 的 provider、许可、历史点时可得性、adjustment/corporate action 与 instrument mapping 仍是 caller-declared/unverified。当前没有具体 MarketDataProvider adapter，也没有可被复现验收的冻结真实行情文件或授权证据。
- 下一工程动作：继续扩展不同伪造 host cursor、对象/文件句柄和动态内省变体，并验证运行时异常/SQLite corruption 的 fail-stop 状态。当前 canary probe 有明确遍历上限，只能提供有界负向证据。真实 provider adapter、授权/来源、下载参数和点时/复权内容 hash 仍缺；没有授权行情时继续将真实数据项报告为 BLOCKED，不伪造样本。

- 证据：`python_upload.py` 已通过 `invoke_strategy_worker` 在真实 Cerebro `Strategy.next()` 中按 current completed bar 执行隔离 worker，并用 native `bt.Order`/BackBroker 收集订单、fills、费用和 equity。`python_upload_service.py` 只接受 immutable version 与严格 importer 结果并持久保存证据；独立认证 `POST /runs/backtests/python-upload` 已连接该 service。B28 将离线 Backtrader event lifecycle 接到单独 backtest journal。B50 的 `PythonPaperEventBridge` 比较 durable/current Bar、检查旧 outbox 和同账户未结账本工作，并在恢复事件不匹配或超过一 proposal 时暂停；B51 已在真实 Cerebro 离线组合中接入 `BacktraderDispatchPort`、SQLite 风险/订单账本、durable callback pump 与 fake transport，回报填充 native Backtrader order。dispatch/callback mapping 同时持久化并逐项核验合约 conId、symbol、security type、currency。仍缺 app-level IBKR client/runtime 管理、正式合约与账户/报价 facts provider、管理 API、订单/持仓/现金 reconciliation 与真实 paper 证据。
- 剩余工程动作：B28 已把专用 durable journal 接到离线 Backtrader run lifecycle，并在 API resume 时从首 bar 全量重建 BackBroker；它不提供在线账户/订单恢复。B81 补上独立本地子进程复算，但没有 artifact 签名或篡改后落盘重验收，也不能排除两套 Backtrader 共用算法缺陷。随后继续完成可信 execution_service 与 risk/outbox/Broker composition，再做 paper worker/host 故障矩阵。大量 bar 一事件一冷容器很慢，生产前需测性能或实现不暴露未来数据的有界长驻 worker 会话。

# Managed PAPER callback/session 尚未进入 app-managed 运行

- B136 真实 Docker worker deadline 能终止死循环、保留未提交 released event 且删除容器；它只走 PythonEventCoordinator，没有验证 ManagedPythonPaperSession 因异常而暂停 scope/journal。B137 补上真实 worker timeout→managed session→service/journal PAUSED、独立 callback-worker 的 fill 及 exact-intent 授权撤单离线切片；IBKR runtime/transport 仍为 fake，不证明真实 SDK/Gateway 或 crash recovery。

- B135 在单一 reopen 场景联合验证 exact-linked partial BUY/native ref=77 恢复、后台剩余 fill 落账并由 Backtrader 完成旧单，再用非空 Python checkpoint 恢复下一事件；这证明 B127/B134 两个离线机制可顺序协作。仍使用 fake IBKR/runtime/sandbox，没有真实 worker crash 或 Gateway 证据；BUY 之外状态继续 fail-closed，T41/T42 不升级。

- B134 验证 managed session 重开 SQLite 后将非空 Python checkpoint 传给后续事件，且 state/proposals/cursor 与不中断 reference 一致；这是 seeded committed prefix + fake sandbox，没有实际 worker 崩溃，也没有联合恢复 active native order/state。T41 仍 PARTIAL；后续仍需隔离 worker timeout/crash、native indicator/order-ref 与真实 Gateway 故障矩阵。

- B133 以可控阻塞策略回归证明 callback worker 可独立将已绑定订单的部分成交写入 SQLite，manager 会拒绝不可归属撤单并仅对可归属主体授权的 exact intent 发送撤单，未提交的策略 proposal 不会提前派单；该用例使用测试预置订单和 fake IBKR，不验证真实 Gateway 或 crash recovery。T43 仍需真实持续回报/对账、进程崩溃与重启后的 native order/ref 恢复，以及生产管理身份与 app wiring。

- B71 已修正 callback pump 的进程内 deferred-ACK 重复投递，并验证未确认 inbox 跨进程重建后可恢复；显式 `callback_worker_owns_pump=True` 能避免 Backtrader/Cerebro 与 managed worker 双重消费。
- B72 增加 managed runtime 的 current recovery snapshot accessor，activate 后旧 snapshot 失效；FastAPI manager 尚未用它创建同一生命周期下的 Cerebro `PortBroker`、worker、verified feed、策略桥接和 `PaperOrderDispatchService`，也没有完整 conId→feed 映射用于启动前 hydration。
- B73 已新增并离线验证 `ManagedPythonPaperSession`，在 fake SDK child 下跑通 frozen Python event→风险/账本→place_order→durable execution callback→Backtrader native fill；启动/停止均 fail-closed。B74 再将固定 XNYS 日历的开收盘边界接到 PAPER feed，拒绝节假日/周末和时段不匹配 bar。session 仍是调用者显式组装的 primitive，没有被 FastAPI manager/runtime factory 构造。当前真实行情 provider、供应商授权/完整 bar 证据、账户/报价/费用/风险 grant provider、合约映射来源和生产运行管理身份均缺；不可从环境变量或默认配置猜值。
- B73 的完整纵向用例使用合成账户快照和 risk facts，且通过 fake SDK 子进程，不构成真实 PAPER adapter 验收。达到 P3b 仍需用户提供并明确确认具体 PAPER Gateway/account、行情权限/标的、预算与有效期/风控，确保只读校验的 Gateway profile 为 PAPER；同时完成真实数据/contract source 与人工操作授权。当前不能尝试连接。
- pump inbox 重放不等于 native Backtrader 恢复：新 `PortBroker` 的 `orders`/order-ref 注册表和执行游标没有从 SQLite 重建。若重放回报引用未知 order_ref，Broker 会 fail-stop；当前不支持有活动单/未知发送结果的策略跨进程续跑。managed session 必须先拒绝这类状态，直到实现 native order/ref/checkpoint 恢复与独立 BackBroker 影子重放。
- snapshot freshness window 默认 10 秒只能限制已知 snapshot 的年龄，不会侦测第三方在对账完成和 Broker hydration 之间刚好修改账户；真实 paper 部署仍需单一账户 owner 与外部订单隔离保证。
- 因此 app 尚不能启用 PAPER strategy session：不得放宽 `PythonPaperSession` 的 `offline=True` 检查，也不得用默认初始现金启动 managed broker。manager composition 与真实可信 provider 未完成前，不将 PAPER status/SDK health 解释为 strategy dispatch readiness。

## P2 上传审阅已补，NL2 与真实数据仍未完成

- B79 已接入受控单次 SMA 意图解析：完整旧句式走确定性路径，其他表达经配置 ChatLLM 转成严格 JSON intent，再由服务端编译；确认 token 保存 preview 生成的 spec，避免 create 时重新采样。解析/API 19 passed、相关后端子树 340 passed、UI 3 passed、build 成功。T12 仍 PARTIAL，实际 provider、任意条件遗漏和多轮问答未验收。
- B75 增加 Python source/data preview 与确认 token；B77 又将滑点参数纳入确认摘要和 hash。变更源码、CSV、session schedule、metadata、现金、佣金或滑点参数会使一次性 token 不匹配；preview 不创建 run 或 StrategyVersion。上传 UI 显示摘要与 hash，并在确认前冻结输入。
- B77 现在以 BackBroker 百分比滑点模型覆盖离线两入口；默认滑点为 0，Python upload 可指定 0–25%。尚无基于目标券商/标的/订单规模校准的真实成交成本模型，不能把当前值解释为 provider 或 IBKR 的实际费用/滑点。
- NL 入口现在对非固定句式尝试模型抽取，但仍只支持 SMA 双均线模板，未做多轮问答、其他指标模板、系统化任意自然语言遗漏检测或 provider 故障的端到端验收。模型输出须声明支持的 entry/exit/direction enum 并列出 unsupported conditions；确定性预检覆盖部分常见过滤条件。用户必须在确认卡对照原文和规则。需用 mock 输出验证遗漏/多余字段和错误 fail-closed，并在受控环境验收当前 provider。
- Python CSV 和 SMA provider 路径都允许 caller/provider 数据形成离线报告，但 provider/授权、Point-in-time availability、公司行动/调整和 symbol→contract mapping 仍未认证。数据不足必须明示，不把 fixture 报告标成真实市场证据。
