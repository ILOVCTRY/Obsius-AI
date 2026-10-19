import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { access, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { promisify } from "node:util";

import { Client } from "@modelcontextprotocol/client";
import { StdioClientTransport } from "@modelcontextprotocol/client/stdio";

import { CATALOG_IDENTITY } from "../dist/catalogIdentity.js";
import { RECONSTRUCTION_READINESS_EXAMPLE } from "../dist/contracts/reconstructionReadinessExample.js";
import { parseEvidence } from "../dist/domain/evidence.js";
import { createEvidenceBundle } from "../dist/domain/evidenceBundle.js";
import { createReconstructionReadinessReport } from "../dist/domain/reconstructionReadiness.js";
import { reconstructionReadinessReportSchema } from "../dist/domain/reconstructionReadinessSchemas.js";
import { PACKAGE_METADATA } from "../dist/generatedPackageMetadata.js";

const execute = promisify(execFile);
const root = process.cwd();
const nativePath = resolve(root, "build/conformance/c");
const fixtureRoot = resolve(root, "tests/conformance/readiness");
const javascriptPath = join(fixtureRoot, "javascript-cli");
const electronPath = join(fixtureRoot, "electron");
const environment = {
  ...process.env,
};
await access(nativePath);
const nativeEvidence = await runCli(["inspect-artifact", nativePath, "--json"]);
const javascriptCliEvidence = await runCli([
  "analyze-javascript-application",
  javascriptPath,
  "--json",
]);
const electronCliEvidence = await runCli([
  "analyze-javascript-application",
  electronPath,
  "--json",
]);

const transport = new StdioClientTransport({
  command: process.execPath,
  args: ["scripts/rea.mjs", "mcp"],
  cwd: root,
  env: environment,
  stderr: "pipe",
});
const client = new Client({
  name: "rea-readiness-verifier",
  version: "1",
  capabilities: { elicitation: {} },
});

try {
  await client.connect(transport);
  const javascriptMcp = await analyzeMcp(client, javascriptPath);
  const electronMcp = await analyzeMcp(client, electronPath);
  assert.equal(
    javascriptMcp.evidence_id,
    javascriptCliEvidence.evidence_id,
    "CLI/MCP JavaScript analysis Evidence identity drifted",
  );
  assert.equal(
    electronMcp.evidence_id,
    electronCliEvidence.evidence_id,
    "CLI/MCP Electron analysis Evidence identity drifted",
  );

  const input = structuredClone(RECONSTRUCTION_READINESS_EXAMPLE);
  input.identity.cli_version = PACKAGE_METADATA.version;
  input.identity.server_version = PACKAGE_METADATA.version;
  input.identity.catalog_digest = CATALOG_IDENTITY.digests.combined_sha256;
  const records = [
    ...input.evidence_bundle.records,
    nativeEvidence,
    javascriptCliEvidence,
    electronCliEvidence,
  ].map(parseEvidence);
  input.evidence_bundle = createEvidenceBundle(records);
  setFixtureDigest(input, "native", nativeEvidence);
  setFixtureDigest(input, "javascript-cli", javascriptCliEvidence);
  setFixtureDigest(input, "electron", electronCliEvidence);
  linkCheck(input, "discover-classify", "artifact-inventory", nativeEvidence);
  linkCheck(input, "static-analysis", "native-routed", nativeEvidence);
  linkCheck(
    input,
    "static-analysis",
    "javascript-routed",
    javascriptCliEvidence,
  );
  linkCheck(
    input,
    "reactive-scenarios",
    "electron-correlated",
    electronCliEvidence,
  );

  const readinessReport = reconstructionReadinessReportSchema.parse(
    createReconstructionReadinessReport(input),
  );
  assert.equal(readinessReport.status, "pass");
  if (process.env.REA_READINESS_REPORT_PATH !== undefined)
    await writeFile(
      resolve(root, process.env.REA_READINESS_REPORT_PATH),
      `${JSON.stringify(readinessReport, null, 2)}\n`,
    );

  const tampered = structuredClone(input);
  tampered.replay.expected_source_digest = readinessReport.source_digest;
  tampered.identity.server_version = "tampered";
  const tamperedReport = reconstructionReadinessReportSchema.parse(
    createReconstructionReadinessReport(tampered),
  );
  assert.equal(tamperedReport.status, "fail");
  assert.ok(
    tamperedReport.findings.some(
      ({ code }) => code === "replay-source-digest-mismatch",
    ),
  );

  process.stdout.write(
    `${JSON.stringify({
      status: readinessReport.status,
      report_id: readinessReport.report_id,
      report_digest: readinessReport.report_digest,
      stages: readinessReport.summary.stages,
      findings: readinessReport.summary.findings,
      native_evidence_id: nativeEvidence.evidence_id,
      javascript_evidence_id: javascriptCliEvidence.evidence_id,
      electron_evidence_id: electronCliEvidence.evidence_id,
      provider: "local-stdio",
    })}\n`,
  );
} finally {
  await client.close();
}

async function runCli(arguments_) {
  const { stdout } = await execute(
    process.execPath,
    ["scripts/rea.mjs", ...arguments_],
    {
      cwd: root,
      env: environment,
      maxBuffer: 64 * 1_024 * 1_024,
    },
  );
  return parseEvidence(JSON.parse(stdout));
}

async function analyzeMcp(client_, inputPath) {
  const response = await client_.callTool({
    name: "analyze_javascript_application",
    arguments: { input_path: inputPath },
  });
  assert.notEqual(response.isError, true);
  const projected = requireObject(response.structuredContent);
  return { evidence_id: projected.evidence_id };
}

function requireObject(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new TypeError("Expected an object");
  return value;
}

function setFixtureDigest(input, kind, evidence) {
  const fixture = input.fixtures.find(
    ({ kind: fixtureKind }) => fixtureKind === kind,
  );
  if (fixture === undefined || evidence.subject === null)
    throw new TypeError(`Missing ${kind} fixture identity`);
  fixture.artifact_sha256 = evidence.subject.digest.sha256;
}

function linkCheck(input, stageId, checkId, evidence) {
  const stage = input.stages.find(({ stage_id: id }) => id === stageId);
  const check = stage?.checks.find(({ check_id: id }) => id === checkId);
  if (stage === undefined || check === undefined)
    throw new TypeError(`Missing readiness check ${stageId}/${checkId}`);
  stage.evidence_ids = [
    ...new Set([...stage.evidence_ids, evidence.evidence_id]),
  ].sort();
  check.evidence_ids = [evidence.evidence_id];
}
