import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Backend origin the dev server proxies API calls to. Overridable so the
// frontend never hard-codes a deployment-specific host.
const backendOrigin = process.env.SENTINEL_API_URL ?? "http://127.0.0.1:8000";

// Backend route prefixes proxied to the API. Proxying keeps the browser on a
// single origin during development, so no CORS configuration is required on the
// backend. If VITE_SENTINEL_API_BASE_URL is set to a full URL instead, the
// browser calls the backend directly and the backend must then allow CORS.
const proxiedPrefixes = ["/health", "/ready", "/incidents", "/investigations"];

const proxy = Object.fromEntries(
  proxiedPrefixes.map((prefix) => [
    prefix,
    {
      target: backendOrigin,
      changeOrigin: true,
      // Server-Sent Events must stream straight through without buffering.
      // http-proxy pipes the response, so disabling compression on the proxy
      // keeps the event-stream framing intact.
      ws: false,
    },
  ]),
);

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy,
  },
  preview: {
    port: 4173,
    proxy,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
