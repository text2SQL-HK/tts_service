import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  root: path.resolve(__dirname),
  base: command === "build" ? "/tts/static/" : "/",
  build: {
    outDir: path.resolve(__dirname, "../backend/static"),
    emptyOutDir: true,
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api/tts": "http://localhost:8000",
    },
  },
}));
