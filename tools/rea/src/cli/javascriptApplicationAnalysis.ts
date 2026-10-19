import { JAVASCRIPT_APPLICATION_PROVIDER } from "../application/InvestigationProviders.js";
import { createProgressReporter } from "../application/ProgressReporter.js";
import { ArtifactReaderFailure } from "../artifacts/ArtifactReader.js";
import { ArtifactResourceScope } from "../artifacts/ArtifactResourceScope.js";
import { analysisErrorWithCleanupFailure } from "../domain/analysisErrorCleanup.js";
import { projectAnalysisError } from "../domain/analysisErrorProjection.js";
import type { JsonValue } from "../domain/jsonValue.js";
import { ProviderCleanupError } from "../domain/providerCleanupError.js";

/** Execute the shared one-shot CLI boundary for static JavaScript analysis. */
export const runCliJavaScriptApplicationAnalysis = async (
  input: unknown,
  signal?: AbortSignal,
): Promise<JsonValue> => {
  const { analyzeJavaScriptApplication } =
    await import("../application/javascript/JavaScriptApplicationService.js");
  const progress = createProgressReporter(
    async (update) => {
      process.stderr.write(`${JSON.stringify({ rea_progress: update })}\n`);
    },
    { minimumIntervalMs: 0 },
  );
  const resources = new ArtifactResourceScope();
  const result = await analyzeJavaScriptApplication(input, resources, {
    progress,
    ...(signal === undefined ? {} : { signal }),
  });
  try {
    await resources.close();
  } catch (cause: unknown) {
    const cleanup = ArtifactReaderFailure.cleanupObservation(
      cause,
      "JavaScript application resources",
    );
    const failure = new ProviderCleanupError(
      JAVASCRIPT_APPLICATION_PROVIDER.id,
      cleanup.resources,
      { reason: cleanup.reason },
      { cause, operation: "analyze_javascript_application" },
    );
    return cliError(
      result.ok
        ? failure
        : analysisErrorWithCleanupFailure(
            result.error,
            failure,
            "analyze_javascript_application",
          ),
    );
  }
  return result.ok ? result.value : cliError(result.error);
};

const cliError = (
  error: Parameters<typeof projectAnalysisError>[0],
): JsonValue => ({
  error: "JavaScript application analysis failed",
  ...projectAnalysisError(error),
});
