# 部署与维护说明

本文面向维护 Vibe-Trading 本机开发环境的人员。普通使用者只需打开应用、登录 IBKR PAPER 并按 [使用说明](USAGE.md) 操作。

## 本机 Python worker

Python 上传回测需要一个已构建、固定 image ID 的隔离 worker。应用不会在运行时下载或构建镜像。

在本机 `colima-vibe-execution` Docker context 中构建并读取不可变 image ID：

```sh
docker --context colima-vibe-execution build \
  --network=none --pull=false \
  --file agent/src/execution/strategy_worker_image/Dockerfile \
  --tag vibe-strategy-worker:local \
  agent/src/execution/strategy_worker_image

docker --context colima-vibe-execution image inspect --format '{{.Id}}' vibe-strategy-worker:local
```

将实际输出的 `sha256:...` 写入用户级 `~/.vibe-trading/.env`：

```dotenv
DOCKER_CONTEXT=colima-vibe-execution
VIBE_PYTHON_WORKER_IMAGE=sha256:<实际的 image ID>
```

重新启动开发后端使配置生效：

```sh
bash scripts/dev restart backend
```

启动脚本会合并加载项目 `agent/.env` 与用户级 `.env`；用户级配置优先。不要将 `.env`、账户 ID、密码或验证码提交到版本库。

## IBKR 本地连接

- 使用者先在 Gateway/TWS 登录 PAPER；回测服务通过本机 API socket 连接，不在应用里收集 IBKR 密码或验证码。
- 标准 Gateway PAPER 端口为 `4002`，TWS PAPER 端口为 `7497`；LIVE 端口要使用 LIVE 只读 profile，当前回测使用 PAPER 配置。
- 首次排查先在连接中心执行只读连接检查；确认环境、端口和连接状态后，再从策略执行页请求历史行情。
- worker 缺失、Docker context 未运行或镜像摘要不匹配时，Python 上传预览应显示不可运行或失败。维护人员应检查 API 进程配置和该 Docker context 中的镜像，不要用可变标签代替固定 image ID。

## 部署范围

以上步骤记录的是本机开发环境。发布到另一台电脑、打包版桌面应用或服务器时，需要在目标环境单独准备受信 worker 镜像并注入其 digest 与 Docker context，再按目标应用的服务管理方式重启后端。不要把本机 Docker image ID 当作其他机器的配置；每个目标 Docker daemon 都要有对应镜像。

IBKR 行情许可、point-in-time 语义、复权规则和合同映射仍需独立评估。本文没有启用账户持仓读取、what-if、PAPER 订单或 LIVE 交易。
