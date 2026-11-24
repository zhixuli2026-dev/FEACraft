# FF-F62 技能名称、简介与默认提示

2026-11-21，Abaqus、Aircraft Design、COMSOL、Fluent、Skill Creator 和 STAR-CCM+ 的 `agents/openai.yaml` 通过验收。SQLite 隔离 API 的技能列表和详情对六项的名称、简介、默认提示返回一致。真实 Edge 逐项搜索并选择，弹窗显示对应名称和简介，默认提示原样进入草稿。[API 与浏览器结果](../../tmp/skill-metadata-hn6hxn2a/skill-metadata-result.json) · [浏览器截图](../../tmp/skill-metadata-hn6hxn2a/skill-picker-draft.png)

平台写入的元数据使用 YAML 可读取的 JSON 格式，保留引号、Windows 路径和换行。实际保存、列表、详情、ZIP 打包和 Edge 草稿均核对了这些字符。读取继续兼容已有的行式 YAML 文件。目录缓存按每个文件的身份和时间核对，覆盖内容更新、元数据损坏、删除元数据和删除个人覆盖后恢复内置版本。第二个普通账户仍读到自己的内置值。未登录请求返回 401，草稿未发送，实际模型调用和模拟模型调用均为 0。

复跑入口 [skills_metadata_smoke.py](../server/skills_metadata_smoke.py)。传入 `--dist platform/web/dist --browser-python C:/ProgramData/anaconda3/python.exe` 执行真实 Edge 验收。在源码回退时同步恢复读写两个模块。通过本版保存的本地元数据可以继续由支持 JSON 的 YAML 读取器打开。
