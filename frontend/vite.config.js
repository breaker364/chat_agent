import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";

const runtimeConfigPath = path.resolve(__dirname, "..", "runtime_config.json");
const runtimeConfig = fs.existsSync(runtimeConfigPath)
  ? JSON.parse(fs.readFileSync(runtimeConfigPath, "utf-8"))
  : {};
const appConfig = runtimeConfig.app || {};
const frontendPort = Number(appConfig.frontend_port || 5173);
const backendHost = appConfig.backend_host || "127.0.0.1";
const backendPort = Number(appConfig.backend_port || 8000);
const backendTarget = `http://${backendHost}:${backendPort}`;

export default defineConfig({
  plugins: [react()],
  server: {
    port: frontendPort,
    proxy: {
      "/chat": backendTarget,
      "/health": backendTarget,
      "/sessions": backendTarget,
      "/skills": backendTarget,
      "/feishu": backendTarget,
      "/uploads": backendTarget,
    },
  },
});
