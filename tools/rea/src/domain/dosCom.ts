import { z } from "zod";
import { err, ok, type Result } from "./result.js";

/** Headerless executable interpretation explicitly selected by the caller. */
export const executableFormatHintSchema = z.enum(["dos-com", "raw-binary"]);
/** Explicit executable interpretation for formats with no identifying header. */
export type ExecutableFormatHint = z.infer<typeof executableFormatHintSchema>;

/** Validate the single 64 KiB COM segment, excluding its 256-byte PSP prefix. */
export const validateDosComLength = (length: number): Result<null, string> =>
  Number.isSafeInteger(length) && length >= 1 && length <= 0xff00
    ? ok(null)
    : err(
        "DOS COM requires 1..65280 file bytes (64 KiB segment minus the 256-byte PSP prefix)",
      );
