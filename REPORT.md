# VibeFlow Studio 0.11.0 逆向工程报告

日期：2026-10-19
分析方式：**纯静态分析**（未执行安装包或任何提取出的二进制）

## 1. 目标与证据

| 项 | 值 |
|---|---|
| 文件 | `VibeFlowStudio-0.11.0-win-x64-setup.exe` |
| 大小 | 420,730,127 字节 (402 MiB) |
| SHA-256 | `1d00ab1d732351754da6a11ea3f88021de086594c231c90efd98a8547c82e184` |
| 安装器格式 | Inno Setup **6.7.0**（loader 表 revision 2） |
| 应用 ID | `vibeflow-studio` |
| 发布者 | VibeFlow（https://vibeflow.org.cn） |
| 默认安装目录 | `%LOCALAPPDATA%\Programs\VibeFlowStudio`（免管理员） |
| 自动更新 | `https://vibeflow.org.cn/api/studio/update?platform=windows-x64&channel=stable` |

## 2. 安装包格式分析（本次的主要技术难点）

### 2.1 为什么现成工具全部失败

- **7-Zip 21.07**：`Cannot open the file as archive` —— 它是 Inno Setup 而非 NSIS。
- **innoextract 1.9**（2020 年，最新正式版）：`Unexpected setup loader revision: 2` / `Could not determine setup data version` —— Inno 6.4+ 把 loader 偏移表升级为 64 位偏移（revision 2），innoextract 只认 revision 1；且其版本表只登记到 6.4.0.1。
- 直接从 PE 资源枚举也可绕过，但本包用的是 5.1.5+ 的资源表方式（magic `rDlPtS CD E6 D7 7B 0B 2A`）。

### 2.2 逆向出的 Inno 6.7 容器格式

通过对照 issrc 官方源码（tag `is-6_7_0`）逐字段确认：

```
[PE stub (x86, 资源中含 loader 表)]
  loader 表 @0xceb58:
    magic(12) + version u32=2 + TotalSize i64 + OffsetEXE i64
    + UncompressedSizeEXE u32 + CRCEXE i32
    + Offset0 i64 = 0x18f2cba9   <- 头部块
    + Offset1 i64 = 0x000cfa00   <- 文件数据区
    + ReservedPadding u32 + TableCRC i32

[头部块 @ Offset0]
  TSetupID:      "Inno Setup Setup Data (6.7.0)" 定长 64 字节
  u32 CRC32 + TSetupEncryptionHeader (49 字节: EncryptionUse=0 未加密)
  TCompressedBlock #1:
    u32 HdrCRC + i64 StoredSize(含子块CRC) + u8 Compressed
    [u32 CRC + ≤4096 字节] * N   -> 拼接后是一条 LZMA1 原始流(5 字节头: props+dict)
    解压内容 = TSetupHeader + 各条目表（语言/消息/权限/类型/组件/任务/目录/
              ISSigKey/文件/图标/INI/注册表/删除/运行）+ 向导图片
  TCompressedBlock #2 (紧跟 #1):
    同样格式 -> 30,275 条 TSetupFileLocationEntry（每条恰好 89 字节）

[文件数据区 @ Offset1]
  一系列 chunk: "zlb\x1A" magic + LZMA2 压缩数据
  LZMA2 流首字节为字典大小编码: dict = (2|(p&1)) << (p/2+11)
  文件在解压后的 chunk 内按 ChunkSuboffset/OriginalSize 切片，多文件共享 chunk（solid）
```

### 2.3 字符串与条目标记

- `String` = u32 字节长度 + UTF-16LE；`AnsiString` = u32 长度 + 原始字节
- 文件条目含 32 字节 SHA-256（**校验的是反过滤后的原始字节**）
- PE 文件启用了 x86 call/jmp 指令优化过滤器（floCallInstructionOptimized）：
  存储时把 E8/E9 指令的相对地址转成"绝对"值提高压缩率；提取时必须按
  64 KB 缓冲区分块、以文件内绝对偏移反变换还原（`TransformCallInstructions` 的逆操作）

### 2.4 自研提取器

`tools/innosplit.py`（约 300 行 Python，纯标准库：lzma/zlib/struct/hashlib）：
- 解析 loader 表 → 两个压缩块 → 头部 JSON 清单 → 逐文件解 chunk、反过滤、**SHA-256 逐文件校验**
- 结果：**30,275 / 30,276 文件提取成功，0 失败，哈希全部吻合**（1 个跳过项是无嵌入数据的外部文件条目）
- 产物：
  - `extracted/` —— 完整安装目录树（约 1.2 GB）
  - `extracted-manifest.json` —— 全部 30,276 个文件的名称/路径/偏移/SHA-256/标志位

## 3. 应用架构（观察结论）

```
VibeFlowStudio.exe (5.4 MB, Qt5.15 + QtWebEngine 外壳)
 ├─ 内嵌 HTTP/WebSocket 服务（出现 HTTP/1.1 101 Switching Protocols 等串）
 ├─ 自定义源 https://vibeflow.local
 ├─ resources/            内嵌 Web 前端
 │   ├─ agent/            agent-conversation.html（Agent 会话 UI）
 │   ├─ cadviewer/        CAD 查看器
 │   ├─ flotherm2foam/    Flotherm -> OpenFOAM 转换器
 │   ├─ pdfjs/  markdown/  terminal/  notifications.qrc
 │   └─ texo/             离线 LaTeX 公式截图识别（FormulaNet ONNX +
 │                        Transformers.js 2.17.2 + onnxruntime WASM，
 │                        刻意兼容 Qt5.15 的老 Chromium 83）
 ├─ VibeFlowAgentHost.exe (0.2 MB)  VibeFlow::Runtime::VFAgentService/VFAgentTask
 │                                  —— LLM 调用宿主，QJson 消息驱动
 ├─ VibeFlowWorker.exe   (0.2 MB)  VFIpcChannel/VFIpcClient —— 索引等后台任务
 ├─ QtWebEngineProcess.exe + Qt5 全家桶 + OpenSSL 1.1/3 双套
 ├─ ParaView 5.11 全家桶（paraview/pvpython/pvbatch/pvserver + 全部 VTK 库）
 ├─ Python 3.9 嵌入版（python39.dll + Lib 标准库）
 ├─ pandoc.exe (231 MB!)  文档转换
 └─ vc_redist.x64.exe     VS 运行库安装包

share/vibeflow/           —— 这是产品的"灵魂"（AI Agent 定义）
 ├─ CLAUDE.md / AGENTS.md  数值仿真 Agent 工作规则（见 §4）
 ├─ skills/                37 个领域技能（vibeflow-openfoam, -fluent, -starccm,
 │                         -abaqus, -comsol, -su2, -lammps, -fenics, -latex,
 │                         -skill-creator, ...）均为 Claude Code 技能格式
 ├─ StandardCase/          算例骨架：0-caseDict/ A-geo/ D-post/ E-report/ UDF/
 ├─ completion/            openfoam-v2406 关键字补全 JSON（16.5 MB）
 ├─ materials/             各 LLM 厂商 logo 等素材
 ├─ python/                vibeflow_geo（参数化几何）、文档转换脚本
 └─ tools/pandoc/

share/vibeflow-agent/     —— 给"开发模式"Agent 用的另一份 AGENTS.md + skills
```

## 4. AI Agent 工作方式（观察 + 推断）

**观察**（直接来自 `share/vibeflow/CLAUDE.md`）：
- Agent 自称 "VibeFlow numerical-simulation agent"，有两种模式：
  1. **开发模式**：在 `UDF/` 下做自定义求解器/库/后处理工具开发，
     D1 需求确认 → D2 设计文档 → D3 实现 → D4 编译验证，文档写入 `UDF/README.md`
  2. **使用模式**：以 `0-caseDict/caseDict` 为**唯一参数源**推进全流程：
     需求澄清 → 参数化几何 → 网格 → 求解器输入生成与计算 → 后处理 → 报告
- 工作流状态机：`workflowState A;` 写回 caseDict，每步执行后更新字母
- `0-caseDict/memory.md` 是 Agent 的长期记忆（记录用户纠错和踩坑经验）
- 提问模板固定为 "Parameter name / Purpose / Explanation / Recommendation"

**观察**（来自主程序字符串）：
- 内置模型标识：`openai/gpt-4.1`, `openai/gpt-4.1-mini`, `openai/gpt-4.1-nano`
- 含 `setupAgentProfileCard` 等 UI 元素和 materials 里的多家 LLM logo
- **推断**：采用 BYOK（用户自配 API Key/服务商）模式，主程序负责把
  CLAUDE.md + skills + caseDict 组装成提示词，经 VFAgentService 调用 LLM API

## 5. 主要发现摘要

1. **这不是 Electron 应用**，而是 Qt5.15 + QtWebEngine 原生外壳内嵌 Web 前端，
   因此没有 app.asar，前端代码以 Qt 资源/文件形式存在于 resources/ 与主程序内。
2. **仿真内核 = OpenFOAM 生态**（v2406 关键字库、StandardCase 骨架、
   flotherm2foam 转换器），可视化 = ParaView 5.11/VTK 全家桶。
3. **AI 能力全部"外置"为文本资产**：37 个 Claude Code 格式技能 + CLAUDE.md
   工作流编排，核心 Know-how 都在 `share/vibeflow/` 明文存放。
4. 附带离线公式 OCR（texo/FormulaNet）、pandoc 文档转换、Python 几何库。
5. 安装包本身未加密（EncryptionUse=euNone），压缩为 LZMA2（64 MB 字典）。

## 6. 证据强度说明

- **观察**（有校验证据）：文件树、文件内容、哈希、安装器元数据 —— 提取全程 SHA-256 通过
- **推断**（合理但未验证）：LLM 调用的具体 API 路径、Agent 运行循环细节 —— 需要
  进一步反汇编 `VibeFlowAgentHost.exe` 或读取前端 JS 确认
- **未知**：许可证/激活机制（本次未见明显迹象）；服务端 api/studio/* 的行为

## 7. 建议的深入方向

| 方向 | 入口 |
|---|---|
| Agent 提示词组装与 LLM 协议 | 反汇编 `bin/VibeFlowAgentHost.exe`（关注 VFAgentService） |
| 前端业务逻辑 | `resources/agent/agent-conversation.html` 及主程序内嵌 qrc |
| 仿真工作流细节 | `share/vibeflow/CLAUDE.md`（全文）+ `StandardCase/0-caseDict` |
| 各领域求解器接入 | `share/vibeflow/skills/vibeflow-*/SKILL.md` |
| 公式识别 | `resources/texo/`（README 写得非常坦白） |

## 附：工具清单

- `tools/innosplit.py` —— Inno Setup 6.7 提取器（本项目自研，可复用于其他 6.4+ 安装包）
- `tools/analyze_manifest.py` —— 清单统计
- `tools/strscan.py` —— 二进制字符串扫描
- `tools/innoextract/` —— innoextract 1.9（对 6.7 无效，仅留档）
- `tools/innoextract-src/` —— innoextract 源码（格式参考）
