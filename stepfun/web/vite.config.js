import { defineConfig } from "vite";

const isolation = {
  "Cross-Origin-Opener-Policy": "same-origin",
  "Cross-Origin-Embedder-Policy": "require-corp",
};

export default defineConfig({
  server: {
    port: 5173,
    headers: isolation,
    proxy: {
      "/api": "http://127.0.0.1:8788",
      "/samples": "http://127.0.0.1:8788",
    },
  },
  preview: { headers: isolation },
  build: {
    target: "es2022",
    chunkSizeWarningLimit: 1800,
  },
});
