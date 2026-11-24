# LaTeX 构建提交的输入封存

2026-11-22，`/latex:build` 先准备主文档、资源、工程成果和原生运行信息，再进入写事务。作业提交仍复核写权限、来源文件、已读成果与原请求键。旧式存储的已有文档校验继续可用。

使用真实 `pdflatex` 和 40 MiB 算例资源对照。旧 SQLite 在扫描输入时阻塞独立写入，同算例保存返回 409。新版同一位置可独立写入，同账户另案保存返回 200，发布后读回 13,488 字节 PDF。用于文档构建的真实求解、VTK 后处理也完成并提取约 350 K。 [旧版对照](../../tmp/n4-latex-submission-baseline-obmyf6ia/latex-submission-baseline.json) · [新版 SQLite](../../tmp/n4-latex-submission-fixed-tcz8q5dy/latex-submission-fixed.json)

认证 SeaweedFS 配 SQLite 同样在实际 40 MiB 源文件核验期间允许独立写入，另案保存返回 200。实际 PDF 可读回，大小同为 13,488 字节。输入准备、原生编译、成果回读和测试服务退出均已核对。 [远端复验](../../tmp/n4-latex-submission-s3-fixed-t9cfsu3x/latex-submission-fixed.json)

本批使用实际 OpenFOAM、VTK、LaTeX 和存储。模型调用为 0。证据纳入 N4-05 的 RB-12 与 RB-14。 [完整验收](../../tmp/N4-05-latex-submission-seal-20261122/validation.json)
