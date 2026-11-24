# G1旧网格、求解、后处理请求的正式重新准备

2026-11-08。[N5-05原收口清单](N5-05-closure-plan.md)G1剩余三类命名场景通过，主线仍34/53、剩19项。原53项及295项目标保持，G2/G3继续。

FS/临时PG和S3/临时PG各验证mesh→mesh.generate、solve→solve.openfoam、postprocess→postprocess.run。每类旧固定argv真实写一次计数，等待独立标记后实际退出23；原封存和失败保留。明确重新准备使用当前caseDict建立新根attempt 1，绑定正式处理器与成果，旧argv即使已改成必定失败的内容也不再运行。同幂等键返回同一新Job。

实际blockMesh/checkMesh通过原质量门禁；laplacianFoam生成80单元time1温度，逐单元与T(x)=300+100x误差小于1e-6；VTK提取均值与350 K误差小于1e-6，19点线采样及实际PDF/SVG/PNG验证。沿用后处理验收的10×4×2专用网格，不替代N7-03原三基准入口验收。当前配置通过修改报告标题区别于旧封存，工程问题和上游工程语义保持一致，不宣称本场景改变了热边界。

实际旧命令尚运行时拒绝重新准备；retry/resume、错误类型/版本/额外命令、原attempt版本、当前修订和事件序号过期均拒绝。缺封存历史行和移除原排空回执是隔离库中的明确协议故障，均recovery_required；不将这些合成历史行称为真实程序执行。

两端共6个真实旧命令、6个正式新执行，另有实际工程前置；不是任意旧shell自动重放。原geometry/convert两类已归档证据见[此前验收](engineering-repeat-and-legacy-reprepare.md)，本批补齐G1其余三类。发布预留归属和工具链损坏、所有既定崩溃交错仍按G2/G3逐条验收。

真实模型api.deepseek.com/deepseek-flash共4次HTTP、17120reported tokens，模拟0。模型仅通过真实read_text_file校验场景报告哈希，原生执行和协议故障另列，不宣称模型自主选择重新准备操作。FS证据tmp/live-cli-startup-b6fmzqvv；S3证据tmp/live-cli-startup-3vlrbntw；live-result.json、legacy-engineering-reprepare.json、真实计数文件及封存/成果清单保留。两临时PG已删除，API与本批S3退出，开发库未改。
