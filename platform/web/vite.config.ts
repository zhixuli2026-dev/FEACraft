import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { existsSync } from 'node:fs'

const apiTarget = process.env.VITE_API_TARGET || 'http://localhost:8000'
const wsTarget = process.env.VITE_WS_TARGET || apiTarget.replace(/^http/, 'ws')
const texoAvailable = [
  'texo-ocr.html', 'model/onnx/encoder_model.onnx', 'model/onnx/decoder_model_merged.onnx',
].every(candidate => existsSync(new URL('./public/texo/' + candidate, import.meta.url)))

const proxy = {
  '/api': { target: apiTarget, changeOrigin: true },
  '/ws': { target: wsTarget, ws: true },
  '/viz-proxy': { target: apiTarget, changeOrigin: true, ws: true },
}

export default defineConfig({
  plugins: [react()],
  define: { 'import.meta.env.VITE_TEXO_AVAILABLE': JSON.stringify(String(texoAvailable)) },
  server: { proxy },
  preview: { proxy },
})
