# FF-E06 文献检索与引用插入

2026-11-19。原合同覆盖读取算例内的 `E-report/latex/refs.bib`，通过 Crossref 和 arXiv 查询公开书目，以及将文献追加到文件后向编辑器插入 `\cite{…}`。Crossref 返回 JSON 元数据，arXiv 关键词查询使用 Atom 搜索接口，精确编号使用 `id_list`。[原审计 E06](feature-gap-audit.md) · [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/) · [arXiv API 手册](https://info.arxiv.org/help/api/user-manual.html)

在线收录先读取 `refs.bib` 和当前版本，再用该 SHA-256 有条件追加。原字节和条目评论保留。同一 DOI 或 arXiv 条目复用已保存的键，碰到不同文献占用同名键时生成未用过的后缀。收录成功后尝试向原 Monaco 选区写入，正文或光标变化时可从已收录条目继续。上游错误和写入冲突会显示原因，保留已检索内容以便重试。[编辑入口](../web/src/panels/CitationPanel.tsx) · [公开数据适配](../server/app/routers/cite.py)

## 实际验收

Edge 从现有 `refs.bib` 插入 `localRef`。Crossref 实时返回 DOI `10.1113/jphysiol.1952.sp004764`，由于已有不同文献占用 `hodgkin1952`，正式收录为 `hodgkin1952-2`。同一 DOI 再引用时只修改正文。实际并发写入使浏览器的旧版本追加收到 HTTP 409 `file_conflict`，另一次编辑保留，正文也未插入悬空引用。用户重试后按新文件版本收录成功。没有打开正文时，在线收录按钮提示打开文件，书目文件保持原字节。[浏览器回执](../../tmp/e06-citation-2cm9yc8t/citation-browser.json)

arXiv 通过官方精确编号接口实时返回 `1706.03762v7`，实际保存 `vaswani2017` 并插入正文。初次标题关键词查询收到上游限流，界面反馈 503，现有文件和正文保持原字节。随后两个关键词分别返回 502 和 503。最新复验通过同一个正式检索入口提交 `implicit large eddy simulation`，返回 HTTP 200 和八条真实结果，第一条编号 `2212.08432v1`。关键词、精确编号、书目写入和失败恢复均已有实际证据。[arXiv 查询与间隔说明](https://info.arxiv.org/help/api/user-manual.html) · [浏览器结果](../../tmp/e06-citation-2cm9yc8t/citation-result.json) · [旧关键词回执](../../tmp/e06-arxiv-keywords-og070ycv/keyword-results.json) · [关键词成功回执](../../tmp/e06-arxiv-keywords-s9qv8pv0/keyword-results.json)

完成正常保存后，以实际 API 两次读回 `refs.bib` 和正文原字节，SHA-256 与浏览器记录一致。未登录检索返回 401，未获授权的账号读写算例文件均返回 404。独立复核隔离 API 端口已关闭，四个登记的 Windows/WSL 后台进程身份均已退出。模型调用 0 次。构建前后 C 盘均为 22.20 GiB，Docker 构建缓存为 0。[工作台截图](../../tmp/e06-citation-2cm9yc8t/citation-workbench.png) · [独立退出证据](../../tmp/e06-citation-2cm9yc8t/cleanup-independent.json)

## 复验入口

从 `platform/server` 启动已登记的专用脚本。它使用真实 Edge、正式 FastAPI 路由、独立 SQLite 和文件存储，以及两个提供方的公开元数据。[验收脚本](../server/citation_browser_acceptance.py)

```powershell
& .venv/Scripts/python.exe -B -X utf8 citation_browser_acceptance.py `
  --dist D:/vibeflow_reverse/tmp/e06-citation-dist-20261119 `
  --browser-python C:/ProgramData/anaconda3/python.exe --channel msedge
```

只确认公开关键词接口时，改用 `--arxiv-keyword electron --arxiv-keyword "quantum criticality"`，它通过正式检索入口发送请求并记录实际 HTTP 状态。
