# FF-A25 导入外部 StandardCase

2026-11-21，40 MiB 导入在 SQLite、PostgreSQL 与文件系统、认证 SeaweedFS 的四组合复验。正文阶段允许另一算例保存，实际 API 退出后可按原名重试。Windows 同卷导入省去解压后再复制的正文副本。[资源回归](archive-capacity.md#2026-11-21-standardcase-大文件安装)

在 Microsoft Edge 文件选择器中导入两个编写好的工程 ZIP。一个包带单层外部目录并指定算例名，另一个包直接包含标准目录并以文件名作为算例名。两次导入均自动选中新算例，文件树使用剥根后的路径，并从实际文件接口打开作者说明。两个算例的初始字典修订均为 1。各自的 11 个原始文件通过正式下载接口逐字节复核，包含自定义几何脚本、求解输入和二进制校准数据。

浏览器显示损坏 ZIP、缺少 caseDict、字典语法错误、大小写冲突和算例重名的服务端提示，状态分别为 400、400、422、400、409。失败后原选中算例保持，工作区内仍只有原算例和两次成功导入的算例。第二个账户提交合法 ZIP 至该工作区与读取导入算例均返回 404。

2026-11-19，从浏览器导入带外层目录的算例后，通过平台作业接口运行 WSL 中真正的 OpenFOAM v2406。`blockMesh` 和 `icoFoam` 均正常结束，正式下载接口返回 `C-run/case-1/0.1/U`，共 11,128 字节，SHA-256 为 `6e04089daf7af8a2ef79a222b28df91d16e42c0fdaa45142b087b0bd65c39349`。原算例的作者说明及 caseDict 保持原样。[完整回执](../../tmp/standard-case-import-browser-vm64ttb3/standard-case-import-browser.json) · [浏览器截图](../../tmp/standard-case-import-browser-vm64ttb3/import-owned-case.png) · [blockMesh 日志](../../tmp/standard-case-import-browser-vm64ttb3/blockMesh.log) · [icoFoam 日志](../../tmp/standard-case-import-browser-vm64ttb3/icoFoam.log)

本次使用 D 盘隔离的 SQLite 和文件存储，复用 D 盘当天正式前端构建。隔离 API 原进程已确认退出。C 盘可用空间由 22.21 GiB 变为 22.20 GiB。真实模型调用、模拟模型调用和 Docker 操作均为 0。[复验入口](../server/standard_case_import_browser_acceptance.py)

较早的 A25 回归记录覆盖路径越界、符号链接和压缩比拒绝。近期 A09 在 SQLite／文件系统和 PostgreSQL／实际 SeaweedFS 复核了 40 MiB 导入、暂存文件被替换后的原子拒绝以及同名重试。[早期 A25 记录](progress.md) · [A09 大文件回执](FF-A09-standardcase-scaffold.md)
