import { z } from "zod";

import { isCliOperationFailure } from "../cliLogging.js";

const referenceSourceImportFailureSchema = z.strictObject({
  error: z.literal("Import failed"),
  category: z.enum([
    "cancelled",
    "invalid_input",
    "unsupported_host",
    "execution_failure",
  ]),
  message: z.string().min(1),
  cleanup: z
    .strictObject({
      reason: z.string().min(1),
      resources: z.array(z.string().min(1)),
    })
    .optional(),
  partial: z.unknown().optional(),
});

/** Identify the historical-source import command's failed operation outputs. */
export const isReferenceSourceImportCliFailure = (value: unknown): boolean =>
  referenceSourceImportFailureSchema.safeParse(value).success ||
  isCliOperationFailure(value);
