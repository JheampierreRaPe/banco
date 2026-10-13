import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Panel interno admin-web (E3-T10). Sirve en :8080 vía docker compose
// (servicio `admin-web`). El backend se resuelve con `VITE_API_BASE_URL`
// (por defecto, mismo origen + `/api/v1`).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: false,
  },
});
