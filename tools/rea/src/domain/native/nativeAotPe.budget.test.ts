import { describe, expect, it } from "vitest";
import type { NativeAotParserLimits } from "./nativeAotPe.js";
import {
  NATIVE_AOT_MAX_REPORT_BYTES,
  parseNativeAotPe,
} from "./nativeAotPe.js";
import {
  nativeAotPeDigest,
  nativeAotPeFixture,
} from "../../../tests/fixtures/nativeaotPe.js";

const REPORT_BUDGET = NATIVE_AOT_MAX_REPORT_BYTES;

describe("NativeAOT parser resource budgets", () => {
  it("reports source-cap rejection without hashing or inventing omitted-byte coverage", () => {
    const bytes = nativeAotPeFixture();
    const sha = nativeAotPeDigest(bytes);
    const limits: NativeAotParserLimits = {
      sourceBytes: bytes.length - 1,
      workUnits: 1024,
      workingMemoryBytes: 1024,
    };
    const report = parseNativeAotPe(
      bytes,
      sha,
      REPORT_BUDGET,
      undefined,
      limits,
    ).summary;
    expect(report).toMatchObject({
      status: "partial",
      analysis_artifact_sha256: sha,
      header_address: null,
      coverage: {
        source_bytes_examined: 0,
        source_byte_budget_bytes: bytes.length - 1,
        truncation_reason: "source-byte-budget",
      },
    });
    expect(report.coverage.source_bytes_omitted).toBeUndefined();
  });
});

describe("NativeAOT parser report admission", () => {
  it("never returns an over-budget report from early exits", () => {
    const bytes = nativeAotPeFixture();
    const sha = nativeAotPeDigest(bytes);
    expect(() =>
      parseNativeAotPe(bytes, sha, 0, undefined, {
        sourceBytes: bytes.length - 1,
        workUnits: 1024,
        workingMemoryBytes: 1024,
      }),
    ).toThrow(/minimum truthful report/iu);
    const nonPe = Buffer.from("not a PE image");
    expect(() => parseNativeAotPe(nonPe, nativeAotPeDigest(nonPe), 0)).toThrow(
      /minimum truthful report/iu,
    );
  });

  it("does not claim a missing RTR header when signature coverage hits the work limit", () => {
    const bytes = nativeAotPeFixture();
    const limits: NativeAotParserLimits = {
      sourceBytes: bytes.length,
      workUnits: 8,
      workingMemoryBytes: 1024,
    };
    const report = parseNativeAotPe(
      bytes,
      nativeAotPeDigest(bytes),
      REPORT_BUDGET,
      undefined,
      limits,
    ).summary;
    expect(report).toMatchObject({
      status: "partial",
      header_address: null,
      truncated: true,
      coverage: {
        rtr_signature_scan_bytes_examined: 8,
        rtr_signature_scan_bytes_omitted: expect.any(Number),
        truncation_reason: "work-unit-budget",
      },
    });
    expect(report.reason).not.toMatch(/No supported ReadyToRun directory/iu);
  });

  it("does not claim a missing root when table discovery is incomplete", () => {
    const bytes = nativeAotPeFixture();
    const report = parseNativeAotPe(
      bytes,
      nativeAotPeDigest(bytes),
      REPORT_BUDGET,
      undefined,
      {
        sourceBytes: bytes.length,
        workUnits: 10_000,
        workingMemoryBytes: 1024,
      },
    ).summary;
    expect(report.truncated).toBe(true);
    expect(report.reason).toMatch(/before determining whether.*root exists/iu);
    expect(report.reason).not.toMatch(
      /no unique System\.Object.*fully examined/iu,
    );
  });
});

describe("NativeAOT detail and summary byte accounting", () => {
  it("admits serialized MethodTable memory descriptors before emitting them", () => {
    const bytes = nativeAotPeFixture();
    const sha = nativeAotPeDigest(bytes);
    const detail = (budget: number) =>
      parseNativeAotPe(bytes, sha, REPORT_BUDGET).readTypeDetail(
        "0x140002280",
        budget,
      );
    const complete = detail(REPORT_BUDGET);
    expect(complete?.derived_memory).not.toBeNull();
    let low = 0;
    let high = Buffer.byteLength(JSON.stringify(complete));
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (detail(middle)?.derived_memory != null) high = middle;
      else low = middle + 1;
    }
    const atBoundary = detail(low);
    const belowBoundary = detail(low - 1);
    expect(atBoundary?.derived_memory).not.toBeNull();
    expect(Buffer.byteLength(JSON.stringify(atBoundary))).toBeLessThanOrEqual(
      low,
    );
    expect(belowBoundary?.derived_memory ?? null).toBeNull();
    expect(belowBoundary?.diagnostics.join(" ")).toContain(
      "omitted because the final serialized report exceeded its byte budget",
    );
    expect(
      belowBoundary === undefined
        ? 0
        : Buffer.byteLength(JSON.stringify(belowBoundary)),
    ).toBeLessThanOrEqual(low - 1);
  });

  it("truncates report rows before exceeding its JSON byte budget", () => {
    const bytes = nativeAotPeFixture();
    const sha = nativeAotPeDigest(bytes);
    const complete = parseNativeAotPe(bytes, sha, REPORT_BUDGET);
    const fullBytes = Buffer.byteLength(JSON.stringify(complete.summary));
    let budget = fullBytes - 1;
    let summary: ReturnType<typeof parseNativeAotPe>["summary"] | undefined;
    while (budget > 0) {
      try {
        const candidate = parseNativeAotPe(bytes, sha, budget);
        if (candidate.summary.truncated) {
          summary = candidate.summary;
          break;
        }
      } catch {
        // Budgets below the fixed report header cannot carry an honest summary.
      }
      budget--;
    }
    if (summary === undefined)
      throw new Error("No partial summary budget found");
    expect(summary.truncated).toBe(true);
    expect(
      (summary.types_omitted ?? 0) +
        (summary.frozen_strings_omitted ?? 0) +
        (summary.coverage.scan_bytes_omitted ?? 0) +
        (summary.coverage.frozen_scan_bytes_omitted ?? 0),
    ).toBeGreaterThan(0);
    expect(Buffer.byteLength(JSON.stringify(summary))).toBeLessThanOrEqual(
      budget,
    );
  });
});
