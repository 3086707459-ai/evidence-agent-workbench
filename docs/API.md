# API 使用

默认地址 `http://127.0.0.1:8090`。服务启动后可访问 `/docs` 和 `/openapi.json`。启用鉴权时这些接口也需要 Bearer token，交互页面中的普通链接不会自动携带网页内存中的凭据。

## 路由

| 方法与路径 | 用途 | 额外要求 |
| --- | --- | --- |
| `GET /healthz` | 数据库与 dispatcher 健康 | 无鉴权；异常返回 503 |
| `GET /api/config` | 可用模式信息 | 不返回密钥 |
| `POST /api/runs` | 创建任务 | `Idempotency-Key` |
| `GET /api/runs` | 最近 50 个任务 | 当前单用户工作区 |
| `GET /api/runs/{id}` | 公开运行状态与报告 | 不包含 owner、lease 或完整回执 |
| `POST /api/runs/{id}/cancel` | 取消 | `expected_version` |
| `POST /api/runs/{id}/approval` | 批准或拒绝固定动作 | 审批凭据、幂等 key、版本和 digest |
| `POST /api/runs/{id}/replay` | 创建无模型回放任务 | `Idempotency-Key`；原任务须有报告 |
| `GET /api/runs/{id}/events?after=0` | 一页事件（最多 200 条） | 全局事件 seq 游标 |
| `GET /api/runs/{id}/events/stream?after=0` | SSE 实时事件 | 使用 fetch 以携带 Bearer header |
| `GET /api/runs/{id}/report` | 纯文本 Markdown 报告 | 报告生成前 409 |
| `GET /api/runs/{id}/audit` | 本地事件链完整性 | 不是独立签名审计 |
| `GET /api/tickets` | 最近本地工单 | 当前单用户工作区 |

## 创建与查询

本机无鉴权模式：

```bash
curl -sS http://127.0.0.1:8090/api/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: compare-demo-0001' \
  -d '{"goal":"比较 Atlas 和 Boreal 的离线部署、审计及三年成本，引用原文","provider":"demo","create_ticket":true}'
```

响应含 `id`。复制到以下命令的 `RUN_ID` 后查看状态：

```bash
RUN_ID='替换为响应中的 id'
curl -sS "http://127.0.0.1:8090/api/runs/$RUN_ID"
curl -N "http://127.0.0.1:8090/api/runs/$RUN_ID/events/stream?after=0"
```

`provider` 可选 `demo/openai`；默认预算为 18 步、30,000 已观测 token、180 秒，至少 2 个来源。上限分别为 30 步、100,000 token、600 秒、5 个来源。请求体严格拒绝未知字段，限制 64 KiB，goal 5–2,000 字符。

token 数是已返回的 SDK usage，不是余额或计费硬上限；单次模型输入和失败重试的用量可能使实际消费超出预算。

## 审批

先获取 run，检查 `approval.payload` 的具体正文，再使用该响应的 `version` 和 `approval.digest`。不要依靠旧页面中的版本，不要只看模型建议就批准。

```bash
curl -sS "http://127.0.0.1:8090/api/runs/$RUN_ID/approval" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: approve-demo-0001' \
  -d '{"expected_version":替换为当前整数版本,"digest":"替换为完整64位摘要","decision":"approve"}'
```

这个模板需要替换占位符后才是合法 JSON。拒绝用 `"decision":"reject"`。审批有效期 1 小时；过期、版本变化或摘要不同返回 409。批准后短暂处于 `approved`，后台投递成功才到 `completed` 并出现 `ticket_id`。

已配置 API token 时，普通请求添加 `Authorization: Bearer <AGENT_API_TOKEN>`；审批添加 `Authorization: Bearer <AGENT_APPROVAL_TOKEN>`。独立审批 token 无法访问普通 API，普通 token 无法完成审批。未设置独立审批 token 的本机模式会使用普通 token。

当前角色是两个共享凭据，不含实名身份、细粒度 RBAC 或租户隔离。

## 错误处理

| 状态 | 含义 | 调用方处理 |
| --- | --- | --- |
| 400 | 真实模型服务端变量缺失 | 配置服务端后重试 |
| 401 | 缺少/错误凭据 | 使用对应角色凭据 |
| 404 | 任务不存在 | 检查 ID |
| 409 | 旧版本、幂等冲突、报告未就绪或不允许的状态 | 先重新获取状态 |
| 413 | 请求体超过 64 KiB | 缩小请求 |
| 422 | 字段、范围或 key 无效 | 按 schema 修正 |
| 429 | 16 个活跃任务容量耗尽 | 完成/取消部分任务后重试 |

运行内错误不一定是 HTTP 错误：模型调用、工具反馈和预算结果显示在运行的 `status/error/tool_errors` 与事件中。模型 SDK 错误只保存异常类别，避免将敏感请求上下文直接写进事件。
