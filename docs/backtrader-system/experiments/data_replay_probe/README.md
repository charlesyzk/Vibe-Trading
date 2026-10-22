# P0 逐事件输入与独立报告复核探针

更新：2026-10-20。探针使用本地 Backtrader 源码 `1.9.78.123`、Vibe `.venv` 的 Python 3.12.13/Pandas 2.3.3。没有读取真实行情或连接账户。

## 复现

从 Vibe 仓库根目录运行：

```sh
PYTHONPATH=/Users/yaozekun/strategy_verification/backtrader ./.venv/bin/python docs/backtrader-system/experiments/data_replay_probe/probe.py
```

结果：退出码 0。

## 观察

- 真正的 Cerebro 使用 `preload=False, runonce=False` 时，逐根可见的 Backtrader line buffer 长度依次为 1–6；从 `close[1]` 正向读尚未释放的 bar 得到 `IndexError`。
- 这**不构成安全隔离**：同一进程策略可通过 `PandasData.p.dataname` 访问完整 DataFrame。探针 Feed 也把完整的六行样本放在 `p.rows`，策略可直接读到下一根 close=11。因此该 Feed 明确是负向例子，不能进入生产。
- 未来数据必须留在可信进程/存储侧；策略进程每次只应从经过权限检查的 IPC 收到当前 event 的一条 bar。Feed 对象及其参数不能持有完整 DataFrame、列表、可查询历史路径或可任意推进的游标。IPC 是否满足此边界仍未实现/验证。
- 订单日志复核通过两个真实 Cerebro + BackBroker 执行：第一次产出 `bar 1 BUY 2`、`bar 3 SELL 1`；第二个不加载原策略逻辑，仅按捕获的订单意图重放。成交分别为下一根开盘价 11.0 和 9.0；佣金 0.022/0.009；两次现金 986.9689999999999、期末权益 1000.9689999999999。忽略进程局部 BT order ref 后，意图、成交和金额完全一致。

## 结论

- **独立 BackBroker 订单日志复核：此最小合成样本 PASS。** 仅验证公开引擎路径和成交复算，不代表正式报告格式或成本模型已冻结。
- **逐事件 line-buffer 行为：PASS；未来数据隔离：FAIL / 待 IPC 设计。** 内存中完整历史被 Feed/策略共享时可直接绕过 line buffer。该事实支持 REVIEW 的边界要求，不能报告 Python 策略未来数据安全。
- 尚未覆盖真实冻结数据、修改未来数据不影响历史决策、IPC 伪造 cursor、底层文件/网络访问、上传代码修改同进程 Broker/Analyzer 等完整 T39/T44 负向场景。
