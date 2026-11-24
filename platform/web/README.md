# VibeFlow Platform Web

VibeFlow 的 React 浏览器工作台，不是 Vite 模板示例。后端入口、数据库和完整开发环境见 [../docs/dev-setup.md](../docs/dev-setup.md)。

## 技术栈

- React 19 + TypeScript + Vite 8
- Zustand 5：工作区、算例、文件树和编辑器状态
- dockview 4：可停靠面板布局
- Monaco：文本/OpenFOAM 字典编辑与补全
- xterm.js：PTY 终端
- Three.js：几何场景
- marked + DOMPurify + KaTeX：Markdown、聊天与公式渲染

pdf.js、texo/WASM/ONNX 位于 `public/`，不是 npm 依赖；第三方资产边界见 [../docs/third-party.md](../docs/third-party.md)。

## 安装与启动

```bash
cd platform/web
npm ci
npm run dev
```

默认地址为 `http://localhost:5173`。Vite 将 `/api`、`/ws` 和 `/viz-proxy` 代理到 `127.0.0.1:8000`，因此还需启动 FastAPI；Agent/工具测试通常还使用 `127.0.0.1:8990` 的 mock LLM。

## 验证

```bash
npx tsc --noEmit
npm run build       # tsc -b + vite build，正式构建检查
npm run lint        # oxlint
```

- 不要用单独的 Vite 转译成功替代 `tsc -b`。
- 当前 lint 仍有既有警告，build 有大 chunk 提示；不能把退出码 0 写成“零警告”。
- 主浏览器回归在 `platform/tools/`：先启动前后端和 mock，再运行 `node e2e-drive.mjs ../docs/shots/web`。
- 测试矩阵和其他 E2E 见 [../docs/testing.md](../docs/testing.md)。

## 源码入口

```text
src/
├── main.tsx              # React 入口
├── App.tsx               # token 校验、登录/主壳条件渲染
├── api.ts                # Bearer REST 封装
├── store.ts              # Zustand 全局工作区/文件状态
├── shell/
│   ├── Shell.tsx         # 12 个 dockview 面板
│   ├── dock.ts           # 标签与文件类型路由
│   └── editorRef.ts      # Monaco 光标插入桥接
├── panels/               # Tree/Editor/Case/Chat/Terminal/Jobs/Geo/Viewer/Viz 等
├── utils/                # diff、聊天渲染、OpenFOAM 语言与补全
└── pages/Login.tsx       # 登录/注册
```

## 实现约定

- REST 尽量使用 `api.ts`，不要在组件中新增裸 `fetch` 而漏掉 Bearer；raw/SSE 通道例外，需显式处理 token 和 HTTP 错误。
- Agent 与 Job SSE 使用 `fetch + ReadableStream` 手工分帧；终端使用原生 WebSocket。
- PTY/viz 会话同时要求有效 token 和当前用户所有权。
- caseDict 编辑保存必须走专用后端端点，普通文件保存不能绕过字典校验和 DB 镜像。
- 文件、会话、作业和可视化状态并不全部集中在 Zustand；面板本地状态修改前先阅读对应实现。
