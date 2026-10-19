import { describe, expect, it } from "vitest";

import { parseConfig } from "../config/parseConfig.js";
import { silentLogger } from "../logger.js";
import { HopperProvider } from "./HopperProvider.js";

describe("Hopper provider capabilities", () => {
  it("declines headerless raw-binary targets with a Ghidra-directed reason", () => {
    const config = parseConfig({});
    if (!config.ok) throw new Error("expected valid configuration");
    const provider = new HopperProvider(config.value, silentLogger, {});
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
  it("declines DOS MZ targets with a provider-specific support reason", () => {
    const config = parseConfig({});
    if (!config.ok) throw new Error("expected valid configuration");
    const provider = new HopperProvider(config.value, silentLogger, {});
    expect(
      provider.inspectTargetSupport({
        path: "/tmp/legacy.exe",
        sha256: "a".repeat(64),
        kind: "executable",
        format: "dos-mz",
        architecture: "x86",
        availableArchitectures: ["x86"],
      }),
    ).toMatchObject({
      status: "unsupported",
      code: "target_format_unsupported",
      reason: expect.stringContaining("Ghidra"),
    });
  });
});
