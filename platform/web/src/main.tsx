import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import * as monaco from 'monaco-editor'
import editorWorker from 'monaco-editor/editor/editor.worker.js?worker'
import jsonWorker from 'monaco-editor/language/json/json.worker.js?worker'
import cssWorker from 'monaco-editor/language/css/css.worker.js?worker'
import htmlWorker from 'monaco-editor/language/html/html.worker.js?worker'
import tsWorker from 'monaco-editor/language/typescript/ts.worker.js?worker'
import './index.css'
import App from './App.tsx'
import ClientErrorBoundary from './shell/ClientErrorBoundary.tsx'

// Worker 使用本地同版本包，不把 jsDelivr 可用性当作启动前置。
;(window as any).monaco = monaco
;(window as any).MonacoEnvironment = {
  getWorker(_moduleId: string, label: string) {
    if (label === 'json') return new jsonWorker()
    if (label === 'css' || label === 'scss' || label === 'less') return new cssWorker()
    if (label === 'html' || label === 'handlebars' || label === 'razor') return new htmlWorker()
    if (label === 'typescript' || label === 'javascript') return new tsWorker()
    return new editorWorker()
  },
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ClientErrorBoundary><App /></ClientErrorBoundary>
  </StrictMode>,
)
