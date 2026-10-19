import { z } from "zod";

/** Default absolute deadline for Ghidra import, analysis, bridge, and readiness. */
export const DEFAULT_GHIDRA_STARTUP_TIMEOUT_MS = 330_000;

/** Default absolute deadline for Hopper launch, analysis, bridge, and readiness. */
export const DEFAULT_HOPPER_STARTUP_TIMEOUT_MS = 120_000;

// Node schedules delays above this signed 32-bit boundary as a 1 ms timer.
const MAX_TIMER_DELAY_MS = 2_147_483_647;

/** Parse the optional setting without reading the ambient process environment. */
export const providerStartupTimeoutSchema = z
  .string()
  .regex(/^[1-9]\d*$/u, "must be a positive decimal integer")
  .transform(Number)
  .pipe(z.number().int().min(1).max(MAX_TIMER_DELAY_MS));
