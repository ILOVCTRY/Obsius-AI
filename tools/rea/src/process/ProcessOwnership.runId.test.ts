import { expect, it } from "vitest";

import type { ProcessOwnershipHost } from "./ProcessOwnership.js";
import { readProcessRunId } from "./ProcessOwnership.js";

it("passes cancellation through run-token reads", async () => {
  const controller = new AbortController();
  const cancellation = new Error("cancelled");
  const host: ProcessOwnershipHost = {
    listProcesses: async () => [],
    environment: async (_pid, signal) => {
      if (signal === undefined) throw new Error("run-token signal was omitted");
      return await new Promise<Readonly<Record<string, string>>>(
        (_resolve, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), {
            once: true,
          });
        },
      );
    },
    signalGroup: () => undefined,
  };

  const reading = readProcessRunId(42, host, controller.signal);
  controller.abort(cancellation);

  await expect(reading).rejects.toBe(cancellation);
});
