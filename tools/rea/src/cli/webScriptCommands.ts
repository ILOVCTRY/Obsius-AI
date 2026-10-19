import { Cli, z } from "incur";

import { exportWebScripts } from "../application/WebScriptExportService.js";
import { ArtifactReaderFailure } from "../artifacts/ArtifactReader.js";
import { ArtifactResourceScope } from "../artifacts/ArtifactResourceScope.js";
import { CLI_COMMANDS } from "../cliCommandNames.js";
import { logCliCommand } from "../cliLogging.js";
import { analysisErrorWithCleanupFailure } from "../domain/analysisErrorCleanup.js";
import { projectAnalysisError } from "../domain/analysisErrorProjection.js";
import { ProviderCleanupError } from "../domain/providerCleanupError.js";
import type { Logger } from "pino";

/** Register the local captured-script export CLI workflow. */
export const registerWebScriptCommands = (
  cli: ReturnType<typeof Cli.create>,
  logger: Logger,
): void => {
  cli.command(CLI_COMMANDS.exportWebScripts, {
    description:
      "Export retained website scripts into an absent directory for static JavaScript analysis",
    args: z.object({
      capturePath: z
        .string()
        .describe("Absolute path to saved page or scenario capture JSON"),
      outputDirectory: z
        .string()
        .describe(
          "Absolute absent directory for verified scripts and manifest",
        ),
    }),
    run: ({ args }) =>
      logCliCommand(logger, CLI_COMMANDS.exportWebScripts, async () => {
        const resources = new ArtifactResourceScope();
        const result = await exportWebScripts(
          {
            capture_path: args.capturePath,
            output_directory: args.outputDirectory,
          },
          resources,
        );
        try {
          await resources.close();
        } catch (cause: unknown) {
          const cleanup = ArtifactReaderFailure.cleanupObservation(
            cause,
            "Web script export resources",
          );
          const failure = new ProviderCleanupError(
            "web-script-export",
            cleanup.resources,
            { reason: cleanup.reason },
            { cause, operation: "export_web_scripts" },
          );
          const error = result.ok
            ? failure
            : analysisErrorWithCleanupFailure(
                result.error,
                failure,
                "export_web_scripts",
              );
          return {
            error: "Script export failed",
            ...projectAnalysisError(error),
          };
        }
        return result.ok
          ? result.value
          : {
              error: "Script export failed",
              ...projectAnalysisError(result.error),
            };
      }),
  });
};
