import type {
  AnalysisExecution,
  ExecutionOptions,
} from "../AnalysisProvider.js";
import type { AndroidRequest } from "../../domain/android/androidAnalysis.js";
import type { BinaryTarget } from "../../domain/binaryTargetTypes.js";
import type { AnalysisError } from "../../domain/analysisErrorBase.js";
import type { Result } from "../../domain/result.js";
import type { ProviderAvailability } from "../AnalysisProvider.js";

/** Static Android inspection boundary; implementation details belong to the provider. */
export interface AndroidAnalysisPort {
  /** Inspect the selected JAR and Java runtime without opening an APK. */
  inspectAvailability(signal?: AbortSignal): Promise<ProviderAvailability>;
  /** Cancel active work and join cleanup of any retained provider resources. */
  close(): Promise<void>;
  execute(
    target: BinaryTarget,
    request: AndroidRequest,
    options?: ExecutionOptions,
  ): Promise<Result<AnalysisExecution, AnalysisError>>;
}
