# FF-B08 CAD 对象树和视图状态

工作台的「对象与设置」显示 CAD 对象、可见性和选中状态。场景包含可显示的 OCP 装配网格时，可以在「平铺」和「装配」之间切换。平铺保留发布文件中的对象。装配以原生部件路径组织实例，显示各级部件并允许独立选择。装配分组的复选框批量显示或隐藏后代，部分可见时显示混合状态。选择实体同时更新画布高亮和属性读数。[对象树组件](../web/src/panels/CadObjectTree.tsx) · [场景解析](../web/src/utils/cadScene.ts)

同一浏览器标签页内，按账号、算例和查看方式分别保存展开状态、对象可见性和选中对象。重载场景、重开工作台面板或刷新页面后，使用当前有效场景恢复这些状态。已从当前场景移除的对象或分组会清掉对应记录。平铺是初次查看的默认方式。保存装配模式偏好后再次进入时尝试恢复，装配数据无效时降级为平铺并显示诊断。Base64 或 STEP 临时查看使用当前视图的内存状态，不写入发布和算例文件。

## 实际验收

隔离 API 使用 SQLite 和实际文件系统。两次 CadQuery 原生预览分别创建含共享实例的嵌套装配及另一个包含同名实体的算例。Edge 通过键盘展开组和选中对象，测试隐藏整个装配、只显示其中一项、区分未选中与选中的画布截图。重新读取 API 场景后，点击已选对象前后的画布哈希一致，确认新建网格已恢复高亮。实际浏览器刷新恢复折叠的抽屉和组、隐藏的实体与当前选择。换到另一个算例时同名对象使用默认状态，返回后原状态恢复。平铺和装配视图偏好独立，原服务器发布 ID 相同，跨账户读取返回 404。模型调用为 0。[浏览器回执](../../tmp/geometry-tree-browser-ftgg8szc/geometry-tree-result.json) · [装配和混合可见性](../../tmp/geometry-tree-browser-ftgg8szc/assembly-tree.png)

两个原生 CadQuery 作业均成功且各有 6 个进程退出凭据。实际隔离 API 已退出，端口关闭。首次夹具误把新算例预置的几何脚本当成缺文件，收到真实条件写 409。随后使用正常读取返回的摘要更新预置脚本，保留首次回执。[退出复核](../../tmp/geometry-tree-browser-ftgg8szc/cleanup-independent.json) · [首次隔离记录](../../tmp/geometry-tree-browser-a0_xd53k/geometry-tree-result.json) · [受审验收入口](../server/geometry_tree_browser_acceptance.py)

装配面板的缓存与当前账号和算例绑定后，用最终前端构建再跑 FF-B08、FF-B07 和 FF-B10，三组真实 Edge 均通过。六个实际 CadQuery 作业均成功，三个隔离 API 端口已关闭，原生进程退出回执已核对。[最终对象树回执](../../tmp/geometry-tree-browser-32e4789b/geometry-tree-result.json) · [合并复验与退出核对](../../tmp/geometry-tree-browser-32e4789b/combined-cleanup-independent.json)

合入截图回传后，使用截图验收的同一前端构建重新验证对象树。实际 Edge 通过选择、折叠、可见性、刷新和跨算例隔离，两个原生 CadQuery 作业成功。[截图功能合并复验](../../tmp/geometry-tree-browser-kzscec9d/geometry-tree-result.json)
