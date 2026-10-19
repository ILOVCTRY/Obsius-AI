import { isAbsolute, relative, sep } from "node:path";
import { z } from "zod";

/**
 * Reject OS-invalid NUL bytes in a local filesystem name. This is not a path
 * shape check: an empty or relative value is admitted here and narrowed by the
 * callers that need a nonempty or absolute path.
 *
 * The pattern spells NUL as `\x00` rather than `\u0000`. Both accept exactly the
 * same strings. The hex form also avoids the short `\0` spelling rejected by
 * strict schema consumers. A client can reject its entire tool request when
 * one advertised pattern cannot compile. See `offsetDateTimeSchema` for the
 * same hex-escape convention.
 */
export const nulFreeStringSchema = z
  .string()
  .regex(/^[^\x00]*$/u, "Local filesystem paths cannot contain NUL");

/** A selected local filesystem path: nonempty and free of NUL bytes. */
export const localPathStringSchema = nulFreeStringSchema.min(1);

/** Test lexical containment on the host platform; callers must resolve symlinks first. */
export const isPathWithinRoot = (root: string, path: string): boolean => {
  const value = relative(root, path);
  return (
    value === "" ||
    (value !== ".." && !value.startsWith(`..${sep}`) && !isAbsolute(value))
  );
};
