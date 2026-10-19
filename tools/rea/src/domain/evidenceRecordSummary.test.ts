import { expect, it } from "vitest";

import { createEvidence } from "./evidence.js";
import { createEvidenceBundle } from "./evidenceBundle.js";
import { summarizeEvidenceBundle } from "./evidenceRecordSummary.js";

it("preserves native identity independently of missing or incompatible value-flow metadata", () => {
  const records = [
    undefined,
    { available: true, truncated: true },
    { unexpected: "legacy" },
    { available: false, reason: "Decompiler unavailable" },
  ].map((flow) =>
    createEvidence(
      { path: "/fixtures/native.exe", format: "pe", sha256: "a".repeat(64) },
      { id: "provider", name: "Provider", version: "1" },
      {
        operation: "analyze_function",
        parameters: {},
        result: {
          procedure: { address: "space:0x10", name: "entry" },
          ...(flow === undefined ? {} : { native_value_flow: flow }),
        },
      },
    ),
  );
  const summary = summarizeEvidenceBundle(createEvidenceBundle(records), {
    procedure_address: "space:0x10",
  });
  expect(summary.matching_records).toBe(4);
  const native = summary.records.map((r) => r.native_dossier);
  expect(
    native.every((r) => r.available && r.procedure_address === "space:0x10"),
  ).toBe(true);
  expect(native).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ value_flow: { status: "not-recorded" } }),
      expect.objectContaining({
        value_flow: {
          status: "observed",
          truncated: true,
          omitted_operations_lower_bound: null,
          known_omitted_inputs: null,
          known_omitted_edges: null,
        },
      }),
      expect.objectContaining({
        value_flow: { status: "unknown", reason: expect.any(String) },
      }),
      expect.objectContaining({
        value_flow: { status: "unavailable", reason: "Decompiler unavailable" },
      }),
    ]),
  );
});
