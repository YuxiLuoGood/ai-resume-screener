import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, forward /api/* to FastAPI so the frontend needs no hard-coded backend URL and avoids CORS issues.
// Use 127.0.0.1, not localhost: Node 17+ resolves localhost to IPv6 (::1) first, but uvicorn listens on IPv4 by default.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
