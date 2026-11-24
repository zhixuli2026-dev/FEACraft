# FF-E07 Semantic Scholar 文献源

2026-11-25，用户决定暂不接入，FF-E07 移出本次交付。下文留作历史记录。 [当前交付范围](full-function-delivery-plan.md#21-最新范围规则)

文献工作台的 Semantic Scholar 来源支持题名和关键词检索，也支持直接输入 DOI、DOI 链接、arXiv 编号、Semantic Scholar Paper ID 或 `CorpusId`。标识符使用官方单篇接口，关键词使用相关性检索接口。精确编号找不到时显示空结果。连续限流返回 503 并携带 `Retry-After`；如果上游要求的等待时间超出本次自动重试窗口，界面直接返回给用户稍后重试。书目条目优先保留可用的 DOI 或 arXiv 标识；其余条目使用 Semantic Scholar 的 Paper ID，供后续插入去重及跳转。[应用入口](../server/app/routers/cite.py) · [文献工作台](../web/src/panels/CitationPanel.tsx) · [Academic Graph API](https://api.semanticscholar.org/api-docs/)

真实 Edge 在正常工作台选择 Semantic Scholar，输入 DOI 链接后收到官方 HTTP 200。实际文献为 D. Crawford 的《Forum》（2008），书目写入 `crawford2008`，对同一结果连续插入两次后，两个 `\cite{crawford2008}` 均已保存，`refs.bib` 只增加一个条目。读回核对了正文和书库 SHA-256。匿名检索返回 401，未授权账号访问书库返回 404。[浏览器与文件回执](../../tmp/semantic-citation-e07-iqer6yb1/semantic-citation-result.json) · [工作台实图](../../tmp/semantic-citation-e07-iqer6yb1/semantic-reference-workbench.png)

本轮通过同一正式入口实际查询 `OpenFOAM` 和 `implicit large eddy simulation`，上游限流均由应用返回 503，页面提示错误，已经保存的引用和书目保持原字节。关键词在线返回条目的成功分支仍待复验。另有三项普通 HTTP 协议测试验证退避、超长等待返还、缺 DOI 元数据和不存在的 Paper ID。这些协议测试与两条实际外网结果分别记录。本项模型调用为零。[工作台限流回执](../../tmp/semantic-citation-e07-iqer6yb1/semantic-browser.json) · [第二次实际关键词查询](../../tmp/semantic-actual-keyword-zaf5agdr/semantic-keyword.json) · [验收入口](../server/citation_semantic_acceptance.py)

2026-11-23 用独立本机 API 查询 `computational fluid dynamics OpenFOAM`，官方服务仍限流。应用返回 503 和 `Retry-After: 4`，待真实关键词有结果后完成验收。 [当次回执](../../tmp/semantic-keyword-current-bentvcxx/semantic-keyword.json)

03:24 再以 `OpenFOAM heat transfer` 查询，正式检索接口仍返回 503，并保留 `Retry-After: 4`。 [实网关键词回执](../../tmp/semantic-keyword-current-fa7r0m2l/semantic-keyword.json)

2026-11-24 16:36，直接查询官方 relevance 接口，`computational fluid dynamics` 返回 HTTP 429。已保存实际响应，关键词在线成功仍待验证。 [官方响应状态](../../tmp/ff-e07-actual-keyword-20261124-1630/result.json)

20:27，隔数小时查询同一官方 relevance 接口，依然返回 HTTP 429，服务端未送回 Retry-After。官方文档说明未认证访问使用共享额度。当前结果保留为上游限流，当次没有模型调用。[本轮 HTTP 回执](../../tmp/ff-e07-keyword-20261124-2030/result.json) · [官方访问额度说明](https://www.semanticscholar.org/product/api)

应用服务可通过私有 `VIBEFLOW_SEMANTIC_SCHOLAR_API_KEY` 配置授权密钥。该值只发送到官方 HTTPS 接口的 `x-api-key` 请求头，拒绝上游重定向。四项独立 HTTP 协议检查通过，包括无密钥情况下的原重试、有密钥时的请求头和配置序列化脱敏。实际持证关键词仍待验证。[协议回执](../../tmp/ff-e07-keyword-20261124-2030/protocol/summary.json)

23:14 单次直查 `OpenFOAM heat transfer`，官方相关性接口仍为 HTTP 429。返回提示申请访问密钥。模型调用 0 次。 [实际响应](../../tmp/ff-e07-keyword-20261124-2314/result.json)
