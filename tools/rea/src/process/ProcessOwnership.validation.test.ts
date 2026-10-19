import { describe, expect, it, vi } from "vitest";
import {
  cleanupOwnedProcessGroup,
  cleanupWindowsProcessTree,
  type ProcessOwnershipHost,
  type ProcessTableEntry,
  type WindowsProcessTreeHost,
} from "./ProcessOwnership.js";
import {
  observeOwnedProcessGroups,
  observeOwnedProcessLineage,
} from "./ProcessOwnershipObservation.js";
import { host, ownership } from "./ProcessOwnership.fixture.js";

const launcher: ProcessTableEntry = {
  pid: 100,
  parentPid: 1,
  processGroupId: 100,
  state: "S",
  command: "fixture",
};

describe("owned process-group cleanup validation: ownership and lineage", () => {
  it("fails closed when a descendant in another process group lacks the token", async () => {
    const adapter: ProcessOwnershipHost = {
      listProcesses: () =>
        Promise.resolve([
          launcher,
          {
            pid: 101,
            parentPid: 100,
            processGroupId: 101,
            state: "S",
            command: "child-session",
          },
        ]),
      environment: (pid) =>
        Promise.resolve({
          REA_PROCESS_RUN_ID: pid === 100 ? "run-token" : "other-run",
        }),
      signalGroup: vi.fn(),
    };
    await expect(
      observeOwnedProcessLineage(ownership, adapter),
    ).resolves.toEqual({
      status: "unavailable",
      observedAt: expect.any(String),
      runId: "run-token",
      launcherPid: 100,
      processGroupId: 100,
      reason: "process lineage contains an unowned or PID-reused process",
    });
  });
  it("does not publish lineage when any member fails ownership checks", async () => {
    const { adapter } = host({
      100: { REA_PROCESS_RUN_ID: "run-token" },
      101: { REA_PROCESS_RUN_ID: "other-run" },
    });
    await expect(
      observeOwnedProcessLineage(ownership, adapter),
    ).resolves.toEqual({
      status: "unavailable",
      observedAt: expect.any(String),
      runId: "run-token",
      launcherPid: 100,
      processGroupId: 100,
      reason: "process lineage contains an unowned or PID-reused process",
    });
  });
  it("fails closed for stale metadata or an unrelated concurrent process", async () => {
    const { adapter, signalGroup } = host({
      100: { REA_PROCESS_RUN_ID: "run-token" },
      101: { REA_PROCESS_RUN_ID: "different-run" },
    });
    expect(await cleanupOwnedProcessGroup(ownership, adapter)).toEqual({
      cleaned: false,
      reason: "process ownership token mismatch for PID(s): 101",
      failures: [{ pid: 101, reason: "run-token-mismatch" }],
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
  it("checks every member and aggregates ownership read failures", async () => {
    const environment = vi.fn((pid: number) => {
      if (pid === 100)
        return Promise.reject(new Error("transient procfs read"));
      return Promise.resolve(
        pid === 101
          ? { REA_PROCESS_RUN_ID: "different-run" }
          : { REA_PROCESS_RUN_ID: "run-token" },
      );
    });
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () =>
        Promise.resolve(
          [100, 101, 102].map((pid) => ({
            pid,
            parentPid: pid === 100 ? 1 : 100,
            processGroupId: 100,
            state: "S",
            command: "fixture",
          })),
        ),
      environment,
      signalGroup,
    };
    expect(await cleanupOwnedProcessGroup(ownership, adapter)).toEqual({
      cleaned: false,
      reason: "process ownership token mismatch for PID(s): 101",
      failures: [
        expect.objectContaining({
          pid: 100,
          reason: "environment-unreadable",
          diagnostic: "transient procfs read",
        }),
        { pid: 101, reason: "run-token-mismatch" },
      ],
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
});

describe("owned process-group cleanup liveness rechecks", () => {
  it("rechecks all unreadable members after the token phase in one fresh snapshot", async () => {
    const members = [100, 101, 102, 103].map((pid) => ({
      pid,
      parentPid: pid === 100 ? 1 : 100,
      processGroupId: 100,
      state: "S",
      command: "fixture",
    }));
    const tokenReads: number[] = [];
    const listProcesses = vi.fn(async () => {
      if (tokenReads.length > 0)
        expect(tokenReads).toEqual([100, 101, 102, 103]);
      return members;
    });
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses,
      environment: async (pid) => {
        tokenReads.push(pid);
        throw new Error(`unreadable ${pid}`);
      },
      signalGroup,
    };
    await expect(cleanupOwnedProcessGroup(ownership, adapter)).resolves.toEqual(
      {
        cleaned: false,
        reason:
          "process ownership token could not be read for 4 live process(es): other_unavailable=4; live candidates 100=other_unavailable, 101=other_unavailable, 102=other_unavailable, 103=other_unavailable",
        failures: members.map(({ pid }) => ({
          pid,
          reason: "environment-unreadable",
          diagnostic: `unreadable ${pid}`,
        })),
      },
    );
    expect(listProcesses).toHaveBeenCalledTimes(2);
    expect(signalGroup).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "retains mismatches and fails closed when the liveness recheck fails: %s",
    async (recheckFails) => {
      const members = [100, 101, 102].map((pid) => ({
        pid,
        parentPid: pid === 100 ? 1 : 100,
        processGroupId: 100,
        state: "S",
        command: "fixture",
      }));
      const listProcesses = vi
        .fn<ProcessOwnershipHost["listProcesses"]>()
        .mockResolvedValueOnce(members);
      if (recheckFails)
        listProcesses.mockRejectedValue(new Error("process table denied"));
      else listProcesses.mockResolvedValue([]);
      const signalGroup = vi.fn();
      const adapter: ProcessOwnershipHost = {
        listProcesses,
        environment: async (pid) => {
          if (pid === 101) return { REA_PROCESS_RUN_ID: "other-run" };
          throw new Error(`unreadable ${pid}`);
        },
        signalGroup,
      };
      await expect(
        cleanupOwnedProcessGroup(ownership, adapter),
      ).resolves.toEqual({
        cleaned: false,
        reason: "process ownership token mismatch for PID(s): 101",
        failures: recheckFails
          ? [
              {
                pid: 100,
                reason: "environment-unreadable",
                diagnostic:
                  "unreadable 100; process liveness recheck failed: process table denied",
              },
              { pid: 101, reason: "run-token-mismatch" },
              {
                pid: 102,
                reason: "environment-unreadable",
                diagnostic:
                  "unreadable 102; process liveness recheck failed: process table denied",
              },
            ]
          : [{ pid: 101, reason: "run-token-mismatch" }],
      });
      expect(listProcesses).toHaveBeenCalledTimes(2);
      expect(signalGroup).not.toHaveBeenCalled();
    },
  );

  it("accepts a member that exits during ownership revalidation", async () => {
    const listProcesses = vi
      .fn<ProcessOwnershipHost["listProcesses"]>()
      .mockResolvedValueOnce([launcher])
      .mockResolvedValue([]);
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses,
      environment: () =>
        Promise.reject(new Error("process exited before environment read")),
      signalGroup,
    };
    await expect(cleanupOwnedProcessGroup(ownership, adapter)).resolves.toEqual(
      {
        cleaned: true,
        signaled: false,
      },
    );
    expect(signalGroup).not.toHaveBeenCalled();
  });
});

describe("owned process-group cleanup validation: refreshed descendant lineage", () => {
  it.each(["planning", "signaling"])(
    "retains cleanup uncertainty for newly detached descendants during %s",
    async (phase) => {
      const child = { ...launcher, pid: 101, parentPid: 100 };
      const stale = { ...launcher, command: "[MainThread]" };
      const signalGroup = vi.fn();
      let reads = 0;
      const adapter: ProcessOwnershipHost = {
        listProcesses: () => {
          reads += 1;
          if (phase === "signaling" && reads === 1)
            return Promise.resolve([launcher, child]);
          if (reads === (phase === "planning" ? 1 : 2))
            return Promise.resolve([stale, child]);
          return Promise.resolve([
            { ...child, parentPid: 1 },
            { ...child, pid: 102, parentPid: 101 },
            { ...child, pid: 103, parentPid: 102, processGroupId: 103 },
          ]);
        },
        environment: () =>
          Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId }),
        signalGroup,
      };
      await expect(
        cleanupOwnedProcessGroup(
          { ...ownership, expectedCommand: "fixture" },
          adapter,
        ),
      ).resolves.toMatchObject({ cleaned: false });
      expect(signalGroup).not.toHaveBeenCalled();
    },
  );
});

describe("owned process-group cleanup validation: launcher exit races", () => {
  it.each([101, 999])(
    "retains cleanup uncertainty for a detached descendant in group %s after launcher exit",
    async (processGroupId) => {
      const child = {
        ...launcher,
        pid: 101,
        parentPid: 100,
        processGroupId: 101,
      };
      const signalGroup = vi.fn();
      const listProcesses = vi
        .fn<ProcessOwnershipHost["listProcesses"]>()
        .mockResolvedValueOnce([
          { ...launcher, command: "[MainThread]" },
          child,
        ])
        .mockResolvedValue([{ ...child, parentPid: 1, processGroupId }]);
      await expect(
        cleanupOwnedProcessGroup(
          {
            ...ownership,
            expectedCommand: "fixture",
          },
          {
            listProcesses,
            environment: () =>
              Promise.resolve({ REA_PROCESS_RUN_ID: "run-token" }),
            signalGroup,
          },
        ),
      ).resolves.toMatchObject({ cleaned: false });
      expect(signalGroup).not.toHaveBeenCalled();
    },
  );
  it.each(["planning", "signaling"])(
    "settles a stale launcher command during %s without skipping surviving group ownership",
    async (phase) => {
      for (const remaining of ["absent", "zombie", "owned", "foreign"]) {
        const stale = { ...launcher, command: "[MainThread]" };
        const child = { ...launcher, pid: 101, command: "child" };
        let reads = 0;
        const signalGroup = vi.fn();
        const adapter: ProcessOwnershipHost = {
          listProcesses: () => {
            reads += 1;
            if (phase === "signaling" && reads === 1)
              return Promise.resolve([launcher]);
            if (reads === (phase === "planning" ? 1 : 2))
              return Promise.resolve([stale]);
            return Promise.resolve(
              remaining === "absent"
                ? []
                : remaining === "zombie"
                  ? [{ ...stale, state: "Z" }]
                  : [child],
            );
          },
          environment: () =>
            Promise.resolve({
              REA_PROCESS_RUN_ID:
                remaining === "foreign" && reads > 1
                  ? "other-run"
                  : "run-token",
            }),
          signalGroup,
        };
        const result = await cleanupOwnedProcessGroup(
          { ...ownership, expectedCommand: "fixture" },
          adapter,
        );
        if (remaining === "foreign")
          expect(result).toMatchObject({
            cleaned: false,
            reason: "process ownership token mismatch for PID(s): 101",
          });
        else
          expect(result).toEqual({
            cleaned: true,
            signaled: remaining === "owned",
          });
        if (remaining === "owned")
          expect(signalGroup).toHaveBeenCalledWith(100, "SIGKILL");
        else expect(signalGroup).not.toHaveBeenCalled();
      }
    },
  );
  it("fails closed when a mismatched launcher's exit cannot be inspected", async () => {
    const { adapter, signalGroup } = host({
      100: { REA_PROCESS_RUN_ID: "run-token" },
    });
    const listProcesses = vi
      .fn<ProcessOwnershipHost["listProcesses"]>()
      .mockResolvedValueOnce(await adapter.listProcesses())
      .mockRejectedValue(new Error("process table unavailable"));
    await expect(
      cleanupOwnedProcessGroup(
        { ...ownership, expectedCommand: "/owned/hopper" },
        { ...adapter, listProcesses },
      ),
    ).resolves.toMatchObject({
      cleaned: false,
      reason: expect.stringContaining("command identity did not match"),
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
  it.each(["planning", "signaling"])(
    "rejects a surviving launcher PID in another group during %s",
    async (phase) => {
      let reads = 0;
      const signalGroup = vi.fn();
      const adapter: ProcessOwnershipHost = {
        listProcesses: () => {
          reads += 1;
          return Promise.resolve([
            phase === "signaling" && reads === 1
              ? launcher
              : reads === (phase === "planning" ? 1 : 2)
                ? { ...launcher, command: "[MainThread]" }
                : { ...launcher, processGroupId: 999 },
          ]);
        },
        environment: () => Promise.resolve({ REA_PROCESS_RUN_ID: "run-token" }),
        signalGroup,
      };
      await expect(
        cleanupOwnedProcessGroup(
          { ...ownership, expectedCommand: "fixture" },
          adapter,
        ),
      ).resolves.toMatchObject({
        cleaned: false,
        reason: expect.stringContaining("command identity did not match"),
      });
      expect(signalGroup).not.toHaveBeenCalled();
    },
  );
});

describe("owned process-group cleanup validation: exited members", () => {
  it("preserves cancellation during process-table observation instead of reporting unverifiable ownership", async () => {
    const controller = new AbortController();
    const reason = new Error("cancelled process observation");
    const adapter: ProcessOwnershipHost = {
      listProcesses: async (signal) => {
        controller.abort(reason);
        signal?.throwIfAborted();
        return [];
      },
      environment: () => Promise.resolve({}),
      signalGroup: () => {
        throw new Error("Observation must never signal processes");
      },
    };
    await expect(
      observeOwnedProcessGroups(
        ownership.runId,
        [ownership.processGroupId],
        adapter,
        controller.signal,
      ),
    ).rejects.toBe(reason);
  });
  it("does not inspect a process group after observation was cancelled", async () => {
    const controller = new AbortController();
    const reason = new Error("already cancelled observation");
    controller.abort(reason);
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => {
        throw new Error("Cancelled observation must not inspect processes");
      },
      environment: () => Promise.resolve({}),
      signalGroup: () => {
        throw new Error("Observation must never signal processes");
      },
    };
    await expect(
      observeOwnedProcessGroups(
        ownership.runId,
        [ownership.processGroupId],
        adapter,
        controller.signal,
      ),
    ).rejects.toBe(reason);
  });
  it("ignores exited zombie members during live ownership checks", async () => {
    const environment = vi.fn((pid: number) =>
      Promise.resolve(pid === 101 ? {} : { REA_PROCESS_RUN_ID: "run-token" }),
    );
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () =>
        Promise.resolve([
          launcher,
          {
            pid: 101,
            parentPid: 100,
            processGroupId: 100,
            state: "Z",
            command: "[node] <defunct>",
          },
        ]),
      environment,
      signalGroup,
    };
    expect(await cleanupOwnedProcessGroup(ownership, adapter)).toEqual({
      cleaned: true,
      signaled: true,
    });
    expect(environment.mock.calls).toEqual([[100], [100]]);
    expect(signalGroup).toHaveBeenCalledWith(100, "SIGKILL");
  });
  it("observes a zombie-only group as settled", async () => {
    const environment = vi.fn(() => Promise.resolve({}));
    const adapter: ProcessOwnershipHost = {
      listProcesses: () =>
        Promise.resolve([
          {
            pid: 101,
            parentPid: 1,
            processGroupId: 100,
            state: "Z+",
            command: "[node] <defunct>",
          },
        ]),
      environment,
      signalGroup: vi.fn(),
    };
    expect(
      await observeOwnedProcessGroups(
        ownership.runId,
        [ownership.processGroupId],
        adapter,
      ),
    ).toEqual(new Map([[ownership.processGroupId, { state: "empty" }]]));
    expect(environment).not.toHaveBeenCalled();
  });
  it("is idempotent when the owned group has already exited", async () => {
    const { adapter } = host({});
    expect(await cleanupOwnedProcessGroup(ownership, adapter)).toEqual({
      cleaned: true,
      signaled: false,
    });
  });
  it("fails closed when the launcher command identity changes", async () => {
    const { adapter, signalGroup } = host({
      100: { REA_PROCESS_RUN_ID: "run-token" },
    });
    expect(
      await cleanupOwnedProcessGroup(
        {
          ...ownership,
          expectedCommand: "/owned/hopper",
          expectedParentPid: 1,
        },
        adapter,
      ),
    ).toEqual({
      cleaned: false,
      reason:
        "owned launcher command identity did not match (observed=fixture; expected=/owned/hopper)",
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
  it("fails closed when the launcher parent identity changes", async () => {
    const { adapter, signalGroup } = host({
      100: { REA_PROCESS_RUN_ID: "run-token" },
    });
    expect(
      await cleanupOwnedProcessGroup(
        { ...ownership, expectedCommand: "fixture", expectedParentPid: 999 },
        adapter,
      ),
    ).toEqual({
      cleaned: false,
      reason: "owned launcher parent identity did not match",
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
});

describe("owned process-group cleanup token batches", () => {
  const root = {
    pid: 100,
    parentPid: 1,
    processGroupId: 100,
    state: "S",
    command: "fixture",
  };

  it("batches validation and falls back only for a missing member row", async () => {
    const child = { ...root, pid: 101, parentPid: root.pid };
    const runTokens = vi.fn(() =>
      Promise.resolve(
        new Map([
          [root.pid, { state: "readable" as const, runId: ownership.runId }],
        ]),
      ),
    );
    const environment = vi.fn(() =>
      Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId }),
    );
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => Promise.resolve([root, child]),
      environment,
      runTokens,
      signalGroup,
    };

    await expect(cleanupOwnedProcessGroup(ownership, adapter)).resolves.toEqual(
      { cleaned: true, signaled: true },
    );
    expect(runTokens).toHaveBeenCalledTimes(2);
    expect(runTokens).toHaveBeenNthCalledWith(1, [root, child]);
    expect(environment.mock.calls).toEqual([[child.pid], [child.pid]]);
    expect(signalGroup).toHaveBeenCalledWith(root.processGroupId, "SIGKILL");
  });

  it("keeps explicit unreadable rows fail-closed without environment fallback", async () => {
    const environment = vi.fn(() =>
      Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId }),
    );
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => Promise.resolve([root]),
      environment,
      runTokens: () =>
        Promise.resolve(
          new Map([
            [
              root.pid,
              { state: "unavailable" as const, reason: "fixture unreadable" },
            ],
          ]),
        ),
      signalGroup,
    };

    await expect(
      cleanupOwnedProcessGroup(ownership, adapter),
    ).resolves.toMatchObject({
      cleaned: false,
      failures: [
        {
          pid: root.pid,
          reason: "environment-unreadable",
          diagnostic: "fixture unreadable",
        },
      ],
    });
    expect(environment).not.toHaveBeenCalled();
    expect(signalGroup).not.toHaveBeenCalled();
  });

  it("falls back to the environment reader when the batch operation throws", async () => {
    const environment = vi.fn(() =>
      Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId }),
    );
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => Promise.resolve([root]),
      environment,
      runTokens: () => Promise.reject(new Error("batch reader unavailable")),
      signalGroup,
    };

    await expect(cleanupOwnedProcessGroup(ownership, adapter)).resolves.toEqual(
      { cleaned: true, signaled: true },
    );
    expect(environment).toHaveBeenCalledTimes(2);
    expect(signalGroup).toHaveBeenCalledWith(root.processGroupId, "SIGKILL");
  });

  it("retains both failures when batch and individual token reads fail", async () => {
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => Promise.resolve([root]),
      environment: () => Promise.reject(new Error("individual unreadable")),
      runTokens: () => Promise.reject(new Error("batch reader unavailable")),
      signalGroup,
    };

    await expect(
      cleanupOwnedProcessGroup(ownership, adapter),
    ).resolves.toMatchObject({
      cleaned: false,
      failures: [
        {
          pid: root.pid,
          reason: "environment-unreadable",
          diagnostic:
            "individual unreadable; run-token batch failed: batch reader unavailable",
        },
      ],
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
});

describe("Windows P0 process-tree cleanup", () => {
  it("reports whether taskkill signaled or found an exited tree", async () => {
    const terminated: WindowsProcessTreeHost = {
      terminateTree: () => Promise.resolve("terminated"),
    };
    const missing: WindowsProcessTreeHost = {
      terminateTree: () => Promise.resolve("missing"),
    };
    await expect(cleanupWindowsProcessTree(42, terminated)).resolves.toEqual({
      cleaned: true,
      signaled: true,
    });
    await expect(cleanupWindowsProcessTree(42, missing)).resolves.toEqual({
      cleaned: true,
      signaled: false,
    });
  });
  it("keeps invalid identity and termination failures explicit", async () => {
    const failing: WindowsProcessTreeHost = {
      terminateTree: () => Promise.reject(new Error("taskkill failed")),
    };
    await expect(cleanupWindowsProcessTree(0, failing)).resolves.toEqual({
      cleaned: false,
      reason: "Windows process-tree PID is invalid",
    });
    await expect(cleanupWindowsProcessTree(42, failing)).resolves.toEqual({
      cleaned: false,
      reason:
        "Windows process-tree termination failed for PID 42: taskkill failed",
    });
  });
});
