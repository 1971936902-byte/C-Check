# 后端适配修改日志（2026-06-03）

## 背景

根据前端页面与 `frontend/src/api/client.ts` 的实际调用方式，对后端接口做静态契约检查。本次未在 Windows 本地部署后端服务，仅通过代码审查与自动化测试验证。

## 修改内容

- 管理员通过 `/api/reviews`、`/api/reviews/{id}`、`DELETE /api/reviews/{id}` 可查看和管理全平台审查任务，匹配前端历史页面的管理员全局视角。
- 管理员通过 `/api/reports/{id}` 可查看全平台报告，支持从历史记录进入他人任务报告详情。
- 管理员编辑模型节点时，如果请求未提交 `api_key` 字段，后端保留已有密钥，避免前端编辑保存时意外清空模型服务凭据。
- 后端测试隔离了 Celery 分发，避免 Windows 本地静态接口测试误连 Redis。
- 补充管理员跨用户任务/报告访问测试，以及模型密钥保留测试。

## 验证

```powershell
python -m pytest backend/tests -q
```

结果：`78 passed`。
