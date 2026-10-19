import { z } from "zod";

/** Shared provider search input fields. */
export const analysisSearchInput = {
  pattern: z
    .string()
    .min(1)
    .describe("The literal text or regex pattern to search for"),
  mode: z.enum(["literal", "regex"]).default("literal"),
  case_sensitive: z.boolean().default(false).describe("Whether to match case"),
  document: z.string().optional().describe("The document name"),
};
