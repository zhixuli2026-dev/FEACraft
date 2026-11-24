# Texo 公式识别准确率

T07 使用 pdfLaTeX 编译 14 个真值公式。Poppler 将页面按 200 dpi 渲染为 PNG，裁去页面空白后逐张上传至实际 Edge 中的公式识别面板。新版验收返回每张图的 SHA-256、原始 LaTeX、预期 LaTeX、耗时与逐 token 编辑距离。 [真值生成](../server/texo_ground_truth.py) · [浏览器验收](../server/texo_accuracy_browser_acceptance.py) · [真值清单](../../tmp/FF-E19-texo-accuracy-20261123/samples/truth.json)

| 公式类型 | 严格匹配 | Token 编辑率 |
| --- | ---: | ---: |
| 基本符号、上下标和希腊字母 | 4/4 | 0.00% |
| 分式、根号和嵌套分式 | 3/3 | 0.00% |
| 积分、极限、求和、偏导和向量算子 | 3/5 | 18.57% |
| 矩阵和分段函数 | 0/2 | 78.33% |
| 合计 | 10/14 | 31.09% |

严格匹配忽略空白、部分 LaTeX 间距命令、`\left`/`\right` 大小控制和单 token 上下标的冗余括号。Token 编辑率为总编辑距离除以 193 个真值 token。这个口径会将某些等价排版计为差异，例如原生矩阵环境与括号加 `array`。表格反映这 14 张干净的排版样本。 [完整原始结果](../../tmp/ff-e19-texo-edge-ymlcq8a9/texo-accuracy.json)

Edge 在禁止外网的全新浏览器环境从本地部署包载入编码器与解码器。模型就绪后将整个浏览器上下文设为离线，最后一张未缓存的嵌套分式仍完成实际推理并严格匹配。损坏的 PNG 实际触发解码失败，重新选图恢复已有结果。随后在面板内改正积分样本，点击复制并从剪贴板读回修正值。实际 FormulaNet 识别结果 14 次，解码失败 1 次，LLM 调用 0 次，模拟 LLM 调用 0 次。 [纠错界面](../../tmp/ff-e19-texo-edge-ymlcq8a9/manual-correction.png)
