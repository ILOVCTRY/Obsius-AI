// 临时验证配置：把 /api 代理到临时徽章实例（8499），不改动主配置。
import { defineConfig } from "vite"
import base from "./vite.config"

const b = base as Record<string, unknown>

export default defineConfig({
  ...(b as object),
  server: {
    port: 5199,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8499", changeOrigin: true, ws: true },
    },
  },
})
