# FloTHERM 输入检查与生成目录重置

2026-11-20，FF-I02 与 FF-I10A 按各自原始功能验收。输入检查在 CLI 和平台 API 提供一致的组合判定，平台转换接口继续拒绝 `force` 和 `keep_stage`。完整 Studio 窗口及转换工作流由 FF-I01 承接，Pack/FloSCRIPT 阶段执行由 FF-I03 承接，部件与物理转换由 FF-I04 承接。

## FF-I02 输入检查

`flotherm2foam.py --inspect INPUT [INPUT ...]` 输出 JSON。接受的配置返回退出码 0，检查发现阻断项时返回 JSON 与退出码 2。平台从 `POST /api/v1/cases/{id}/flotherm:inspect` 提供同一检查，越界、缺失或超限输入返回 400，跨账户返回 404。

逐一检查 ECXML、V2310 PDML、ECXML+PDML、XTXML+PDML、XTXMLA+PDML，以及 XTXML/XTXMLA 各自配对 PDML 和补充 ECXML。为原始字节返回 SHA-256，同一文件的重复 bbox 产生提示。PDML 检查从记录流文件头或兼容 XML 根声明读取版本，正文里的版本字样无法覆盖文件头。对无效 XML、单独 XTXML、错名配对和重复类型给出阻断。单文件上限继续为 32 MiB，认证 S3 按已选版本大小在下载前检查。[输入检查源码](../server/app/services/flotherm_inputs.py)

Windows 实际 CLI 和两个独立 SQLite API，分别搭配文件系统和认证 SeaweedFS，均通过七种组合。HTTP 验收另覆盖真实跨账户 404、本地越界链接、超过 32 MiB、重复输入和失败后原请求可继续使用。CLI 将 Pack/FloSCRIPT 识别为输入类型，Studio 明确拒绝。已验证 PDML 记录流文件头与输入检查契约，记录转换语义由相应转换任务承接。[文件系统](../../tmp/flotherm-i02-fs-ue72sa_k/inspection.json) · [认证 S3](../../tmp/flotherm-i02-s3-bx9w9no4/inspection.json)

## FF-I10A 安全重置

`flotherm2foam.py --version` 输出平台自有工具的版本。`--output DIR --force` 只清理生成标记列明的普通文件及其目录。清理前检查现存文件清单、路径和文件身份，拒绝混有额外文件或空目录、链接、越界内容和无效标记的目标。进程锁保护正在重置的目标，重叠请求立即被拒绝。原有 v1 生成标记可直接使用。[输出保护源码](../server/app/services/flotherm_output.py)

CLI 在开始删除前持久记录文件身份，进程中断后同一命令可清理仍匹配的剩余生成物。中断后写入的新文件和同名替换文件会使重试停止。无法读取的标记保留供人工核对。Windows 和非特权 WSL 实际运行 CLI 验证根和父目录链接、标记链接、并发重置、实际退出码 83、重复执行及真实生成的 OpenFOAM 目录清理。平台转换正常发布与无效 IR 兼容通过，HTTP 实际拒绝覆盖选项。[双运行时](../../tmp/flotherm-i10a-native-20261120-v3.json) · [转换回归](../../tmp/flotherm-publication-evidence-qiv7jsv0/result.json)

实际模型调用 0，模拟调用 0。测试在独立算例和临时目录执行，回退使用原有版本与空目录约定，数据库无需迁移。[执行前源码审查](../../tmp/flotherm-i02-i10a-review-20261120-v3.json)
