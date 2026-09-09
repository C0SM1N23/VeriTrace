import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { UI_PORT } from "./devserver";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  // FsmMode is a lazy entry. Without an explicit dependency hint, Vite first
  // discovers elk-api only when that mode opens and reloads the page after
  // optimising it, losing the user's first click in development.
  optimizeDeps: { include: ["elkjs/lib/elk-api.js"] },
  // The wave socket cannot go through the proxy (see `WS_ORIGIN` in
  // `api/client.ts`), so it needs the backend's real origin at build time. Baked
  // in from the same constant the proxy uses: when the two were allowed to drift
  // apart, moving the backend off 8765 left the socket dialling 8765 and the
  // waveform simply never arrived — a blank canvas with nothing in the console.
  define: { "import.meta.env.VITE_BACKEND": JSON.stringify(BACKEND) },
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
