import { Cli } from "incur";
import { z } from "zod";

import { captureBrowserScenario } from "./application/BrowserScenarioCaptureService.js";
import { withCommandCancellation } from "./cli/commandCancellation.js";
import { CLI_COMMANDS } from "./cliCommandNames.js";
import { parseCliJsonInput, resolveCliJsonPaths } from "./cliJsonInput.js";
import { logCliCommand } from "./cliLogging.js";
import { AnalysisInputError } from "./domain/analysisErrorCore.js";
import { AnalysisError } from "./domain/analysisErrorBase.js";
import { projectAnalysisError } from "./domain/analysisErrorProjection.js";
import { BrowserObservationError } from "./domain/browserObservationError.js";
import { projectInputIssues } from "./domain/inputIssueProjection.js";
import { browserScenarioSchema } from "./domain/browserScenario.js";
import type { JsonValue } from "./domain/jsonValue.js";
import type { Logger } from "pino";

const OPERATION = "capture_browser_scenario";

/** Register the one-shot controlled browser scenario command. */
export const registerBrowserScenarioCommands = (
  cli: ReturnType<typeof Cli.create>,
  logger: Logger,
  environment: Readonly<Record<string, string | undefined>>,
): void => {
  cli.command(CLI_COMMANDS.captureBrowserScenario, {
    description:
      "Run one bounded Playwright browser scenario and return Evidence",
    args: z.object({
      inputJson: z
        .string()
        .describe("Inline browser scenario JSON or JSON file path"),
    }),
    run: ({ args }) =>
      withCommandCancellation((signal) =>
        logCliCommand(logger, CLI_COMMANDS.captureBrowserScenario, async () => {
          const input = await parseCliJsonInput(args.inputJson, OPERATION);
          if (!input.ok) return input.error;
          const scenario = browserScenarioSchema.safeParse(
            resolveCliJsonPaths(input.value, [["browser", "executable_path"]]),
          );
          if (!scenario.success)
            return cliError(
              new AnalysisInputError(
                OPERATION,
                { cause: scenario.error },
                projectInputIssues(scenario.error.issues, input.value),
              ),
            );
          // Load Playwright only for this command, not during CLI startup.
          const { createBrowserScenarioProvider } =
            await import("./composition/browserScenario.js");
          const provider = createBrowserScenarioProvider(environment);
          let result:
            | Awaited<ReturnType<typeof captureBrowserScenario>>
            | undefined;
          try {
            result = await captureBrowserScenario(provider, scenario.data, {
              signal,
            });
            return result.ok ? result.value : cliError(result.error);
          } finally {
            try {
              await provider?.close?.();
            } catch (cause: unknown) {
              // Failed captures already retain their primary error and partial
              // observations. A cleanup retry must not replace that outcome.
              if (result?.ok)
                throw new BrowserObservationError(OPERATION, "cleanup_failed", {
                  cause,
                  cleanup:
                    cause instanceof AnalysisError &&
                    cause.cleanup !== undefined
                      ? cause.cleanup
                      : {
                          reason:
                            cause instanceof Error
                              ? cause.message
                              : String(cause),
                          resources:
                            cause instanceof AnalysisError &&
                            cause.cleanupResources.length > 0
                              ? cause.cleanupResources
                              : ["browser_transport"],
                        },
                  partialObservation: result.value,
                });
            }
          }
        }),
      ),
  });
};

const cliError = (
  error: Parameters<typeof projectAnalysisError>[0],
): JsonValue => ({
  error: "Browser scenario capture failed",
  ...projectAnalysisError(error),
});
