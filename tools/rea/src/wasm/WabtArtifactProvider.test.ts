import { access, chmod, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { expect, it } from "vitest";
import { createTestTempDirectory } from "../../tests/fixtures/temporaryDirectory.js";
import { WabtArtifactProvider } from "./WabtArtifactProvider.js";
import { PrivateRuntimeRoot } from "../process/PrivateRuntimeRoot.js";
import { spawnOwnedProviderProcess } from "../process/ProviderProcess.js";
import { OwnedCommandFailure } from "../process/OwnedCommand.js";

const failureTags: Readonly<Record<string, string>> = {
  invalid: "AnalysisInputError",
  timeout: "AnalysisTimeoutError",
  cancelled: "AnalysisCancelledError",
  "changed-snapshot": "AnalysisArtifactChangedError",
  cleanup: "ProviderAdapterError",
  malformed: "AnalysisOutputError",
  "malformed-wat": "AnalysisOutputError",
  "wat-warning": "AnalysisOutputError",
};

it("returns actionable unavailable without creating a workspace when tools are absent", async () => {
  const provider = new WabtArtifactProvider({}, undefined, () => {
    throw new Error("Must not allocate");
  });
  expect(
    await provider.inspect({
      path: "/module.wasm",
      glue_paths: [],
      candidate_paths: [],
    }),
  ).toMatchObject({
    ok: false,
    error: {
      _tag: "AnalysisCapabilityUnavailableError",
      userMessage: expect.stringContaining("REA_WABT_BIN_DIRECTORY"),
    },
  });
});
it.each([
  "success",
  "invalid",
  "malformed",
  "timeout",
  "cancelled",
  "cleanup",
  "changed-snapshot",
  "malformed-wat",
  "wat-warning",
])("owns private artifact bytes and retains %s outcome", async (mode) => {
  const selectedRoot = await createTestTempDirectory("rea-wabt-seam-");
  const path = join(selectedRoot, "selected.wasm");
  const bytes = Buffer.alloc(76, 1);
  await writeFile(path, bytes);
  for (const tool of ["wasm-validate", "wasm-objdump", "wasm2wat"])
    await writeFile(
      join(selectedRoot, tool + (process.platform === "win32" ? ".exe" : "")),
      "source-owned executable seam",
      { mode: 0o700 },
    );
  const dump = await readFile(
    new URL("../../tests/fixtures/wasm/objdump.txt", import.meta.url),
    "utf8",
  );
  let runtimePath = "";
  let acquiredPid: number | undefined;
  let cleanupAttempts = 0;
  const controller = new AbortController();
  const provider = new WabtArtifactProvider(
    { REA_WABT_BIN_DIRECTORY: selectedRoot },
    async (input) => {
      const version = input.arguments[0] === "--version";
      if (!version) {
        expect(await readFile(join(input.cwd ?? "", "module.wasm"))).toEqual(
          bytes,
        );
        if (mode === "changed-snapshot") {
          const snapshot = join(input.cwd ?? "", "module.wasm");
          await chmod(snapshot, 0o600);
          await writeFile(snapshot, Buffer.alloc(76, 2));
        }
        if (mode === "timeout")
          throw new OwnedCommandFailure(
            "timeout",
            "Source-owned deadline seam",
          );
      }
      const stdout = version
        ? "1.0.42\n"
        : input.command.includes("objdump")
          ? mode === "malformed"
            ? "unexpected producer output"
            : dump
          : input.command.includes("wasm2wat")
            ? mode === "malformed-wat"
              ? "unexpected decoder output\n"
              : '(module (@custom "test" "payload"))\n'
            : "";
      const invalid = !version && mode === "invalid";
      const stderr = invalid
        ? "module.wasm: invalid bytes\n"
        : !version &&
            input.command.includes("wasm2wat") &&
            mode === "wat-warning"
          ? "decoder warning\n"
          : "";
      const script =
        !version && mode === "cancelled"
          ? "setInterval(() => undefined, 1000)"
          : `process.stdout.write(${JSON.stringify(stdout)}); process.stderr.write(${JSON.stringify(stderr)}); process.exit(${invalid ? 1 : 0});`;
      const launched = await spawnOwnedProviderProcess({
        ...input,
        command: process.execPath,
        arguments: ["-e", script],
        expectedCommand: null,
      });
      if (!version && mode === "cancelled") {
        acquiredPid = launched.process.pid;
        setImmediate(() => controller.abort());
      }
      return launched;
    },
    async () => {
      const root = await PrivateRuntimeRoot.create({
        prefix: "rea-wabt-owned-test-",
      });
      runtimePath = root.path;
      return {
        path: root.path,
        close: async () => {
          if (mode === "cleanup" && ++cleanupAttempts <= 2)
            throw new Error("Source-owned cleanup uncertainty");
          await root.close();
        },
      };
    },
  );
  const result = await provider.inspect(
    { path, glue_paths: [], candidate_paths: [] },
    { signal: controller.signal },
  );
  if (mode === "success")
    expect(result).toMatchObject({
      ok: true,
      value: {
        result: {
          artifact: { path, bytes: 76 },
          validation: "valid",
          runtime_execution: "not-performed",
        },
      },
    });
  else
    expect(result).toMatchObject({
      ok: false,
      error: { _tag: failureTags[mode] },
    });
  if (mode === "invalid")
    expect(result).toMatchObject({
      error: { capturedOutput: { stderr: "module.wasm: invalid bytes\n" } },
    });
  if (mode === "cleanup") {
    expect(result).toMatchObject({
      error: {
        cleanupIncomplete: true,
        cleanupResources: [runtimePath],
        partialObservation: { normalized_result: { validation: "valid" } },
      },
    });
    await expect(access(runtimePath)).resolves.toBeUndefined();
    await expect(provider.close()).rejects.toMatchObject({
      cleanupIncomplete: true,
    });
    await expect(access(runtimePath)).resolves.toBeUndefined();
    await provider.close();
  }
  if (mode === "malformed-wat" || mode === "wat-warning")
    expect(result).toMatchObject({
      error: {
        capturedOutput: {
          stdout:
            mode === "malformed-wat"
              ? "unexpected decoder output\n"
              : '(module (@custom "test" "payload"))\n',
          stderr: mode === "wat-warning" ? "decoder warning\n" : "",
        },
      },
    });
  await expect(access(runtimePath)).rejects.toMatchObject({ code: "ENOENT" });
  expect(await readFile(path)).toEqual(bytes);
  if (acquiredPid !== undefined) {
    const pid = acquiredPid;
    expect(() => process.kill(pid, 0)).toThrow();
  }
});
