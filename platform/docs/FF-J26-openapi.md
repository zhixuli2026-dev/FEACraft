# FF-J26 OpenAPI、Swagger 和报告读取诊断

`/docs` 读取 `/openapi.json`。报告派生产物与 LaTeX 原件分别登记 GET 和 HEAD，各自产生唯一的 `operationId`。HEAD 的 200 响应描述没有正文。两个 GET 的 409 都公开诊断结构，包含 `detail.code`、`detail.message` 以及可能存在的文件路径和修订号。 [报告路由](../server/app/routers/reports.py) · [OpenAPI Operation Object](https://spec.openapis.org/oas/v3.1.0.html#operation-object)

隔离 API 实际返回 Swagger 页面和 OpenAPI 3.1 文档，检查 276 个路径与 321 个操作，无重复操作标识。用正式报告草稿入口生成 2,491 字节的已发布原件。GET 和 HEAD 都返回 200，文件版本和长度一致，HEAD 正文为空。区间 GET 返回 206，内容与原件逐字节对应。传入过期的文件版本返回 409 和 `file_changed_during_read`，与公开诊断结构一致，匿名请求返回 401。缺少 LaTeX 发布时，GET 和 HEAD 均返回 404。 [实际验收](../../tmp/openapi-j26-80x84yik/openapi-result.json) · [实际生成的契约](../../tmp/openapi-j26-80x84yik/openapi.json)

首次试验把可编辑草稿的别名误作已归档原件的路径。复验使用发布记录给出的 `source_path`，保留初次失败回执。验收入口经审查，模型调用为 0。 [首次回执](../../tmp/openapi-j26-0ji9x1d3/openapi-result.json) · [验收入口](../server/openapi_contract_acceptance.py)
