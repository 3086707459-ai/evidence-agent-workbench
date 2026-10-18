# 交付验证记录

验证日期：2026-10-18。以下结果来自实际执行，不把未执行的检查标记为通过。

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| Python 单元与集成测试 | 61 passed | `tests/`，含 CLI 子进程、SDK 协议、API 与崩溃恢复 |
| Python 代码行覆盖率 | 96.00%（937 / 976 条语句） | pytest-cov，启用 subprocess coverage |
| Ruff lint 与格式 | 通过 | `ruff check`、`ruff format --check` |
| JavaScript 语法 | 通过 | `node --check src/evidence_agent/static/app.js` |
| 工程评测 | 8 / 8 通过 | `examples/evaluation-demo.json` |
| 实际 HTTP 服务 | 通过 | CLI 启动 uvicorn，真实 TCP 请求验证完整流程 |
| HTTP 审批、SSE 与回放 | 通过 | `scripts/smoke_http.py`、`examples/http-smoke.json` |
| 依赖一致性 | 通过 | `pip check`，版本见两个 lock 文件 |
| Python wheel 构建 | 通过 | setuptools 构建完成，检查包含三个静态资源 |
| 真实模型语义质量 | 未执行 | 无模型密钥；只有官方 SDK 的 mock transport 协议测试 |
| 浏览器交互/响应式视觉验收 | 未完成 | 已验证静态资源 HTTP 返回与 JS 语法，浏览器无法连接当前临时服务 |
| Docker 构建 / systemd 启动 | 未执行 | 当前环境未提供 Docker/systemd 运行条件 |
| GitHub Actions | 3 / 3 环境通过 | Python 3.11、3.12、3.13 均通过 lint、61 项测试和 8 项评测；[查看运行记录](https://github.com/3086707459-ai/evidence-agent-workbench/actions/runs/39085917091) |

本机环境：Python 3.12.14，FastAPI 0.143.3，OpenAI Python SDK 2.54.0，pytest 9.1.1。覆盖率表格为本机结果；CI 中 Python 3.11 为 935 / 976，3.12 与 3.13 为 937 / 976，均约 96%。测试有一条上游 Starlette/httpx 弃用提示，未影响结果；升级依赖时应复测 TestClient 兼容性。

## 重新验证

```bash
python -m pip install -r requirements.lock -e '.[dev]'
ruff check src tests scripts
ruff format --check src tests scripts
pytest --cov=evidence_agent --cov-fail-under=85 --cov-report=term-missing
evidence-agent evaluate --output out/evaluation.json
node --check src/evidence_agent/static/app.js
```

另开终端运行 `evidence-agent serve` 后，使用：

```bash
python scripts/smoke_http.py
```

smoke 脚本会创建演示任务、批准一条**本地**工单并创建回放，使用虚构的随附资料；它不调用真实模型，也不会写外部工单。若服务启用了凭据，将相同的 `AGENT_API_TOKEN`、`AGENT_APPROVAL_TOKEN` 导出给脚本。脚本直连指定服务，不使用系统代理。

## 结果解读

96% 是已执行代码行覆盖率，不是分支覆盖率、安全证明或业务正确率。工程评测检查的是确定性行为；虚构 fixture 中的三年费用为 1920 与 1860，不是实际设备报价。

SIGKILL 测试确实启动子进程并发送终止信号，再以可控时钟跨过租约期限恢复；它验证 prepared decision 后的恢复窗口。未覆盖真实主机断电、磁盘硬件故障或所有文件系统持久性差异。
