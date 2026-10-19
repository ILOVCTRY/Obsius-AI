import { describe, expect, it } from "vitest";

import { parseConfig } from "../config/parseConfig.js";
import { IdaProvider } from "./IdaProvider.js";

describe("IDA provider target support", () => {
  it("declines headerless raw-binary targets", () => {
    const config = parseConfig({});
    if (!config.ok) throw new Error("expected valid configuration");
    const provider = new IdaProvider(config.value, {});
    expect(
      provider.inspectTargetSupport({
        path: "/tmp/ecu.bin",
        sha256: "a".repeat(64),
        kind: "executable",
        format: "raw-binary",
        architecture: "arm",
        availableArchitectures: ["arm"],
        entryPoint: "0x8000000",
        entryPointSource: "0x8000000",
      }),
    ).toMatchObject({
      status: "unsupported",
      code: "target_format_unsupported",
      reason: expect.stringContaining("Ghidra"),
    });
  });
});
