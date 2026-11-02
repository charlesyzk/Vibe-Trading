# Windows PAPER 交接：当前状态与下一步

更新时间：2026-11-02。接手时先读本文件、`PROGRESS.md`、`BLOCKERS.md`、`ACCEPTANCE.md`、`DECISIONS.md` 和 `PAPER_USER_GUIDE.html`；较早的进度条目是历史记录，以上方最新条目为准。任务基线为 `95c87ad7`，本轮变更从 `e3e38dcb` 工作树继续。

## 用户目标与明确边界

- 合作伙伴使用 Windows 安装包，另行安装并登录 IB Gateway 或 TWS 的 PAPER 账户；不需要安装 Python、Docker，不需填写 token 或内部端口。LIVE 下单禁用。
- 支持分别为受支持标的设置整股数量、订单类型、方向、参数、交易窗口及风险上限。**每份草稿绑定一个标的**；可以为 AAPL 和 MSFT 等分别准备草稿，并非一份策略同时交易多个标的。合约资格、行情、账户事实和风险不满足时阻止并显示原因。
- 同一个 PAPER 账户由伙伴共享，只预期一个活动 owner/session；撤单须覆盖授权内的已有活动委托，即使新单窗口已经结束。
- **禁止代理发送或验证真实券商订单**，包括 PAPER；真实订单 ACK、成交、撤单回报和对账由用户及伙伴后续验证。假券商、自动化测试、打包及不发订单的只读 Gateway 验证可进行。
- 保留工作区未提交工作；不要覆盖历史结果。不要将私有仓库的安装器公开发布，也不要将 IB Gateway 专有二进制纳入仓库。

## 本轮实现

1. **通用草稿及界面**：上传 Python 策略、冻结参数与来源、IBKR PAPER 日线回测、选择已验证版本、核对订单证据、设置时段与风险、保存和部署草稿、启动/停止、订单及成交历史。订单约束绑定冻结版本、标的、整股数量、类型及对应价格；不再把 AAPL 1 股固定联调限制套到通用草稿。固定联调入口仍单独存在。
2. **风险和账户**：本地桌面操作员身份、PAPER 账户自动发现及账户绑定；合约、账户、报价、时段、金额、持仓预算、每日亏损和订单笔数由服务端核验。具体失败原因由界面显示；只有端口监听不能证明账户已验证。
3. **撤单和恢复**：撤单授权独立于新单窗口，仍要求受信 PAPER 账户、应用自有订单及有效授权。保留 pending/confirmed/unknown 状态，支持符合条件的停止后恢复并对账；已成交部分不能撤销。202 只作为已绑定订单通知，正式 `Cancelled`/`ApiCancelled` 状态才确认撤单；未知 ID、跨账户关闭执行，10148 保持未知并要求对账。
4. **跨日运行**：最多 90 自然日窗口，XNYS 常规时段以紧前交易日完整日线驱动；缺失、跳日或不一致时暂停。应用重启不会自动续跑；暂停后的草稿需停止、核对并准备新回测；已有活动委托的恢复有原草稿、配置、绑定和授权等条件。
5. **Windows**：Electron 自动创建临时本机 PAPER 身份，自动只读识别唯一 PAPER Gateway/TWS。将 WASI CPython/Wasmtime 隔离策略 worker 和独立 IBKR runtime 打入安装包，保留进程隔离；跨平台文件锁及 Windows 管道、启动和安装脚本已接入。Gateway/TWS 不随包分发。
6. **手册**：`PAPER_USER_GUIDE.html` 与 `frontend/public/PAPER_USER_GUIDE.html` 一致，最新独立 HTML 已补齐首次使用按钮顺序、示例、风险字段和故障排查。**已构建 exe 内置的是更新前的手册**，给伙伴同时提供新版独立 HTML；源码版本下次构建才进入 exe。

## 已有证据与交付物

- 最新 unsigned Windows review 安装器：`/Users/yaozekun/strategy_verification/vibe-trading-output/windows-paper-review-41378682142/Vibe-Trading-Desktop-Unofficial-0.3.0-x64.exe`，版本 0.3.0，大小 381935433 字节，SHA-256 `3b810be0d4c703c44d11d1f42b06a5866df5c7ec6997893a58316871c1e8e374`。新版独立手册在同目录 `PAPER_USER_GUIDE.html`。本地路径不随 Git 同步。
- 私有构建仓库 `charlesyzk/vibe-paper-win-validation-9m2bfepe` 的私有草稿 `paper-review-41378682142` 含安装器及校验文件，需相应仓库权限。不要公开发行。旧包 `41368955377` 早于 202 修复，不应替代最新包。
- 私有 Windows Actions `41378682142`，应用快照 `eaefd62933341d7cf750a711c1237779fcad0d55`：打包 WASI worker、上传回测、日线调度与回调等 **120 passed、97 warnings**；干净安装、启动、同版本重装及配置保留通过。该 run 不含后来的 B347 测试扩展及 B356 手册修改。
- 本机 callback pump **68 passed**，含部分成交 → 202 → 正式取消的假回报；先前相关后端 **151 passed、168 warnings**，前端 **36 passed**、production build 通过。AAPL 2 股和 MSFT 5 股的假券商/真实 Cerebro 测试通过；覆盖范围与命令详见 `PROGRESS.md`、`ACCEPTANCE.md`，不要把这些假测试当真实订单证据。
- 私有 Actions `41395038654` 验证新版安装包哈希及包内 IBAPI license、官方 `client.py` 和独立 runtime worker 存在；不等同完整许可审阅。构建使用的 SDK 许可证文本是 GPLv3。
- 早期曾只读获取 PAPER AAPL 日线并进行离线回测（见 B 记录）；当前本机 Gateway 的只读发现/账户读取多次超时，未能确认当前 PAPER 会话。监听端口不等于连接成功。未发送真实订单。

## 未完成、限制及建议接手顺序

1. 在用户或伙伴提供**已登录且 API 可用的 PAPER Gateway** 后，先做有界、只读账户发现和行情检查，不输出账户敏感信息；确认 Windows 安装应用能识别正确 PAPER 账户、显示状态并获取数据。先前失败是超时，未确定根因；不要循环做无状态探测。
2. 用户和伙伴负责真实 PAPER 订单 ACK、部分成交、撤单、断线/重连及对账验收；记录准确结果，不推断真实回调顺序或拒单状态。应用不自动续跑，操作前先核对已有订单与持仓。
3. 审阅 SDK 和其他依赖的分发许可与义务；包内存在许可证和源文件不等于合规审阅完成。
4. 如需新安装包，基于本次提交重新在 Windows 构建、测试、安装、重装和独立校验 SHA；新版手册尚未进入现有 exe。当前证据仅覆盖同版本重装，不证明跨版本升级，也不证明已登录 Gateway 的完整伙伴流程。
5. 没有明确授权不要发布、推送上游、发送消息或下真实订单。不要把旧 `BLOCKERS.md` 历史条目当当前事实。工作区和私有构建目录的已有改动须保留。

## 常用入口

- 用户手册：`docs/backtrader-system/PAPER_USER_GUIDE.html`；打包副本：`frontend/public/PAPER_USER_GUIDE.html`。
- 主要实现：`agent/src/execution/paper_*`、`local_paper_*`、`ibkr_callback_pump.py`、`wasi_*`；API：`agent/src/api/paper_execution_routes.py`；界面：`frontend/src/pages/Execution.tsx`。
- Windows 构建：`desktop/electron/WINDOWS_PACKAGING.md`、`desktop/electron/scripts/build-paper-installer.ps1`、`.github/workflows/paper-windows-validation.yml`。官方 IBKR SDK 来源和许可文件按构建说明提供，不在公开仓库存放 vendor 副本。
- 可核对 `git diff --check`、两份手册 `cmp`，以及 `ACCEPTANCE.md` 中具体测试命令。先按变更范围选择测试，不要用本机测试替代 Windows 或真实 Gateway 证据。
