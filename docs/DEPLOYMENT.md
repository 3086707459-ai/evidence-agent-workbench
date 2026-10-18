# 部署与运行

## 本机与 CLI

按 README 安装后执行 `evidence-agent serve`。默认只绑定 `127.0.0.1:8090`，资料来自工作目录 `corpus/`，状态存入 `data/agent.db`。本机无鉴权适用于受控个人演示，不能防止本机其他进程访问。

```bash
evidence-agent run '比较 Atlas 和 Boreal 的离线与审计能力，并引用原文' --ticket
evidence-agent inspect <run_id>
evidence-agent approve <run_id> --version <整数版本> --digest <64位摘要> --decision approve
evidence-agent replay <run_id>
```

所有命令支持 `--db` 和 `--corpus`。CLI 拥有数据库文件权限即可执行审批，因此不要让未经授权的用户读写状态目录。直接 run 命令遇到 SIGKILL 后，可启动 `serve` 让 dispatcher 自动接管未完成运行；无须重复创建任务。

## Docker Compose

随附 Dockerfile 和 Compose 配置，需本机有 Docker。镜像尚未在本交付环境构建验证。

创建仅限自己读取的 `.env`，设置两个**不同**的随机 token：

```bash
umask 077
python3 - <<'PY' > .env
import secrets
print('AGENT_API_TOKEN=' + secrets.token_urlsafe(32))
print('AGENT_APPROVAL_TOKEN=' + secrets.token_urlsafe(32))
PY
docker compose up --build -d
docker compose logs -f agent
```

Compose 默认只向宿主机回环地址映射 8090，容器内使用 `0.0.0.0`，因此必须设置两个 token。打开页面，展开“访问凭据”，输入普通和审批 token。浏览器只在内存保存，刷新后需重新输入。

Docker Compose 会读取 `.env` 的变量；这与 Python 应用不会自动读取 `.env` 不冲突。若要启用真实模型，在同一 `.env` 增加 `OPENAI_API_KEY`、`OPENAI_MODEL`，重新创建容器。不要提交此文件。

镜像以非 root 用户运行，根目录只读，数据库持久化到命名卷，`/tmp` 为临时目录。`docker compose down` 保留数据；`down -v` 会删除数据库卷，避免用于需要保留的运行。

## Linux / systemd

部署目录示例 `/opt/evidence-agent`，创建专用非登录用户 `evidence-agent`；只赋予 `/var/lib/evidence-agent` 写权限。将源码、已安装的 `.venv` 和 `corpus/` 放入部署目录。参考 `deploy/evidence-agent.service`，安装后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now evidence-agent
sudo systemctl status evidence-agent
```

可选环境文件 `/etc/evidence-agent.env` 应归 root 且权限 600。不要把真实凭据放入 unit 文件或命令行参数。示例保持回环监听，远程使用应放在 HTTPS 反向代理后；不要将共享 token 通过明文 HTTP 传输。

## 代理与容量

只启动一个 ASGI 进程（CLI 固定 `workers=1`），内部最多两个 worker。不要用多副本共享网络文件系统上的 SQLite。SSE 代理需关闭缓冲、允许长连接，并保留 Authorization；浏览器使用 `fetch` 读流以携带 header。

默认限制：16 个活跃任务、30 步上限、6 个已读片段、256 KiB checkpoint、64 KiB HTTP 请求、最多 100 份 50 KB 的 Markdown。等待审批也占活跃名额；应及时取消过期/放弃的运行。

`/healthz` 检查数据库 schema 可访问及 dispatcher 是否仍在运行；异常返回 503。它不证明模型服务可用或任务结果正确。尚无自动重启失效 dispatcher 的应用内逻辑，使用进程监督并监测健康状态。

## 费用与超时

真实 provider 使用 SDK 超时 20 秒、最多两次 SDK 重试；单轮外层最多等待 65 秒，并受剩余墙上时间约束。当前 token 预算只控制后续调用，单次 `max_output_tokens` 受剩余已观测预算约束，但输入 token 和失败重试未必已经计入。

需要硬费用控制时，应同时配置 API 项目预算/限额、网关限流和账户告警，并测量具体服务商行为。关闭/超时/取消也不保证已经发出的模型请求停止计费。

## 资料更新与备份

替换 `corpus/*.md` 后重启导入。新任务使用新快照，旧任务与回放继续使用原快照。导入路径属于管理员配置，不提供文件上传、URL 抓取、PDF 解析或自动同步接口。

SQLite 在线备份应使用 backup API，不能只复制活跃的主 `.db` 而遗漏 WAL：

```bash
python3 - <<'PY'
import sqlite3
with sqlite3.connect('data/agent.db') as source:
    with sqlite3.connect('agent-backup.db') as target:
        source.backup(target)
PY
```

定期在独立目录恢复并执行 `PRAGMA integrity_check`、查看运行与回放。数据库含任务、证据与报告，应按资料敏感程度保护备份。当前未实现自动保留/清理策略；事件、幂等 key 和旧资料会持续占空间。不能只清理旧 chunks，否则历史快照和回放会失效。

## 故障处理

| 现象 | 检查与处理 |
| --- | --- |
| OpenAI 选项不可用 | 确认服务进程读取了两个模型变量，再重启 |
| `provider_or_runtime_error:*` | 检查模型权限、网络与模型支持能力；不把包含密钥的异常粘贴到公共 issue |
| 崩溃后仍显示 running | 等待原租约最多约 90 秒过期，dispatcher 会接管 |
| `wall_time_limit` | 停机时间也计入墙上时间；重建任务或适当调整预算 |
| 审批 409 | 刷新版本、核对摘要；过期则取消并创建新运行 |
| 429 | 取消已放弃的待批任务，释放全局活跃名额 |
| 健康检查 503 | 检查数据库目录、磁盘与 dispatcher，修复后重启服务 |

系统时钟跳变会影响租约和审批有效期。生产环境需要稳定时钟及相应监控；不要把本项目的租约用于不可逆设备控制。
