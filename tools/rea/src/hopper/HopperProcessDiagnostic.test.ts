import { expect, it } from "vitest";

import type { HopperLauncherOutcome } from "../domain/hopperErrors.js";
import { nativeHopperLaunchWasDenied } from "./HopperProcessDiagnostic.js";

// Captured from Hopper 6.1.0-demo and 6.6.0-demo's unmodified helpers with native
// AppleScript error -1743 before open dispatch. The target script never ran.
const denied: HopperLauncherOutcome = {
  exit_code: 2,
  signal: null,
  stdout: { text: "", bytes: 0, retained_bytes: 0 },
  stderr: {
    text: "Cannot launch Hopper!\nNot authorized to send Apple events to current application.\n",
    bytes: 82,
    retained_bytes: 82,
  },
  output_closed: true,
  diagnostic_truncated: false,
};

it("recognizes the native helper's complete authorization rejection", () => {
  expect(nativeHopperLaunchWasDenied(denied)).toBe(true);
});

it.each([
  { output_closed: false },
  { diagnostic_truncated: true },
  { signal: "SIGTERM", exit_code: null },
  { signal: "SIGTERM", exit_code: 2 },
  { exit_code: 0 },
  {
    stderr: {
      ...denied.stderr,
      text: "Cannot launch Hopper!\nExpected end of line, etc. but found identifier.\n",
    },
  },
  {
    stderr: {
      ...denied.stderr,
      text: `${denied.stderr.text}another failure\n`,
    },
  },
  {
    stderr: {
      ...denied.stderr,
      text: "Not authorized to send Apple events to current application.\n",
    },
  },
])(
  "preserves unknown document ownership for incomplete or different failures: %j",
  (change) => {
    expect(nativeHopperLaunchWasDenied({ ...denied, ...change })).toBe(false);
  },
);
