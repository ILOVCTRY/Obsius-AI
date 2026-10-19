import { z } from "zod";

/**
 * ISO-8601 date-time that requires an explicit UTC offset.
 *
 * `z.string().datetime({ offset: true })` emits Zod's offset class as `[+-]`.
 * Annex B and `u` mode accept that, but ECMA-262 `v` mode rejects the
 * unescaped `-`, so any tool whose schema carries the pattern is unusable for a
 * client that compiles advertised patterns — schema compilation happens before
 * arguments are supplied, so the whole tools request fails rather than one call.
 *
 * The accepted set is identical to the Zod validator; only the offset class
 * escapes `-`, as `\x2d`, because an escaped `-` inside a character class is
 * removed by `oxlint --fix` as a useless escape.
 */
export const offsetDateTimeSchema = z
  .string()
  .regex(
    /^(?:(?:\d\d[2468][048]|\d\d[13579][26]|\d\d0[48]|[02468][048]00|[13579][26]00)-02-29|\d{4}-(?:(?:0[13578]|1[02])-(?:0[1-9]|[12]\d|3[01])|(?:0[469]|11)-(?:0[1-9]|[12]\d|30)|(?:02)-(?:0[1-9]|1\d|2[0-8])))T(?:(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d+)?)?(?:Z|(?:[+\x2d](?:[01]\d|2[0-3]):[0-5]\d)))$/u,
    "Use an ISO-8601 date-time with an explicit UTC offset",
  );
