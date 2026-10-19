import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";
import { parseEvidence } from "../../../dist/domain/evidence.js";
import { hashAndroidFile } from "./digest.mjs";

/** Verify folded field assignments and remaining static-block calls through both public adapters. */
export const verifyAndroidInitializers = async ({
  apk,
  repository,
  entrypoint,
  environment,
  execute,
}) => {
  const digest = await hashAndroidFile(apk);
  const cases = [
    {
      class_name: "fixture.InitializationPolicy",
      witnesses: [
        "declared-policy",
        "->singletonList",
        "->POLICY:Ljava/util/List;",
      ],
    },
    {
      class_name: "fixture.MixedInitialization",
      witnesses: ["field-value", "->LABEL:Ljava/lang/String;", "->touch()V"],
    },
  ];
  const check = (value, scenario) => {
    const evidence = parseEvidence(value);
    assert.equal(evidence.subject.digest.sha256, digest);
    const result = evidence.normalized_result;
    assert.equal(result.class_name, scenario.class_name);
    assert.equal(result.method.name, "<clinit>");
    assert.equal(result.overload_count, 1);
    assert.equal(result.body_status, "available");
    assert.equal(result.representation, "smali");
    assert.equal(result.fell_back, true);
    assert.ok(result.markers.includes("static_initializer_disassembly"));
    assert.equal(result.source.status, "complete");
    for (const witness of scenario.witnesses)
      assert.ok(
        result.source.text.includes(witness),
        `Missing initializer evidence: ${witness}`,
      );
    return evidence;
  };
  const cli = new Map();
  for (const scenario of cases) {
    const response = await execute(
      process.execPath,
      [
        entrypoint,
        "inspect-android-method",
        apk,
        scenario.class_name,
        "<clinit>",
        "--format",
        "json",
      ],
      {
        cwd: repository,
        env: environment,
        timeout: 150_000,
        maxBuffer: 8 * 1024 * 1024,
      },
    );
    cli.set(scenario.class_name, check(JSON.parse(response.stdout), scenario));
  }
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [entrypoint, "mcp"],
    cwd: repository,
    env: environment,
    stderr: "pipe",
  });
  let stderr = "";
  transport.stderr?.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  const client = new Client({
    name: "rea-real-android-initializers",
    version: "1",
  });
  try {
    await client.connect(transport, { timeout: 30_000 });
    for (const scenario of cases) {
      const response = await client.callTool(
        {
          name: "inspect_android_method",
          arguments: {
            path: apk,
            class_name: scenario.class_name,
            method_name: "<clinit>",
          },
        },
        { timeout: 150_000 },
      );
      assert.notEqual(response.isError, true, JSON.stringify(response));
      const evidence = check(response.structuredContent, scenario);
      assert.deepEqual(
        evidence.normalized_result,
        cli.get(scenario.class_name).normalized_result,
      );
      assert.deepEqual(
        evidence.parameters,
        cli.get(scenario.class_name).parameters,
      );
      assert.deepEqual(
        evidence.provider,
        cli.get(scenario.class_name).provider,
      );
    }
    await client.ping();
  } catch (cause) {
    process.stderr.write(stderr);
    throw cause;
  } finally {
    await client.close();
  }
  assert.equal(
    await hashAndroidFile(apk),
    digest,
    "Static inspection changed fixture bytes",
  );
  console.log(
    "PASS real CLI/MCP static initializer evidence, identity, parity and unchanged APK",
  );
};
