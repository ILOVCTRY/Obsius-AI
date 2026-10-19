import { z } from "incur";

import { inspectAnalysisViewValidated } from "../application/analysisView/AnalysisViewService.js";
import { readEvidenceBundle } from "../application/EvidenceBundleFiles.js";
import { inspectEvidenceBundle } from "../application/investigation/InspectEvidenceBundle.js";
import {
  getEvidenceBundleInputSchema,
  importEvidenceBundleInputSchema,
} from "../contracts/sessionToolSchemas.js";
import { CLI_COMMANDS } from "../cliCommandNames.js";
import { parseCliJsonInput } from "../cliJsonInput.js";
import { logCliCommand } from "../cliLogging.js";
import { projectAnalysisError } from "../domain/analysisErrorProjection.js";
import { analysisInputErrorFromIssues } from "../domain/inputIssueProjection.js";
import { jsonValueSchema } from "../domain/jsonValue.js";
import type { Logger } from "pino";
import type { CliInstance } from "./types.js";

import { inspectAnalysisViewInputSchema } from "../domain/analysisView/analysisView.js";
/** Project selected views of completed analysis Evidence through the CLI. */
export const registerAnalysisViewCommands = (
  cli: CliInstance,
  logger: Logger,
): void => {
  cli.command(CLI_COMMANDS.inspectEvidenceBundle, {
    description:
      "Inspect complete or selected discovery metadata from a local Evidence bundle",
    args: z.object({
      inputJson: z
        .string()
        .describe(
          "JSON or JSON file with absolute path, detail and optional record filters",
        ),
    }),
    run: ({ args }) =>
      logCliCommand(logger, CLI_COMMANDS.inspectEvidenceBundle, async () => {
        const input = await parseCliJsonInput(
          args.inputJson,
          CLI_COMMANDS.inspectEvidenceBundle,
        );
        if (!input.ok) return input.error;
        const schema = getEvidenceBundleInputSchema.safeExtend({
          path: importEvidenceBundleInputSchema.shape.path,
        });
        const parsed = schema.safeParse(input.value);
        if (!parsed.success)
          return {
            error: "Evidence bundle inspection failed",
            ...projectAnalysisError(
              analysisInputErrorFromIssues(
                CLI_COMMANDS.inspectEvidenceBundle,
                parsed.error.issues,
                input.value,
              ),
            ),
          };
        const loaded = await readEvidenceBundle(parsed.data.path);
        if (!loaded.ok)
          return {
            error: "Evidence bundle inspection failed",
            ...projectAnalysisError(loaded.error),
          };
        const result = inspectEvidenceBundle(loaded.value, parsed.data);
        return result.ok
          ? result.value
          : {
              error: "Evidence bundle inspection failed",
              ...projectAnalysisError(result.error),
            };
      }),
  });
  cli.command(CLI_COMMANDS.inspectAnalysisView, {
    description:
      "Project a selected view of completed layout, JavaScript application or native function Evidence JSON",
    args: z.object({
      inputJson: z.string().describe("Inline workflow JSON or JSON file path"),
    }),
    run: ({ args }) =>
      logCliCommand(logger, CLI_COMMANDS.inspectAnalysisView, async () => {
        const input = await parseCliJsonInput(
          args.inputJson,
          CLI_COMMANDS.inspectAnalysisView,
        );
        if (!input.ok) return input.error;
        const parsed = inspectAnalysisViewInputSchema.safeParse(input.value);
        if (!parsed.success)
          return {
            error: "Application workflow failed",
            ...projectAnalysisError(
              analysisInputErrorFromIssues(
                CLI_COMMANDS.inspectAnalysisView,
                parsed.error.issues,
                input.value,
              ),
            ),
          };
        const result = inspectAnalysisViewValidated(parsed.data);
        return result.ok
          ? jsonValueSchema.parse(result.value)
          : {
              error: "Application workflow failed",
              ...projectAnalysisError(result.error),
            };
      }),
  });
};
