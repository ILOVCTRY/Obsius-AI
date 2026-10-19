import { expect, it } from "vitest";

import {
  digestProcessCommitment,
  parseProcessScenario,
  PROCESS_TERMINAL_CELL_BUDGET,
  processComparisonContract,
  processScenarioCommitment,
  processScenarioSchema,
} from "./processScenario.js";

const baseScenario = { executable: "/usr/bin/true" };

it("defaults the finalization interval to zero without changing committed identity", () => {
  const scenario = parseProcessScenario(baseScenario);

  expect(scenario, "parsed scenario carries the default").toMatchObject({
    finalization_ms: 0,
  });
  expect(
    Object.hasOwn(processScenarioCommitment(scenario), "finalization_ms"),
    "full scenario commitment omits a zero interval",
  ).toBe(false);
  expect(
    Object.hasOwn(processComparisonContract(scenario), "finalization_ms"),
    "comparison contract omits a zero interval",
  ).toBe(false);
});

it("accepts an explicit zero finalization interval", () => {
  expect(
    parseProcessScenario({ ...baseScenario, finalization_ms: 0 }),
    "zero is the immediate-kill default, not an invalid budget",
  ).toMatchObject({ finalization_ms: 0 });
});

it("commits a positive finalization interval in both identity projections", () => {
  const scenario = parseProcessScenario({
    ...baseScenario,
    finalization_ms: 500,
  });

  expect(
    processScenarioCommitment(scenario),
    "full scenario commitment records the interval",
  ).toMatchObject({ finalization_ms: 500 });
  expect(
    processComparisonContract(scenario),
    "comparison contract records the interval",
  ).toMatchObject({ finalization_ms: 500 });
});

it.each([-1, 1.5, Number.MAX_SAFE_INTEGER + 1])(
  "rejects the finalization interval %j",
  (finalization_ms) => {
    expect(() =>
      parseProcessScenario({ ...baseScenario, finalization_ms }),
    ).toThrow();
  },
);

it("accepts a combined lifecycle budget up to the safe-integer limit", () => {
  const limit = Number.MAX_SAFE_INTEGER;
  const accepted = parseProcessScenario({
    ...baseScenario,
    timeout_ms: limit - 100,
    idle_timeout_ms: 1,
    settle_ms: 90,
    finalization_ms: 10,
  });

  expect(
    accepted.timeout_ms + accepted.finalization_ms + accepted.settle_ms,
    "a total exactly at the representable limit is accepted",
  ).toBe(limit);
  expect(
    parseProcessScenario(baseScenario),
    "the default scenario stays far below the limit",
  ).toMatchObject({ timeout_ms: 30_000, settle_ms: 100, finalization_ms: 0 });
});

it.each([
  ["timeout alone over the sum", { timeout_ms: Number.MAX_SAFE_INTEGER }],
  [
    "components that only overflow together",
    {
      timeout_ms: Number.MAX_SAFE_INTEGER - 10,
      settle_ms: 6,
      finalization_ms: 5,
    },
  ],
  [
    "a large settle and finalization",
    {
      timeout_ms: 5,
      settle_ms: Number.MAX_SAFE_INTEGER,
      finalization_ms: 1,
    },
  ],
])("rejects %s with a representability message", (_label, budget) => {
  const parse = () =>
    parseProcessScenario({ ...baseScenario, idle_timeout_ms: 1, ...budget });

  expect(parse, "the combined budget is rejected").toThrow(
    "timeout_ms + finalization_ms + settle_ms",
  );
  expect(parse, "the message names the representability limit").toThrow(
    "exactly representable",
  );
});

it("keeps the committed identity of a scenario without finalization byte for byte", () => {
  const scenario = parseProcessScenario({
    executable: "/usr/bin/true",
    arguments: ["-x"],
    working_directory: "/tmp",
    environment: { A: "b" },
    filesystem_observation_paths: ["/tmp"],
    timeout_ms: 5_000,
    idle_timeout_ms: 2_000,
    settle_ms: 50,
  });

  // Digests computed from the compiled base commit before finalization_ms existed.
  expect(
    digestProcessCommitment(processScenarioCommitment(scenario)),
    "the full scenario commitment is unchanged",
  ).toBe("b50005d26269bd1db4a49c79e05bca67c3a18d837b45cd1c8ff591c01eb7623a");
  expect(
    digestProcessCommitment(processComparisonContract(scenario)),
    "the comparison contract commitment is unchanged",
  ).toBe("675b759347a737a4f1cbfbf2fc759258d9c877216513f2f4dad3ed1e9e6e7631");
});

it("admits the default and the exact combined terminal-cell budget", () => {
  expect(parseProcessScenario({ executable: "/bin/true" }).terminal).toEqual({
    columns: 80,
    rows: 24,
    scrollback: 1_000,
  });

  expect(
    processScenarioSchema.safeParse({
      executable: "/bin/true",
      terminal: {
        columns: 1,
        rows: 1,
        scrollback: PROCESS_TERMINAL_CELL_BUDGET - 1,
      },
    }).success,
  ).toBe(true);
});

it("rejects an oversized initial buffer without clamping caller dimensions", () => {
  const input = {
    executable: "/bin/true",
    terminal: { columns: 1_000, rows: 1, scrollback: 1_000 },
  };
  const parsed = processScenarioSchema.safeParse(input);

  expect(parsed.success).toBe(false);
  if (parsed.success) throw new Error("expected terminal capacity rejection");
  expect(parsed.error.issues).toContainEqual(
    expect.objectContaining({
      code: "custom",
      path: ["terminal"],
      message: expect.stringContaining(
        `${String(PROCESS_TERMINAL_CELL_BUDGET)} renderer cells`,
      ),
    }),
  );
});

it("applies the selected scrollback budget to every scheduled resize", () => {
  const parsed = processScenarioSchema.safeParse({
    executable: "/bin/true",
    events: [{ type: "resize", at_ms: 10, columns: 1_000, rows: 1 }],
  });

  expect(parsed.success).toBe(false);
  if (parsed.success) throw new Error("expected resize capacity rejection");
  expect(parsed.error.issues).toContainEqual(
    expect.objectContaining({
      code: "custom",
      path: ["events", 0],
      message: expect.stringContaining("resize event at 10 ms"),
    }),
  );

  expect(
    processScenarioSchema.safeParse({
      executable: "/bin/true",
      terminal: { columns: 1, rows: 1, scrollback: 0 },
      events: [{ type: "resize", at_ms: 10, columns: 1_000, rows: 1 }],
    }).success,
  ).toBe(true);
});
