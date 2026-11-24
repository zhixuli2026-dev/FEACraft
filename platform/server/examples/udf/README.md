# UDF D1–D5 参考实现：最小 functionObject

本例用于证明真实的 UDF 开发闭环，不是求解器或物理模型实现。

## D1：需求与边界

- **目标**：提供一个可在 OpenFOAM 算例中注册并执行的 `myFunctionObject`，首次执行时输出 `P0_UDF_OK`。
- **输入**：包含 `functions { myFunctionObject {} }` 的 `system/controlDict`，以及动态库搜索路径。
- **输出**：编译 `libmyFunctionObject.so`、动态库加载成功、OpenFOAM 日志出现标记、生成新的时刻目录。
- **非目标**：不实现自定义求解器、湍流模型、边界条件、fvOption、MPI 或其他 UDF 类型。
- **验收**：D4 的编译/加载和 D5 的算例执行全部通过，不能只检查文件存在。

## D2：设计

- 类 `myFunctionObject` 继承 `functionObject`，通过 `TypeName` 和 `addToRunTimeSelectionTable` 注册。
- `execute()` 只在首次调用时输出一次 `P0_UDF_OK`；`write()` 返回 true，不写文件。
- `Make/files` 列出实现文件，`Make/options` 指定目标 `libmyFunctionObject` 和 `LIB_LIBS`。
- 测试在临时目录复制源码和算例，避免污染用户源码与 case。

## D3：实现组织

- `myFunctionObject.H`：类声明、构造函数和 `execute/write`。
- `myFunctionObject.C`：注册、构造函数和最小执行逻辑。
- `Make/files`、`Make/options`：目标、包含路径和链接依赖。

## D4：编译与最小验证

```bash
cd platform/server
./.venv/Scripts/python.exe udf_smoke.py
```

测试从空临时目录复制源码，执行 `wmake libso` 和 `ctypes.CDLL`，随后继续 D5。当前机器同时存在 OpenFOAM 1912 与 v2406；脚本显式 source v2406、设置所需预加载库，并使用 `wsl --exec bash -lc`，避免 WSL 默认 shell 提前展开变量而混入另一套运行库。手工复现时应以 `server/udf_smoke.py` 的完整环境为准，不能只执行裸 `ctypes.CDLL`。

编译和加载成功是 D4 的必要条件，不代表 D5 已完成。

## D5：算例集成与交付

在隔离算例的 `system/controlDict` 中加入：

```foam
functions
{
    myFunctionObject
    {
        type myFunctionObject;
        libs ("libmyFunctionObject.so");
    }
}
```

执行 `icoFoam`，要求日志出现 `P0_UDF_OK` 并生成新的时刻目录。`type` 必须显式写出，动态库路径必须可被 OpenFOAM 找到；缺少 `type` 会被 OpenFOAM 拒绝，不能把它当作插件已加载。

## 风险与剩余工作

- 本例只覆盖 functionObject 的一种注册/执行路径；其他 UDF 类型、复杂对象生命周期、并行和实际物理计算仍需独立验证。
- 通用 UDF 工作流 UI、自动文档生成、G5 全套验收和其他求解器类型尚未实现。
