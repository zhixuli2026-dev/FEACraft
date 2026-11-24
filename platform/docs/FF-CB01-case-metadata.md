# FF-CB01 算例元数据与配置检查

## 2026-11-22 原 T02 验收

按本地 StandardCase v1 核对状态、学科、分析类型、维度、精度、执行方式、单位和描述字段。配置检查现在提示独立学科与明确物理问题不匹配、算例维度与显式网格维度不匹配、本地执行偏好与远程模式冲突。接受 `2D-wedge` 和 `2D-axisymmetric` 的二维或三维网格表达。耦合问题保留原学科选择。状态及时间行为的原错误仍阻止修改，硬规则冲突不增加修订。 [原模板](../../extracted/share/vibeflow/StandardCase/0-caseDict/caseDict) · [检查实现](../server/app/services/case_meta.py)

文件系统和认证 SeaweedFS 分别配 SQLite、PostgreSQL 均通过提示、有效配置、修订 409、无效配置 422、三种跨账户拒绝和未知字段保留。Edge 通过现有表单显示三条提示，调整学科、维度和执行方式后检查通过，实际保存仅增加一个修订。首轮准备脚本用了只允许替换已存在字段的接口，第二轮暴露字典解析保留数字为字符串。按实际解析格式改正后通过。隔离 API、SeaweedFS 和临时 PostgreSQL 均收尾。模型调用 0，模拟 0。 [当前四组与 Edge 回执](../../tmp/FF-CB01-lint-20261122/validation.json)

既有验收覆盖 38 个 v1 枚举值、12 个单位、5 种时间行为，以及稀疏旧 ZIP、单标签文本升级、修订保留和 SQL 镜像／溯源。当前核对覆盖已有模板和历史格式，FF-CB01 按原条件完成。 [历史枚举与迁移证据](../../tmp/case-meta-profile-browser-mljt7kdb/case-meta-profile-result.json)

## 原实现

算例面板的元数据表单使用服务端提供的 v1 选项。六组字段覆盖状态、学科、分析类型、维度、精度层级和执行方式，另有单位、名称、项目、项目修订、备注及标签。工具读取的选项与本地原始 StandardCase 模板逐项一致。[原始模板](../../extracted/share/vibeflow/StandardCase/0-caseDict/caseDict)

原块表单一次只能保存一个块。旧算例的稳态分析和稳态时间行为需要一起切换时，单独保存新的分析或时间行为都返回 422。新表单通过一次提交更新两个字段，保留其它块和未编辑字段的源文本。检查配置给出字段路径和严重程度，复用原来的状态／阶段与分析／时间行为规则。执行偏好为本地、执行块却指定远程时显示核对提示。冲突检查同时核对旧修订及文件摘要。用户保留草稿并采用服务器修订时，仅合并自己编辑过的字段。[原拆分保存反例](../../tmp/cb01-before-2stvdvhc/case-meta-before.json)

## 2026-11-19 实际验收

- 通过真实 API 保存并回读六组共 38 个枚举值、12 个单位选项、5 个时间行为值。状态与阶段冲突、非法学科、非法维度和非法执行方式各收到 422，原文件与修订保持。三种跨账号请求均返回 404。[最终验收回执](../../tmp/case-meta-profile-browser-mljt7kdb/case-meta-profile-result.json)
- 实际导入带稀疏元数据的旧格式 ZIP。读回时，原始单标签文本和项目修订保持原字节。用户在 Edge 增加标签后，当前字典采用括号包裹的列表语法，并保留项目修订、未编辑的单位和自定义字段原文。Edge 同时保存谐响应与频域设置，只增加一个字典修订。无写入检查、陈旧版本冲突和保留服务器后来备注均通过。最终文件、SQL 镜像和溯源摘要一致。[最终浏览器回执](../../tmp/case-meta-profile-browser-mljt7kdb/meta-browser.json) · [桌面表单](../../tmp/case-meta-profile-browser-mljt7kdb/meta-profile-desktop.png)
- 旧通用块表单与单位换算在同一构建复跑通过。首次 ZIP 夹具把名称放入 URL 查询，系统按原设计使用文件名；改用正式 multipart 名称字段后通过。[A06 回归](../../tmp/case-meta-browser-pvgonxl4/case-meta-result.json) · [首次夹具回执](../../tmp/case-meta-profile-browser-1g_003y9/case-meta-profile-result.json)

本轮使用 D 盘隔离文件存储和 SQLite，模型调用为 0。原始资料给出了 v1 字段和值，没有版本标记与迁移映射。已验收的历史迁移范围为稀疏元数据、标签表示以及原文和已有项目修订的保留。若后续发现其它 schema 版本或跨学科、维度和求解器的排斥规则，需要对应版本映射或物理规范才能验收该范围。
