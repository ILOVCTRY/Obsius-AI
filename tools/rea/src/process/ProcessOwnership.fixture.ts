import { vi } from "vitest";
import type { ProcessOwnershipHost } from "./ProcessOwnership.js";

export const ownership = {
  runId: "run-token",
  leaderPid: 100,
  processGroupId: 100,
};
export const host = (
  environments: Readonly<Record<number, Readonly<Record<string, string>>>>,
): {
  readonly adapter: ProcessOwnershipHost;
  readonly signalGroup: ReturnType<typeof vi.fn>;
} => {
  const signalGroup = vi.fn();
  return {
    adapter: {
      listProcesses: () =>
        Promise.resolve(
          Object.keys(environments).map((pid) => ({
            pid: Number(pid),
            parentPid: Number(pid) === 100 ? 1 : 100,
            processGroupId: 100,
            state: "S",
            command: "fixture",
          })),
        ),
      environment: (pid) => Promise.resolve(environments[pid] ?? {}),
      signalGroup,
    },
    signalGroup,
  };
};
