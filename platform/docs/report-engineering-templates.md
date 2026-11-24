# 自动工程报告模板与实际求解验收

2026-11-18，FF-E02，WP09 / R4 / T07。自动工程报告现在与手写 Markdown 报告共用本地 DOCX 模板、CSS 及嵌套资源解析。模板配置来自 caseDict；工程正文仍由真实 caseDict、网格和后处理证据组织。模型调用 0，模拟 LLM 0。

## 使用

在算例中上传 DOCX 模板和样式文件，通过现有 caseDict 编辑器保存：

```foam
report
{
    title "项目工程报告";
    referenceDoc "templates/reference.docx";
    css ("styles/main.css" "styles/extra.css");
}
```

上例对应 `E-report/templates/reference.docx`、`E-report/styles/main.css` 和 `E-report/styles/extra.css`。路径沿用报告已有规则：优先相对 `E-report`，再查算例根目录。CSS 也可只写一个字符串；空配置沿用默认报告。CSS 的相对导入以该样式表所在目录解析。文件不得越出算例，远程资源须先保存到算例。

现有工作流 `target: report`、`parameters.report_mode: compose` 直接生成工程 HTML/DOCX，不改写用户的 `E-report/report.md`。现有“生成报告草稿”入口则将配置写成 YAML 元数据，保留可编辑正文；后续“构建 HTML/DOCX”使用同样的模板。覆盖草稿仍需原有显式覆盖及版本条件，并保留旧稿备份；本次没有新增平行构建接口。

准备阶段解析模板和嵌套 CSS，封存实际选中的文件及候选路径；提交阶段再次核对。修改模板或嵌套样式使旧发布过期，新构建产生新版本，历史原件仍可按发布 ID 下载。缺失依赖在创建报告作业前拒绝。普通 Markdown 路径保持已有快照、资源、取消和版本行为。

## 本批实际证据

入口 `server/report_engineering_templates_acceptance.py`，最终功能运行审查 v5：

- `tmp/engineering-templates-n45hy6bq/benchmark.json`：9 个真实原生作业成功，全部停止凭据核验；无效配置、完整工程模板链、缺失依赖拒绝、模板/CSS 变更与历史原件、可编辑草稿及备份导出，共 5 项检查通过。
- 实际 CadQuery 几何、OpenFOAM blockMesh/checkMesh/laplacianFoam、VTK 提取和 Pandoc 输出，未编造场文件或指标。10×4×2 共 80 单元；独立按实际网格中心读取求解器双精度温度，与 `T(x)=300+100x` 比较，L2 和最大绝对误差均为 0 K。
- VTK 体平均为 349.9999997764826 K，满足原 10⁻⁶ K 绝对容差。HTML 表格和 DOCX 表格均按数值校验，不要求浮点数显示为字符串“350”。verification 成立，未声明实验 validation。
- 模板化的直接工作流、模板变更后的新版本均保持手写稿原字节。显式生成草稿后，旧稿备份原字节和新报告的模板输入哈希均核验。

入口 `tools/report-engineering-s3-acceptance.py`，审查 v6：

- `tmp/engineering-s3-3497668db5/result.json`：从上述已成功求解的封存目录上传原始网格与场文件到独立 S3 算例，逐文件记录来源哈希；S3 worker 重新执行真实 VTK 后处理，随后生成草稿和 HTML/DOCX，两项检查通过。
- 算例 3，作业 5/6/7，SQL 实际 worker 归属匹配独立 worker 容器；API/worker 使用既有分离缓存卷。不是跨物理主机验收。
- `tools/report-engineering-render.cjs` 与同目录 `render-result.json`：Chromium 打开实际下载的 HTML，标题色 `rgb(19, 92, 114)`、均温及 K 单位核对通过，外部 HTTP 请求 0、页面错误 0。实际截图 `engineering-report.png` 已检查。

Windows Python 3.11.7 / Pandoc 3.9.0.2；Docker Python 3.11.18 / Pandoc 3.1.11.1，版本来自本批报告工具链原件。

## 保留的失败与修复

`engineering-templates-mn5d3atf` 保存准备阶段空输入清单错误；已修复只对非空已知清单调用清单函数。`engineering-templates-pyn1g2i2` 保存 CSS 把 OpenFOAM 外层括号当作文件名的错误；现按实际列表语法去掉外层括号。

`engineering-templates-yfdn584p` 是测试错误：caseDict 裸数字解析为字符串，不能作为类型错误输入；改为实际非法字典块。`engineering-templates-8d7w8w8a` 已生成真实报告，但验收错误地按字面“350”检查浮点结果；改为原数值容差检查，同时正确读取自包含 CSS。上述失败均留存，未伪装成通过。

最终兼容修正保留空的资源解析记录，避免无模板 compose 的准备/提交记录不同。该变更不改变有模板路径；原始 `server/thermal_conduction_smoke.py` 真实默认报告闭环通过，证据 `tmp/cae-registered-conduction-73cpb5qu/benchmark.json`，6个原生作业成功并核验停止。精确源码分别保存在 `tmp/sources-FF-E02-engineering-templates-v1…v7-20261118.zip`，未以新源码覆盖旧证据。

## 部署与回退

本批只改后端报告和 caseDict 配置校验，无数据库迁移或前端依赖变更。FS 网站 `http://127.0.0.1:62883`，S3 网站 `http://127.0.0.1:57700`，API/worker 最终镜像 `vibeflow/api:engineering-templates-v3-20261118`。部署详情及源码核验以 `tmp/mainline-engineering-templates-evidence/validation.json` 为准。

回退 API/worker 可使用此前 `vibeflow/api:srcset-v2-20261117`，保留数据库、文件、S3 和原件卷。旧版不识别工程模板配置，需移除该配置后重新生成工程报告；已发布的历史原件仍保留。不得把回退解释为旧版本具备本批新增能力。

逐项交付结论见 [报告交付核对](report-delivery-reconciliation.md)。普通 Markdown 的模板、复杂版式、权限、取消和恢复采用已有实际证据；本批补其工程正文组合，不无限扩大退出条件。跨物理主机及全项目最终门槛仍由 N7-01 / N7-06 承担。
