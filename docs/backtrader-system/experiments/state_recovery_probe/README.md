# Backtrader 策略状态恢复探针

对应 `REVIEW.md` 第 4 项、T41 和 `CONTRACTS.md` §2、§5.3。该离线实验用真实 Backtrader `Cerebro` 与本地 `BackBroker`，比较连续运行和在第 24 根 bar 保存 JSON 状态、重启后从完整历史前缀暖机并续跑。没有连接券商、Gateway 或网络，也没有提交真实订单。

## 运行方式

从仓库根目录执行：

```sh
PYTHONPATH=/Users/yaozekun/strategy_verification/backtrader ./.venv/bin/python docs/backtrader-system/experiments/state_recovery_probe/probe.py
```

记录执行环境：Python 3.12.13、Backtrader 1.9.78.123。上述命令退出码为 **0**。

## 验证内容

- 策略的 `snapshot_state()` 输出经 JSON 编码再解码后，传给新策略实例的 `restore_state()`；状态包含 schema 版本、有效事件数、累计趋势事件、冷却计数、信号数、上次动作、订单通知数和成交通知数。
- `RecursiveVolatility` 是逐步更新的 EW 均值/方差递归指标，历史影响不受固定窗口截断。恢复进程从第 1 根历史 bar 开始重新计算指标；第 24 根及以前只暖机，不运行策略状态更新或决策。另一个 EMA 指标也由同一历史前缀重算。
- 样例信号依赖递归指标、累计趋势事件和冷却变量。触发时通过真实 BackBroker 提交固定数量买单，`notify_order` 更新的订单状态字段也进入 JSON snapshot。
- 对第 25–44 根 bar 的每个 `next` 事件，比较日期、close、EMA、递归波动指标、决策及全部策略状态字段。两条事件流必须逐项相等，否则脚本以非零码失败。

## 实际结果

```text
backtrader_version=1.9.78.123
python_version=3.12.13
input_bars=44 cutoff_bar_index=24
checkpoint_json={"cooldown":3,"last_action":"BUY","last_order_status":"Completed","order_fill_count":2,"order_notification_count":6,"processed_events":18,"signal_count":2,"state_schema_version":1,"trend_events":11}
compared_future_events=20
future_buy_bar_indices=[33,41]
future_order_notifications=continuous:6 resumed:6
all_future_event_state_and_decision_equal=true
broker_end_state=continuous(value=99997.68,cash=99544.98) resumed(value=99994.50,cash=99768.15)
probe_result=PASS
```

退出码 **0**。续跑后的 20 个事件状态和决策完全相同，包含 bar 33、41 的买入决策，以及后续 BackBroker 通知带来的状态更新。

## 结论与边界

**策略自定义 JSON 状态恢复与指标前缀暖机：此合成离线样本通过。** 指标状态由原始输入前缀重建，自定义变量从 JSON checkpoint 恢复；只恢复少数最近 bars 不足以重建该递归指标。

这不是完整账户恢复验收。恢复运行创建了新的 BackBroker，未恢复连续运行的现金、持仓或未完成订单；策略决策也刻意不依赖现金/持仓。因此末值出现差异（连续运行权益/现金 99997.68/99544.98，恢复运行 99994.50/99768.15）是预期且可见的范围限制，不影响本探针逐事件比较的策略状态与决策结论。真实恢复还须先由可信执行服务完成账户和订单对账、恢复 Broker 状态，再把实际事件纳入影子重放；任何策略输出不一致应暂停。

此外，数据是确定性合成日线，checkpoint 通过进程内 JSON 对象传递，未验证磁盘原子写入、跨进程协议、策略隔离、故障崩溃点或 IBKR 回报。此探针不能单独证明任意用户策略可安全恢复或 paper/live 可部署。
