# FF-D24 后处理证据和报告就绪状态

在作业面板选择一个已完成的后处理作业，然后展开「后处理证据」。这里显示所选作业发布的就绪状态、阻碍、指标及单位、参考来源、相对 L2 和容差。历史发布保留提取时的判断，单独标出版本状态。Verification 和 Validation 来自已发布的证据字段。

作业面板空间较小时，可拖动它上方的分隔条扩大视图。

图件清单从同一发布的指标与归档清单取得。PNG 预览和 PDF、SVG、PNG、CSV 下载都按原发布取件并核对摘要。切换作业时取消旧请求，刷新证据时重新核对所选版本。生成报告的参考表也列出证据类型和容差。

三条报告入口沿用同一后处理就绪门禁。普通组稿、Pandoc 构建和 LaTeX 编译都会核对当前结果与已发布的来源。历史报告和 PDF 可以按原发布 ID 查看。LaTeX 的绝对路径图件通过 `input_aliases` 指向算例内的文件，编译结果保存实际读取的源映射和图件摘要。[出版图表](FF-D23-publication-charts.md) · [LaTeX 输入冻结](latex-absolute-resources.md)

## WSL 工具链探测

同一台机器直接以 WSL 非特权用户探测 TeX 工具链耗时 0.79 秒。前两次文件系统编译的记录显示，注册子进程从预留到授权运行用了约 38 至 41 秒，45 秒期限触发时还未拿到完整工具链结果。客户端和执行器改用同一份探测时限，WSL 上限为 90 秒，原生 Linux 仍为 45 秒。请求和作业各自的期限继续取较早值；探测取消时仍须完成子进程回收。旧失败回执保留。[首次超时](../../tmp/FF-D24-postprocess-evidence-20261122/early-fs-latex-timeout.json) · [首次 S3 超时](../../tmp/d24-postprocess-evidence-q2zai5gw/d24-result.json)

文件系统复验曾因脚本只接受 HTTP 409 而提前退出。组稿在读取图件时可以先返回明确的 HTTP 400 摘要冲突。复验依据准确的状态、图件路径与错误信息判断，并直接核对作业数。[保留的失败回执](../../tmp/FF-D24-postprocess-evidence-20261122/prior-fs-4d5b454f7c.json)

## 实际验收

- OpenFOAM 导热模型提取出的平均温度为 349.99999994412065 K。按 350 K 参考值回算的相对 L2 为 `1.596552985055106e-10`，通过 `1e-06` 容差。将参考值改为 365 K 后回算相对 L2 为 `0.04109589056405302`，三条报告入口均在创建作业前拒绝。解析参考只建立 Verification。文件系统还验证了对发布图件篡改单字节的拒绝、跨账户 404、源参数恢复后重启编译与历史 PDF 摘要一致。[文件系统回执](../../tmp/d24-postprocess-evidence-sw08feg6/d24-result.json)
- Edge 从真实发布读取当前就绪、过往就绪和历史诊断三种状态。通过键盘展开、预览焦点和关闭归位、CSV 摘要核对、2040 像素 PNG 预览与 960、650、390 像素宽度检查。[浏览器回执](../../tmp/FF-D24-postprocess-evidence-20261122/browser-result.json) · [作业面板](../../tmp/FF-D24-postprocess-evidence-20261122/evidence-desktop.png) · [图件预览](../../tmp/FF-D24-postprocess-evidence-20261122/verified-chart-edge.png)
- 认证 SeaweedFS 实测两个独立 API、篡改单字节、数值诊断、历史读取、跨账户 404 和重启后重新编译。Pandoc 在 HTML 和 DOCX 嵌入两份原始 PNG。双存储的 LaTeX 均归档实际读取的 PNG，PDF 中的图像为 2040×1110。四次成功编译各核验三条非特权 WSL 子进程退出回执。模型调用为 0。[SeaweedFS 回执](../../tmp/d24-postprocess-evidence-85onl6lk/d24-result.json) · [验收汇总](../../tmp/FF-D24-postprocess-evidence-20261122/validation.json)

如需回退界面，撤回 `JobsPanel` 的证据入口及证据组件。如需回退 WSL 探测时限，同时撤回客户端和执行器对共享时限的引用。[真实验收脚本](../../tmp/FF-D24-postprocess-evidence-20261122/check.py)
