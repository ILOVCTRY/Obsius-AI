import type { BinaryTarget } from "../../domain/binaryTargetTypes.js";

/** Preserve the admitted target interpretation in public lifecycle results. */
export const binaryTargetSummary = (target: BinaryTarget) => ({
  path: target.path,
  format: target.format,
  kind: target.kind,
  sha256: target.sha256,
  architecture: target.architecture ?? null,
  ...(target.format === "raw-binary" ? { entry_point: target.entryPoint } : {}),
});
