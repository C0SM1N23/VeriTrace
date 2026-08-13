import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { UI_PORT } from "./devserver";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    // Bind the v4 loopback explicitly: `localhost` resolves to ::1 on some
    // Windows setups, and then anything probing 127.0.0.1 is refused.
    // The port lives in `devserver.ts`, shared with the Playwright config and
    // overridable — see the note there about Windows reserving port ranges.
    host: "127.0.0.1",
    port: UI_PORT,
    // The dev server proxies to `veritrace serve` so the app talks to one
    // origin and the WebSocket needs no CORS dance.
    proxy: {
      "/session": { target: BACKEND, ws: true, changeOrigin: true },
      "/api": { target: BACKEND, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") },
    },
  },
  build: {
    // Straight into the Python package, so `pip install veritrace` ships a
    // working UI rather than an API with nothing in front of it. The directory
    // is generated, so it is gitignored and rebuilt by `make web-build`.
    outDir: "../python/veritrace/web",
    emptyOutDir: true,
    sourcemap: true,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
