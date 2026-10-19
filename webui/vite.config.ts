import path from "node:path"
import { fileURLToPath } from "node:url"
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

const here = path.dirname(fileURLToPath(import.meta.url))

// dev 时代理 /api 到本地 core API（scripts/serve.py 默认 8420）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(here, "./src") },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules")) {
            if (id.includes("react-resizable-panels")) return "layout"
            if (id.includes("@xyflow")) return "graph"
            if (id.includes("@xterm")) return "terminal"
            if (id.includes("react-markdown") || id.includes("remark-gfm")) return "markdown"
            if (id.includes("lucide-react")) return "icons"
            if (id.includes("react-dom") || id.includes("/react/")) return "react"
            return "vendor"
          }
          if (id.includes("/src/views/reverse/")) return "reverse-workbench"
          if (id.includes("/src/views/")) return "views"
          if (id.includes("/src/components/chat/")) return "chat-components"
          if (id.includes("/src/components/settings/")) return "settings-components"
          if (id.includes("/src/components/blackboard/") || id.includes("/src/components/team/")) return "board-components"
          return undefined
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
