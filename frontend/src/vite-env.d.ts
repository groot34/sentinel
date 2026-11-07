/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL for the Sentinel API; empty means same-origin (dev proxy). */
  readonly VITE_SENTINEL_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
