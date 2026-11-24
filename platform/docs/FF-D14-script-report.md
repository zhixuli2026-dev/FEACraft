# FF-D14 后处理脚本与报告图件

在算例文件树保存 `D-post/postprocess.py`，然后在「作业 → 新建任务 → 单作业」选择「后处理脚本」。提交前可核对运行命令、算例、时限和日志上限。命令从算例根目录在 WSL 隔离沙箱启动，使用现有作业日志、取消和版本化发布。脚本可以读取算例内求解结果，输出 CSV、`D-post/summary.md` 和出版图。一个算例可以同时使用脚本和 `caseDict` 驱动的提取任务。

WSL `vibeflow-linux` 需要可用的 `python3`。本次验收使用 Ubuntu 的 `python3-numpy` 和 `python3-matplotlib`，分别为 1.26.4 和 3.6.3。Matplotlib 默认使用非图形 `Agg` 后端。首次下载时旧索引指定的 Pillow 包返回 404，刷新索引后安装通过。该发行版存储在 D 盘。[首次下载日志](../../tmp/FF-D14-script-report-20261122/wsl-packages.log) · [重试日志](../../tmp/FF-D14-script-report-20261122/wsl-packages-retry.log)

## 把脚本图件带入报告

在 `caseDict` 的 `report` 块加入 `resultFigures`，然后从报告面板组稿和构建。报告会按配置嵌入算例 PNG/JPEG，并与现有的版本化后处理图件一起列在结果章节。

```text
report
{
    resultFigures
    {
        thermalProfile
        {
            image "D-post/figures/热响应 profile.png";
            data "D-post/data/热响应 cells.csv";
            title "单元温度分布";
        }
    }
}
```

`resultFigures` 最多配置 24 项。`title` 和 `data` 可省略。`image` 位于 `D-post/figures/`，`data` 位于 `D-post/data/`。配置支持中文、空格和子目录。组稿封存图件和 CSV 的字节，结果章节及证据清单保存其路径与 SHA-256。算例图件沿用现有后处理就绪门禁，并标为 `caseFile` 来源。PDF/SVG 可作为脚本的独立算例文件保存。嵌入 HTML/DOCX 时选择对应的 PNG 或 JPEG。

## 实测

真实 OpenFOAM 稳态导热的 80 个单元经 NumPy 读取得到约 350 K 平均温度。脚本用 Matplotlib 生成 2100×1200 PNG、PDF、SVG 和 80 行 CSV，文件名包含中文和空格。真实 Pandoc 在 HTML 与 DOCX 都嵌入脚本 PNG 和原有提取图件，并核对原图 SHA-256。文件系统也验证脚本失败或取消后的暂存文件不会进入算例、跨账户访问返回 404、历史报告恢复及冷启动重跑。[文件系统回执](../../tmp/d14-script-report-fs-fabtu2sj/d14-result.json)

改变当前 CSV 的一个字节后，报告草稿标为过期，构建在创建作业前返回 `input_stale`。恢复原字节后，草稿重新有效，历史 HTML 摘要一致。Edge 实际提交脚本，双击只创建一个作业，刷新未重复提交。在 960、650、390 像素宽度检查了启动入口。[CSV 来源变化回执](../../tmp/FF-D14-script-report-20261122/csv-staleness.json) · [Edge 回执](../../tmp/FF-D14-script-report-20261122/browser-result.json) · [作业入口](../../tmp/FF-D14-script-report-20261122/edge-python-task.png)

未配置类型化提取的算例也通过仅用脚本图件的组稿。HTML 与 DOCX 嵌入浏览器提交作业产生的原始 PNG。[纯脚本报告回执](../../tmp/FF-D14-script-report-20261122/script-only-report.json)

认证 SeaweedFS 通过实际运行、脚本及类型化图件共同嵌入、路径越界拒绝、跨账户访问拒绝、失败与取消后的暂存保护、历史报告回读和冷启动重跑。模型调用为 0。[SeaweedFS 回执](../../tmp/d14-script-report-s3-qiiwess0/d14-result.json) · [验收汇总](../../tmp/FF-D14-script-report-20261122/validation.json)

如果要回退新入口，可移除 `report.resultFigures` 后重新组稿，并撤回任务启动器的脚本选项。已封存的历史报告可从版本读取入口按原发布查看。
