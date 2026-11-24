# 模型配置页

在桌面「设置」中点击「管理模型配置」。移动端可以从工作面板选择「模型配置」。用户可以新增、编辑、连接测试、读取模型目录，并确认删除自己的配置。组织共享配置保留只读元数据及可用性检查。编辑时留空密钥会保留已保存的凭据，成功保存后输入框清空。变更后 Agent 会刷新配置列表。

2026-11-24，隔离账号在 Edge 完成新增、连接测试、动态目录、无效地址修改后恢复和再次测试。前两轮遇到上游连接超时，保留了实际失败记录。最后一轮三次 DeepSeek `/v1/models` 请求均返回 HTTP 200，包含 `deepseek-flash` 和 `deepseek-v4-pro`。目录读取没有发起推理，实际与模拟 LLM 调用各 0 次。另一个账户的读取、测试、编辑和删除均返回 404，所有者实际删除成功。 [浏览器及权限回执](../../tmp/ff-f11-provider-settings-bsbumcqd/provider-settings.json) · [首次超时](../../tmp/ff-f11-provider-settings-he1krrrh/upstream.jsonl) · [桌面页面](../../tmp/ff-f11-provider-settings-bsbumcqd/provider-desktop.png) · [390 像素页面](../../tmp/ff-f11-provider-settings-bsbumcqd/provider-mobile.png)
