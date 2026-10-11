// Build + preview config for the e2e embed host page (`e2e-embed-host/`): the
// preview proxies two Servers behind one origin, authenticated as the host's user.
// The harness points `OMNIGENT_E2E_HOST_DIST` at a per-run output directory.
import fs from "node:fs";
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin, type ProxyOptions } from "vite";

const OUT_DIR = process.env.OMNIGENT_E2E_HOST_DIST
  ? path.resolve(process.env.OMNIGENT_E2E_HOST_DIST)
  : path.resolve(__dirname, "dist-e2e-embed-host");

function serverProxy(prefix: string, target: string, user: string): Record<string, ProxyOptions> {
  return {
    [prefix]: {
      target,
      changeOrigin: true,
      ws: true,
      rewrite: (requestPath) => requestPath.slice(prefix.length),
      configure: (proxy) => {
        proxy.on("proxyReq", (proxyReq) => proxyReq.setHeader("X-Forwarded-Email", user));
        proxy.on("proxyReqWs", (proxyReq) => proxyReq.setHeader("X-Forwarded-Email", user));
        proxy.on("proxyRes", (proxyRes, _req, res) => {
          if (String(proxyRes.headers["content-type"] ?? "").includes("text/event-stream")) {
            setImmediate(() => res.flushHeaders());
          }
        });
      },
    },
  };
}

const proxy = {
  ...serverProxy(
    "/server-a",
    process.env.OMNIGENT_E2E_SERVER_A_URL ?? "http://127.0.0.1:6767",
    process.env.OMNIGENT_E2E_SERVER_A_USER ?? "alice@example.test",
  ),
  ...serverProxy(
    "/server-b",
    process.env.OMNIGENT_E2E_SERVER_B_URL ?? "http://127.0.0.1:6768",
    process.env.OMNIGENT_E2E_SERVER_B_USER ?? "bob@example.test",
  ),
};

// The Vite root stays at `web/` so Tailwind scans `src/`; the host page is in a
// subdirectory, so its index.html is hoisted for the preview SPA fallback.
function hoistHostIndex(): Plugin {
  return {
    name: "hoist-e2e-embed-host-index",
    closeBundle() {
      fs.copyFileSync(
        path.join(OUT_DIR, "e2e-embed-host", "index.html"),
        path.join(OUT_DIR, "index.html"),
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), tailwindcss(), hoistHostIndex()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      input: path.resolve(__dirname, "e2e-embed-host/index.html"),
    },
    minify: false,
    reportCompressedSize: false,
  },
  server: { proxy },
  preview: { proxy },
});
