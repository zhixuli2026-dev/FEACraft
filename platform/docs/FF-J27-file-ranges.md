# FF-J27 文件下载和字节区间

本地算例的 `/raw` 支持完整下载、`HEAD`、单段和多段字节区间。实测固定区间、开放尾部、后缀和超过结尾的上界均返回 206。响应中的 `Content-Range`、`Content-Length` 和正文字节逐一核对。不可满足、倒序、格式错误及重叠区间返回 416。HEAD 表明同一长度和文件版本，正文为空。 [文件路由](../server/app/routers/files.py) · [实际验收](../../tmp/file-read-consistency-mpjqvuyb/download-result.json)

真实文件系统共通过 17 项检查。读取开始前替换、数据块间替换或删除、同长度就地修改均正确处理。旧版本冲突返回 409，重新获取当前版本后恢复下载。第二个实际 API 进程读取同一文件版本，外部账号被拒绝。另有 6 项独立对象存储协议故障检查。正式执行器退出 0，模型调用为 0。 [正式执行器结果](../../tmp/vibeflow-regression-offline-20261119-201836-bef073/summary.json) · [文件下载资源验收](raw-download-capacity.md)
