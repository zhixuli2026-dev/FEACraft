# 通用结果阅读器与 viz 文件路由（WP07）

> 当前实现：2026-10-26。面向结果文件使用者和 reader/viz 维护者。
> 文件归属与路径校验在 API 层完成；reader 本身只负责读取数据，不作为授权边界。
>
> 2026-11-19 的时序、复合数据块和大文件实测见 [FF-D25 通用工程结果读取](FF-D25-result-readers.md)。本页保留 2026-10-26 的实现基线和当时的开发环境回归记录。

## 1. 已验证格式

| 格式 | 当前 reader | 验证 |
|---|---|---|
| `.vtk` | `vtkDataSetReader` | legacy POLYDATA 三角形与标量 |
| `.vtu` | `vtkXMLUnstructuredGridReader` | 四面体与点场 |
| `.vtp` | `vtkXMLPolyDataReader` | 三角形与点场；文件树→cookie 鉴权 viz→实际渲染均通过 |
| `.vtm` | `vtkXMLMultiBlockDataReader` | VTU+VTP 多块合并 |
| `.xdmf/.xmf` | `vtkXdmfReader` | 内联 XML 三角形 |
| `.exo/.e/.exodus` | `vtkExodusIIReader` | 标准 NetCDF/Exodus 四面体 |
| `.stl` | `vtkSTLReader` | ASCII STL 三角形 |
| `.series` | JSON 时刻索引+上述 reader | time=0/1 枚举与选择、越界拒绝 |

composite 输出通过 `vtkCompositeDataGeometryFilter` 转为可渲染数据集，并在返回前 `DeepCopy`，避免局部 reader/filter 生命周期导致悬空引用。静态文件无时刻时不虚构时间轴；series 的时刻必须唯一，成员路径不能越过 series 所在目录。

Exodus 测试文件由 SciPy 按 NetCDF/Exodus 结构生成。本机 `vtkExodusIIWriter` 在最小一单元输入上也会段错误，因此没有使用该 writer，也没有把崩溃写成通过。

## 2. API 与文件树

`POST /api/v1/cases/{id}/viz:sessions` 现在接受两种互斥输入：

```json
{"run_dir": "C-run/case-1"}
```

或：

```json
{"file_path": "D-post/data/sample.vtu"}
```

- `run_dir` 保持原 OpenFOAMReader 和自动 `.foam` 桩行为。
- `file_path` 只允许支持的扩展，必须是当前 case 内实际文件；相对越界、Windows 盘符/UNC、目录逃逸、缺失文件和不支持格式在创建进程前拒绝。
- 会话响应增加 `file_path`；所有者 proxy、并发限制、配额和关闭语义不变。首页 query token 成功后设置按 sid 路径隔离的 HttpOnly/SameSite=Strict cookie，供 trame 子资源和 wslink 使用，每次仍校验归属。
- 文件树点击 `.foam` 仍按 run_dir 打开；点击上述通用结果文件按 file_path 打开。STEP 暂不冒充 VTK reader，仍走原路径。

viz kernel 使用 `--foam` 或 `--data-file` 之一。几何无场文件使用普通颜色；有点标量时沿用现有 LUT、切面和其他可用显示路径。流线/矢量等需要相应向量场，不能由 STL 无场数据推导。

## 3. 当前边界

- 这是通用 reader+现有 VTK/trame 显示链，不是完整 ParaView server-manager 属性系统。
- 尚未实现 reader 参数面板、异构多块选择、并行 reader、超大文件流式加载和全格式矩阵。
- `series` 不支持嵌套 series；成员文件必须与索引同目录或其安全子目录。
- bundled `extracted/bin/pvpython.exe` 存在，但当前权限策略拒绝执行解包的外部二进制；server-manager/pvbatch 运行时探针未执行，D15 保持未实现/未验证。
- 当前 viz 仍是进程内会话注册表，未解决多副本共享和持久恢复。

## 4. 验证

```bash
cd platform/server
./.venv/Scripts/python.exe -X utf8 result_reader_smoke.py
./.venv/Scripts/python.exe -X utf8 viz_smoke.py
```

前端：

```bash
npm --prefix ../web run build
node ../tools/e2e-drive.mjs ../docs/shots/wp07-readers
```

`result_reader_smoke` 新建独立 case，逐格式检查点/单元、series 时刻、file_path 会话、直接所有者 proxy 和 Vite 子资源 cookie 续接，并关闭自身 viz 会话。主浏览器另验证 `.foam` 与 VTP 子资源鉴权；VTP 实际显示三角形、3 点/1 单元和 p=[1,3]，截图 `shots/wp07-readers-vtp.png` 已人工核对。
