import type {
  AnalysisExecution,
  ExecutionOptions,
} from "../AnalysisProvider.js";
import type { AnalysisError } from "../../domain/analysisErrorBase.js";
import type { InspectWasmArtifactInput } from "../../domain/wasm/wasmArtifact.js";
import type { Result } from "../../domain/result.js";

/** Replaceable offline WASM inspection engine, independent of target execution and network clients. */
export interface WasmArtifactPort {
  /** Retry removal of retained workspaces; failures preserve ownership for another attempt. */
  close(): Promise<void>;
  inspect(
    input: InspectWasmArtifactInput,
    options?: ExecutionOptions,
  ): Promise<Result<AnalysisExecution, AnalysisError>>;
}
