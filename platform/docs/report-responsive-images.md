# 报告响应式图片与 DOCX 回退

2026-11-17，FF-E02 / FF-E22，WP09 / R4 / T07。报告支持 `img/srcset`、`picture/source`、密度描述符（`1x/2x`）、宽度描述符（`160w/320w`）及 `sizes/media`。所有候选图片进入冻结输入清单；HTML内嵌候选原件，当前屏幕未选择的图片也参与来源复核。

## 用户行为

```html
<img src="small.png"
     srcset="small.png 160w, large.png 320w"
     sizes="(max-width:600px) 160px, 320px" alt="示意图">
```

相对路径沿用报告目录优先、算例根目录其次的既有选择规则。支持空格路径的URL编码、文件名中的逗号、内嵌图片数据URI；候选解析不会简单按逗号切开数据URI。只接受规范的单一正数密度或正整数宽度，拒绝重复描述符、混用宽度/密度、空项、零值和非法描述符。缺失文件、远程候选和目录越界在作业创建前拒绝。

描述符和 `sizes/media` 保留，由浏览器实际选图。低像素密度并不承诺一定使用最小图片：浏览器可以优先使用已可获得的更高分辨率候选，内嵌图片不需要额外网络传输。依据 [HTML候选语法与处理规则](https://html.spec.whatwg.org/multipage/images.html#parsing-a-srcset-attribute)，本实现拒绝非法候选，不采用浏览器的静默容错作为报告输入校验。

DOCX是静态文档。原始HTML中的 `img` 现在保留 `src` 回退图片；没有 `src` 时使用自身第一个候选，或同一个原始picture块中的第一个source候选。DOCX不猜测屏幕宽度，也不丢弃整张图片。编译使用同一冻结来源，并对原生重编译结果继续做完整比较。

应用内阅读器保留经过逐候选校验的候选列表：发布报告只接受内嵌图片；普通工作区文档的本地候选通过Bearer鉴权读取并转成Blob URL。远程候选列表不挂载，保留可读的本地fallback。切换文件/算例沿用请求取消和Blob回收。消毒器仅将srcset交给专用列表校验，其他危险标签/属性限制不变。

## 实际验收

入口 `tools/report-srcset-browser.cjs`，审查 `tmp/FF-E02-srcset-review-v5-20261117.json` 共676份源码。最终 `tmp/docker-deployment-20261117/srcset-57f5bbe3b7/result.json`，算例33、作业28/29，6项通过：

1. 实际下载HTML在Chromium的1000/390px与DPR1/2四种组合中渲染；密度/宽度/picture/内嵌数据URI全部选到有效候选，原描述符和sizes保持，外部网络请求0。
2. 应用内发布阅读器保留4个候选列表、4张图片，不读取变化后的工作区资源。
3. 普通工作区HTML的候选图片使用实际Bearer/Blob读取；远程候选没有发出外网请求，fallback正常。
4. 实际DOCX阅读器显示4张静态fallback，逐像素核对红/红/蓝/红，补上此前Pandoc忽略原始HTML图片导致DOCX无图的问题。
5. 修改候选图片使旧来源过期，新发布使用新颜色，旧HTML/DOCX下载保持原字节。
6. 缺失、远程、越界、重复密度、混用描述符、零宽度和多个描述符七类输入均400；不新增作业，既有发布仍可下载。

像素颜色来自测试自行编码的纯色PNG，只用于确认浏览器选中了哪一个文件，不冒充工程图表。窄屏/DPR为Chromium模拟，不能代替其他浏览器/实际手机验收。独立HTML与DOCX界面截图已查看。

本轮实际模型调用0、模拟LLM0。所有检查为真实API/Pandoc/Chromium或文件协议；没有拦截业务响应来伪造成功。

## 失败、构建与回退

保留 `srcset-afc17cc88c`：最初错误假设1倍屏必选小图；实际输出属性完整，但Chromium选择已有的大图。同时检查发现DOCX没有任何图片。之后按标准核对允许的选图、仍严格断言属性/media/DPR高分辨率及图片像素，并补DOCX回退，不是忽略缺图。

保留 `srcset-738d3db369`：独立HTML已通过，但DOMPurify将候选属性按单个URI处理而全部移除；修复为逐候选校验后沿用原4个列表断言通过。`build-web-srcset-v2.log` 为Docker Hub认证EOF失败；`build-web-srcset-v2-retry.log` 成功。其间Docker引擎与服务正常，没有重置数据。

当前API/worker镜像 `vibeflow/api:srcset-v2-20261117`，web `vibeflow/web:srcset-v2-20261117`。正式Docker构建含TypeScript/Vite通过，无新增生产依赖和数据库迁移。回退分别使用 `report-css-v2-20261117`、`docx-layout-20261117`；已发布原件继续保留。

下一步是新增CSS/reference-doc/srcset资源的S3读取视图、独立worker、第二账户拒绝和恢复组合，见[交付核对](report-delivery-reconciliation.md)。主线49/53、剩4；资源11/17、剩6；295项目标不变。
