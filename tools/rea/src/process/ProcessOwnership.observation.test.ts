import { describe, expect, it, vi } from "vitest";
import {
  cleanupOwnedProcessGroup,
  type ProcessOwnershipHost,
} from "./ProcessOwnership.js";
import {
  observeOwnedProcessGroups,
  observeOwnedProcessLineage,
} from "./ProcessOwnershipObservation.js";
import { host, ownership } from "./ProcessOwnership.fixture.js";

const processes = [100, 101].map((pid) => ({
  pid,
  parentPid: pid === 100 ? 1 : 100,
  processGroupId: 100,
  state: "S",
  command: "fixture",
}));

describe("multi-group ownership observation", () => {
  it("observes multiple groups from one process snapshot and token batch", async () => {
    const rows = [
      ...processes,
      {
        pid: 200,
        parentPid: 1,
        processGroupId: 200,
        state: "S",
        command: "other-owned-group",
      },
      {
        pid: 300,
        parentPid: 1,
        processGroupId: 300,
        state: "Z",
        command: "[exited]",
      },
    ];
    const listProcesses = vi.fn(() => Promise.resolve(rows));
    const environment = vi.fn(() => Promise.resolve({}));
    const runTokens = vi.fn<NonNullable<ProcessOwnershipHost["runTokens"]>>(
      (members) =>
        Promise.resolve(
          new Map(
            members.map(({ pid }) => [
              pid,
              {
                state: "readable" as const,
                runId: pid === 200 ? "other-run" : ownership.runId,
              },
            ]),
          ),
        ),
    );

    await expect(
      observeOwnedProcessGroups(ownership.runId, [100, 200, 300], {
        listProcesses,
        environment,
        runTokens,
        signalGroup: vi.fn(),
      }),
    ).resolves.toEqual(
      new Map([
        [100, { state: "alive" }],
        [
          200,
          { state: "unverifiable", reason: "process ownership did not match" },
        ],
        [300, { state: "empty" }],
      ]),
    );
    expect(listProcesses).toHaveBeenCalledTimes(1);
    expect(runTokens).toHaveBeenCalledTimes(1);
    expect(runTokens).toHaveBeenCalledWith(
      rows.filter(({ state }) => state !== "Z"),
    );
    expect(environment).not.toHaveBeenCalled();
  });

  it("rechecks unavailable members individually before classifying their groups", async () => {
    const rows = [
      ...processes,
      {
        pid: 200,
        parentPid: 1,
        processGroupId: 200,
        state: "S",
        command: "unreadable-group",
      },
    ];
    const listProcesses = vi
      .fn<ProcessOwnershipHost["listProcesses"]>()
      .mockResolvedValueOnce(rows)
      .mockResolvedValueOnce(rows.filter(({ pid }) => pid !== 101))
      .mockResolvedValueOnce(rows);
    const runTokens = vi.fn<NonNullable<ProcessOwnershipHost["runTokens"]>>(
      (members) =>
        Promise.resolve(
          new Map(
            members.map(({ pid }) => [
              pid,
              pid === 101 || pid === 200
                ? {
                    state: "unavailable" as const,
                    reason: `unreadable-${String(pid)}`,
                  }
                : { state: "readable" as const, runId: ownership.runId },
            ]),
          ),
        ),
    );

    await expect(
      observeOwnedProcessGroups(ownership.runId, [100, 200], {
        listProcesses,
        environment: vi.fn(() => Promise.resolve({})),
        runTokens,
        signalGroup: vi.fn(),
      }),
    ).resolves.toEqual(
      new Map([
        [100, { state: "alive" }],
        [
          200,
          {
            state: "unverifiable",
            reason:
              "process ownership could not be revalidated for PID 200: unreadable-200",
          },
        ],
      ]),
    );
    expect(listProcesses).toHaveBeenCalledTimes(3);
    expect(runTokens).toHaveBeenCalledTimes(1);
  });
});

describe("multi-group observation short-circuit", () => {
  it("stops missing-row fallbacks once every live group is unverifiable", async () => {
    const listProcesses = vi.fn(() => Promise.resolve(processes));
    const environment = vi.fn((pid: number) =>
      Promise.resolve({
        REA_PROCESS_RUN_ID: pid === 100 ? "other-run" : ownership.runId,
      }),
    );
    const runTokens = vi.fn<NonNullable<ProcessOwnershipHost["runTokens"]>>(
      () => Promise.resolve(new Map()),
    );

    await expect(
      observeOwnedProcessGroups(ownership.runId, [100], {
        listProcesses,
        environment,
        runTokens,
        signalGroup: vi.fn(),
      }),
    ).resolves.toEqual(
      new Map([
        [
          100,
          { state: "unverifiable", reason: "process ownership did not match" },
        ],
      ]),
    );
    expect(runTokens).toHaveBeenCalledTimes(1);
    expect(environment.mock.calls).toEqual([[100]]);
    expect(listProcesses).toHaveBeenCalledTimes(1);
  });
});

describe("batched ownership observation", () => {
  it("observes group and lineage with one batch each and falls back only for missing rows", async () => {
    const { adapter } = host({
      100: { REA_PROCESS_RUN_ID: ownership.runId },
      101: { REA_PROCESS_RUN_ID: ownership.runId },
    });
    const environment = vi.fn(adapter.environment);
    const runTokens = vi.fn<NonNullable<ProcessOwnershipHost["runTokens"]>>(
      () =>
        Promise.resolve(
          new Map([[100, { state: "readable", runId: ownership.runId }]]),
        ),
    );
    const observedHost = { ...adapter, environment, runTokens };
    await expect(
      observeOwnedProcessGroups(
        ownership.runId,
        [ownership.processGroupId],
        observedHost,
      ),
    ).resolves.toEqual(
      new Map([[ownership.processGroupId, { state: "alive" }]]),
    );
    await expect(
      observeOwnedProcessLineage(ownership, observedHost),
    ).resolves.toMatchObject({
      status: "verified",
      lineage: {
        descendants: [{ pid: 101, parentPid: 100, processGroupId: 100 }],
      },
    });
    expect(runTokens.mock.calls).toEqual([[processes], [processes]]);
    expect(environment.mock.calls).toEqual([[101], [101]]);
  });

  it.each(["batch", "environment"] as const)(
    "preserves cancellation during %s reads, including successful completion races",
    async (boundary) => {
      const controller = new AbortController();
      const reason = new Error("cancelled ownership read");
      const listProcesses = vi.fn(() => Promise.resolve(processes));
      const environment = vi.fn<ProcessOwnershipHost["environment"]>(
        async (_pid, signal) => {
          expect(signal).toBe(controller.signal);
          controller.abort(reason);
          return { REA_PROCESS_RUN_ID: ownership.runId };
        },
      );
      const runTokens = vi.fn<NonNullable<ProcessOwnershipHost["runTokens"]>>(
        async (_processes, signal) => {
          expect(signal).toBe(controller.signal);
          if (boundary === "batch") controller.abort(reason);
          return new Map();
        },
      );
      await expect(
        observeOwnedProcessGroups(
          ownership.runId,
          [ownership.processGroupId],
          { listProcesses, environment, runTokens, signalGroup: vi.fn() },
          controller.signal,
        ),
      ).rejects.toBe(reason);
      expect(listProcesses).toHaveBeenCalledTimes(1);
      expect(environment).toHaveBeenCalledTimes(boundary === "batch" ? 0 : 1);
    },
  );

  it.each([100, 101])(
    "rechecks unavailable batch rows for exited PID %i without overriding them through fallback",
    async (exitedPid) => {
      const listProcesses = vi
        .fn<ProcessOwnershipHost["listProcesses"]>()
        .mockResolvedValueOnce(processes)
        .mockResolvedValue(processes.filter(({ pid }) => pid !== exitedPid));
      const environment = vi.fn(() =>
        Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId }),
      );
      const observedHost: ProcessOwnershipHost = {
        listProcesses,
        environment,
        signalGroup: vi.fn(),
        runTokens: () =>
          Promise.resolve(
            new Map(
              processes.map(({ pid }) => [
                pid,
                pid === exitedPid
                  ? { state: "unavailable" as const, reason: "process exited" }
                  : { state: "readable" as const, runId: ownership.runId },
              ]),
            ),
          ),
      };
      const result = await observeOwnedProcessLineage(ownership, observedHost);
      if (exitedPid === 100)
        expect(result).toMatchObject({
          status: "unavailable",
          reason: "owned launcher exited during lineage validation",
        });
      else
        expect(result).toMatchObject({
          status: "verified",
          lineage: { descendants: [] },
        });
      expect(environment).not.toHaveBeenCalled();
    },
  );
});

describe("token sweep batch failures", () => {
  it("cleans token-owned groups when a failed batch has readable fallback observations", async () => {
    const { adapter, signalGroup } = host({
      100: { REA_PROCESS_RUN_ID: ownership.runId },
      101: { REA_PROCESS_RUN_ID: ownership.runId },
    });
    await expect(
      cleanupOwnedProcessGroup(
        { ...ownership, sweepTokenOwnedProcesses: true, captureBaseline: [] },
        {
          ...adapter,
          runTokens: () =>
            Promise.reject(new Error("batch reader unavailable")),
        },
      ),
    ).resolves.toEqual({ cleaned: true, signaled: true });
    expect(signalGroup.mock.calls).toEqual([[100, "SIGKILL"]]);
  });

  it("uses fallback ownership when the initial batch fails and preserves both failures through retries", async () => {
    const signalGroup = vi.fn();
    const adapter: ProcessOwnershipHost = {
      listProcesses: () => Promise.resolve(processes),
      environment: (pid) =>
        pid === 100
          ? Promise.resolve({ REA_PROCESS_RUN_ID: ownership.runId })
          : Promise.reject(new Error("individual unreadable")),
      runTokens: () => Promise.reject(new Error("batch reader unavailable")),
      signalGroup,
    };
    const result = await cleanupOwnedProcessGroup(
      { ...ownership, sweepTokenOwnedProcesses: true, captureBaseline: [] },
      adapter,
    );
    expect(result).toMatchObject({
      cleaned: false,
      failures: expect.arrayContaining([
        expect.objectContaining({
          pid: 101,
          diagnostic:
            "individual unreadable; run-token batch failed: batch reader unavailable",
        }),
      ]),
    });
    expect(signalGroup).not.toHaveBeenCalled();
  });
});
