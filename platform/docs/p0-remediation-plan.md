# P0 修复实施计划（2026-10-24）

> 来源：`feature-gap-audit.md` 的 P0 项。原则：先修确定的行为缺陷，按 RED→GREEN→回归执行，不夹带 P1/P2 大型功能。

## 当前约束

- 没有 Git 仓库，回滚按修改文件逐项还原，不执行 commit。
- 新测试先在旧实现上复现失败，再修改实现；不能只凭代码推断写完成。
- 每次修复同步 `progress.md` 和本文件状态；旧文档中的“已闭环”只有在当前验证通过后才改。
- 外部安装包、WSL/OpenFOAM 环境变更、实库 stamp/upgrade 另行明确授权，不自动执行。

## P0-1 派生保留初始 `0/` 场并验证可运行

**旧问题**：`jobs.py` 的时刻目录正则同时排除 `0/` 和 `0.x/`，导致派生算例缺初始场。

**修改范围**：`server/app/routers/jobs.py`、`server/jobs_smoke.py`。

**TDD**：
1. 在派生源中保留 `0/U`、`0/p`，增加 `0.1/`、`1/` 结果目录、日志和 `.foam`。
2. 先断言 `0/U`、`0/p` 被复制，结果时刻/日志/.foam 被排除；旧实现应失败。
3. 修正时间目录过滤，再运行派生工况的 `icoFoam`，证明不是“只有目录复制”。
4. 重复派生同名仍应为 409。

**状态**：完成。旧实现先失败于缺失 `0/U`；修正后 `0/U`、`0/p` 保留，`0.1/1/`、日志和 `.foam` 不复制，派生工况实际运行 `icoFoam` 成功并产生 `0.1/U`。同名重复派生仍由既有 409 路径处理。

## P0-2 preview 不得执行导出钩子

**旧问题**：`geo_script.py` 在 preview/export 分支判断前调用 `export_geometry(ctx, objects)`，preview 会产生导出副作用。

**修改范围**：`server/app/services/geo_script.py`、新增/扩展几何测试。

**TDD**：preview 前构造含导出钩子的脚本，先证明旧 preview 会产出 STEP/STL；修复后 preview 只允许 scene.json，显式 export 才生成 STEP/STL/manifest。补充 preview/export 分支与返回结构回归。

**状态**：完成。旧 preview 先产生 `export-hook.txt` 证明副作用；移动钩子到 export 分支后，preview 不再产生标记文件，显式 export 仍产生标记、STEP/STL/manifest。

## P0-3 caseDict 编辑闭环与块内保真

**旧问题 A**：Monaco 保存走普通 `/file`，后端拒绝 caseDict。

**修改范围 A**：`web/src/store.ts`、必要的 EditorPanel、浏览器 E2E。

**TDD A**：在 Monaco 修改 caseDict 后保存，验证走 `/caseDict`、文件与 DB 镜像一致、workflowState 可保留，普通文件仍走 `/file`。

**旧问题 B**：`set_block` 重写目标块全部内部文本，目标块内注释/格式丢失。

**修改范围 B**：`server/app/services/casedict.py`、`casedict_sync_smoke.py`、必要的 CasePanel。

**TDD B**：目标块内同时含注释、嵌套块和待修改键。只替换目标键，其他键/注释/格式保持；找不到块/键时明确错误，不静默改坏文件。先写块内注释失败用例，再实现局部替换。

**状态 A：完成。Monaco 保存已改走 `/caseDict`，其他文件仍走 `/file`；保存后刷新 case 镜像。浏览器验证 marker 写入成功、revision 增加且原文恢复。**

**状态 B：完成。`set_block` 已改为 token 级局部替换，保留目标块内注释、其他键和 workflowState；全量删键、局部改键、缺失块/键报错均通过。**

## P0-4 普通二进制下载按钮

**旧问题**：普通 `<a href>` 不带 Bearer，DOCX 等二进制下载 401。

**修改范围**：`web/src/panels/EditorPanel.tsx`、下载辅助代码、浏览器 E2E。

**TDD**：普通文本/二进制链接改为带 Bearer 的 fetch→Blob→objectURL 下载，处理文件名、释放 objectURL 和 HTTP 错误。验证 DOCX 实际下载成功，未登录不返回文件内容。

**状态**：完成。二进制按钮已改为 Bearer fetch→Blob→objectURL，带文件名并在下载后释放；浏览器实测 DOCX 下载成功，未授权请求仍由 raw 鉴权拒绝。

## P0-5 阶段门控与 JobsPanel runtime

**旧问题 A**：`errors.summary.open` 只提示，二次点击仍可推进；服务端没有硬门控。

**修改范围 A**：`server/app/routers/files.py`、`server/app/services/casedict.py`、caseDict 测试、CasePanel。

**TDD A**：`errors.summary.open>0` 或 blocking 时，服务端拒绝 workflowState 推进并保留原状态；结清后允许。字段齐备度、网格质量门控放 P1，不在本项虚写完成。

**旧问题 B**：JobsPanel 只能默认 Windows 提交，不能选择 WSL。

**修改范围 B**：`web/src/panels/JobsPanel.tsx`、`server/jobs_smoke.py` 或 UI 探针。

**TDD B**：增加 runtime 选择并验证 `runtime: windows|wsl` 都正确传递，WSL 命令在指定发行版运行，失败时显示真实错误。

**状态 A：完成。统一写入函数在状态真正变化时检查 `errors.summary.open/blocking`，存在即拒绝；REST 返回 409，Agent 返回失败结果，文件和 revision 不变。字段齐备度和网格质量门控留 P1。**

**状态 B：完成。JobsPanel 增加 Windows/WSL runtime 选择，浏览器实际提交 WSL `pwd` 并返回 `/mnt/` 路径。**

## P0-6 OpenFOAM 开发环境与 UDF 前置

**旧问题**：OpenFOAM 为 1912 而非 v2406；`wmake` 和 `foamEtcFile` 不可用，环境脚本报缺件。

**修改范围**：WSL 环境与 UDF 最小验证，不先写“已安装完成”。

**状态**：完成。已备份包/APT 清单，使用官方 `https://dl.openfoam.com/add-debian-repo.sh` 配置仓库并安装 `openfoam2406-default`（含 dev/tutorials）。`wmake`、`foamEtcFile` 可用；最小 functionObject 库已编译、链接并通过 `ctypes.CDLL` 加载。新环境下 2D/3D 黄金算例也通过。本项只承担环境前置；最小 functionObject D1–D5/G5 切片已于 2026-10-25 在 P1-1 另行验证，通用 UDF 工作流仍属后续范围。

## 每个修复后的共同验证

- 相关 smoke 与新增负向用例通过。
- 主浏览器 E2E 必须含实际行为断言，不能只截图。
- `npm run build`、Python compile、`schema_diff.py` 不引入新问题。
- 定期复跑全部 21 个后端/专项套件；`quota_smoke` 若仍被工具阻断，明确记为未执行，不凑全绿。

## 状态总表补充

**P0-1 至 P0-6 全部完成。** P0 完成时回归为 20/20；2026-10-25 加入 UDF smoke、授权补装 Alembic 并重跑 migration 后，最新为 21/21。后续 UDF 完整工作流、批量工况、flotherm、PPT、ParaView 长尾、S3/生产部署等按 `feature-gap-audit.md` 的 P1/P2 另行拆解，不在本计划中虚写完成。

## 状态总表

| 项目 | 状态 | 验证结果 |
|---|---|---|
| P0-1 派生初始场 | 完成 | RED 缺 `0/U`；GREEN 初始场保留且 icoFoam 实际可运行 |
| P0-2 preview 副作用 | 完成 | preview 无导出，export 有导出 |
| P0-3 caseDict 编辑/保真 | 完成 | Monaco 保存+块内注释/格式/局部编辑/缺失拒绝全通过 |
| P0-4 二进制下载 | 完成 | 浏览器实际 DOCX 下载+Bearer/Blob/revoke 通过 |
| P0-5 门控/runtime | 完成 | open/blocking 409+revision 不变、结清可推进；WSL 浏览器提交返回 /mnt/ |
| P0-6 OpenFOAM/UDF 环境 | 完成 | 官方 v2406 开发环境+wmake/foamEtcFile；最小库编译/加载+2D/3D golden 通过 |
| 全量回归 | 最新 21/21 | P0 完成时 20/20；新增 UDF 通过，migration 在授权补装 Alembic 后重跑通过 |
