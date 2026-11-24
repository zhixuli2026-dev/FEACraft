# FF-B01 / FF-B02 几何脚本契约

几何作业在封存的算例内执行脚本，提供 `__file__`、`CASE_ROOT`、`A_GEO_DIR`、`case_root`、`VIBEFLOW_CASE_ROOT`、`VIBEFLOW_GEOMETRY_SCRIPT` 和 `ctx`。脚本以 `vibeflow_case_geometry` 模块运行，可以导入同目录的辅助模块。`ctx.log` 与 `debug` 的消息同时写入作业日志和场景。[几何执行器](../server/app/services/geo_script.py)

预览识别 `build_geometry(ctx)`、`build(ctx)`、`result`、`objects`、登记对象和自动发现的顶层几何。工作平面、CadQuery 实体与装配体以及原生 OCCT 形状进入同一验证和预览路径。`show_object`、`show` 和 `ctx.show` 的颜色及透明度按登记保留。几何相对文件名和带 `A-geo/` 前缀的路径都可写到本次尝试的几何目录。预览抑制 `cq.exporters.export`，显式导出执行脚本的导出钩子。[脚本验收入口](../server/geometry_script_contract_acceptance.py)

隔离 SQLite／文件系统的九个实际 CadQuery 作业覆盖六种脚本形式、封存路径、辅助模块导入、数据类和参数化尺寸、日志、错误与恢复。装配体由两个实体构成，三个被自动发现的对象实测宽度为 2、3、7。上下文脚本从 caseDict 读宽度 6、倍率 2，经同目录辅助模块生成宽度 12 的实体。预览和显式导出的行为分别核对，脚本异常保留正常历史，改回后成功，原发布仍可读取。匿名和其它账户分别得到 401、404。共同文件读取接口合入 OpenFOAM 文件识别后，九个作业整套复验通过。两轮 API 均已退出，真实模型和模拟调用均为 0。[最终回执](../../tmp/geometry-script-contract-xvytrz5r/geometry-script-contract.json) · [首次回执](../../tmp/geometry-script-contract-76w10bpq/geometry-script-contract.json)

加入精度参数与 OCP 可选载荷后，未带新选项的原九作业再次整套通过。真实模型和模拟调用均为 0。[精度变更后复验](../../tmp/geometry-script-contract-o9bhnh9t/geometry-script-contract.json)
