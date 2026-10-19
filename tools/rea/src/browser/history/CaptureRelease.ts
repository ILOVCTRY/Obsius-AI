import { javaScriptRuntime } from "../../domain/runtimeVersion.js";
import { javaScriptWorkerMemoryLimitBytes } from "../../process/JavaScriptWorkerMemoryLimit.js";

/** Exact upstream package profile for unchanged offline HAR validation and numeric parsing. */
export const HAR_CAPTURE_PROVIDER_IDENTITY = {
  id: "har-schema/lossless-json",
  name: "REA HAR capture adapter",
  version: "har-schema@2.0.0;jsonc-parser@3.3.1;lossless-json@4.3.1",
} as const;

/** Fixed V8 heap settings used by the owned HAR decoder process. */
export const HAR_CAPTURE_HEAP_LIMITS = {
  oldGenerationMiB: 192,
  semiSpaceMiB: 8,
} as const;

/** The HAR decoder runs on this executable; Bun accepts but ignores V8 heap flags. */
export const HAR_CAPTURE_HEAP_ENFORCED =
  javaScriptRuntime(process.versions).id === "node";

/** Windows Job Object commit limit replacing the ignored heap flags under Bun; otherwise null. */
export const HAR_CAPTURE_PROCESS_MEMORY_LIMIT_BYTES =
  javaScriptWorkerMemoryLimitBytes(HAR_CAPTURE_HEAP_LIMITS.oldGenerationMiB);

/** Native executable profile verified with unchanged mitmproxy raw-state decoding. */
export const MITMPROXY_CAPTURE_PROVIDER_IDENTITY = {
  id: "mitmproxy-native-tnetstring",
  name: "REA offline native mitmproxy adapter",
  version: "12.2.3",
} as const;
