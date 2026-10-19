import { z } from "zod";
import { artifactIntegrityPolicySchema } from "../../domain/artifactIntegrityPolicy.js";
import { javaScriptAnalysisResourceControlsSchema } from "../../domain/javascript/javascriptAnalysisResourceControls.js";
import { javaScriptSourcePathsSchema } from "../../domain/javascript/javascriptSourceSelection.js";

/** Local ASAR/directory reconstruction request. */
export const javascriptArtifactReconstructionInputSchema = z.strictObject({
  input_path: z.string().min(1),
  format: z.enum(["auto", "asar", "directory"]).default("auto"),
  integrity_policy: artifactIntegrityPolicySchema,
  source_paths: javaScriptSourcePathsSchema.optional(),
  ...javaScriptAnalysisResourceControlsSchema.shape,
});

/** Parsed local reconstruction request. */
export type JavaScriptArtifactReconstructionInput = z.infer<
  typeof javascriptArtifactReconstructionInputSchema
>;
