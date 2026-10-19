import type { ArtifactInventorySnapshot } from "../../domain/artifactInventorySnapshot.js";
import type { ApplicationNode } from "../../domain/javascript/javascriptApplicationGraphSchemas.js";
import { completeApplicationCoverage } from "../../domain/javascript/javascriptApplicationEvidenceSchemas.js";
import type { JavaScriptModuleArtifactAnalysis } from "./JavaScriptArtifactAnalysisTypes.js";
import { jsonValueSchema } from "../../domain/jsonValue.js";
import { freezeJsonSnapshot } from "../../domain/immutableJson.js";
import type { JavaScriptSourceRange } from "../../domain/javascript/javascriptStaticAnalysisTypes.js";
import type {
  JavaScriptArtifactContainer,
  JavaScriptArtifactFile,
} from "../../domain/javascript/javascriptArtifactFiles.js";
import type { JavaScriptArtifactGraphAccumulator } from "./JavaScriptArtifactGraphAccumulator.js";
import {
  addArtifactContainsEdge,
  addUnavailableStaticParseScope,
  artifactFileNodeKind,
  artifactLocalIdentity,
  javascriptAnalysisCoverage,
  createElectronRoleNode,
  linkElectronRoleToAsset,
  type JavaScriptArtifactGraphContext,
} from "./JavaScriptArtifactGraphContext.js";
import {
  artifactObservationEvidence,
  astObservationEvidence,
  staticInferenceEvidence,
} from "./JavaScriptArtifactGraphEvidence.js";
import { resolveArtifactPathByContext } from "./JavaScriptArtifactPathResolution.js";
import { compareUnicodeCodePoints } from "../../domain/unicodeCodePointOrder.js";

interface PackageRoleInput {
  readonly packageNode: ApplicationNode;
  readonly packageFile: JavaScriptArtifactFile;
  readonly kind: "electron-main" | "electron-renderer";
  readonly declaredPath: string | null;
}

/** Create the exact root artifact node from the inventory manifest. */
export const createJavaScriptArtifactRootNode = (
  accumulator: JavaScriptArtifactGraphAccumulator,
  snapshot: ArtifactInventorySnapshot,
): ApplicationNode => {
  const root = snapshot.nodes.find(
    ({ artifact_id: id }) => id === snapshot.manifest.root_artifact_id,
  );
  if (root === undefined)
    throw new TypeError("Artifact inventory root is missing");
  return accumulator.addNode({
    kind: "artifact",
    identity: {
      strategy: "content-digest",
      stability: "global-exact",
      sha256: root.sha256,
    },
    observations: [
      {
        label: "artifact root",
        properties: {
          format: snapshot.manifest.root_format,
          bytes: root.size,
          inventory_manifest_id: snapshot.manifest.manifest_id,
          inventory_graph_sha256: snapshot.manifest.graph_sha256,
          inventory_artifact_id: root.artifact_id,
        },
        evidence: artifactObservationEvidence({
          sha256: root.sha256,
          path: "artifact-root",
          operation: "inventory-root",
          coverage: completeApplicationCoverage(),
          limitations: [
            "artifact-root is a path-independent alias; the application result retains the canonical local input path.",
          ],
        }),
      },
    ],
  });
};

/** Project nested ASAR containers inventoried inside a directory. */
export const addJavaScriptArtifactContainers = (
  context: JavaScriptArtifactGraphContext,
): void => {
  for (const container of context.fileSet.containers) {
    const node = context.accumulator.addNode({
      kind: "artifact",
      identity: {
        strategy: "content-digest",
        stability: "global-exact",
        sha256: container.sha256,
      },
      observations: [
        {
          label: container.path,
          properties: containerProperties(container),
          evidence: artifactObservationEvidence({
            sha256: container.sha256,
            path: container.path,
            operation: "inventory-nested-asar",
            coverage: completeApplicationCoverage(),
          }),
        },
      ],
    });
    context.containerNodes.set(container.sha256, node);
    context.accumulator.addEdge({
      source_node_id: context.root.node_id,
      target_node_id: node.node_id,
      relation: "contains",
      properties: { path: container.path, format: "asar" },
      evidence: artifactObservationEvidence({
        sha256: context.snapshot.manifest.root_sha256,
        path: container.path,
        operation: "inventory-nested-asar",
        coverage: completeApplicationCoverage(),
      }),
    });
  }
};

/** Project relevant files, ASAR entries, and explicit unavailable parse scopes. */
export const addJavaScriptArtifactFiles = (
  context: JavaScriptArtifactGraphContext,
): void => {
  const packagesByPath = indexFirstByPath(context.analysis.packages);
  const jsonModulesByPath = indexFirstByPath(context.analysis.json_modules);
  const sourceMapsByPath = indexFirstByPath(context.analysis.source_maps);
  for (const analyzed of context.analysis.files) {
    const { file } = analyzed;
    const jsonValue = jsonModulesByPath.get(file.path);
    const target = createFileTarget(
      context,
      file,
      analyzed.javascript,
      jsonValue,
    );
    if (
      analyzed.application_projection_failure !== undefined &&
      analyzed.javascript !== null
    )
      context.accumulator.addNode({
        kind: target.kind,
        identity: target.identity,
        observations: [
          {
            label: file.path,
            properties: {
              static_analysis: freezeJsonSnapshot(
                jsonValueSchema.parse(analyzed.javascript),
              ),
              semantic_module_analysis:
                analyzed.semantic === null
                  ? null
                  : freezeJsonSnapshot(
                      jsonValueSchema.parse(analyzed.semantic.ir),
                    ),
              application_projection_failure:
                analyzed.application_projection_failure.error,
            },
            evidence: astObservationEvidence({
              sha256: file.sha256,
              path: file.path,
              operation: "retain-unexpanded-static-findings",
              range: sourceFileRange(file),
              coverage: javascriptAnalysisCoverage(analyzed.javascript),
              limitations: [analyzed.application_projection_failure.reason],
            }),
          },
        ],
      });
    context.fileNodes.set(file.path, target);
    if (file.kind === "javascript") context.assetNodes.set(file.path, target);
    const entry = createAsarEntry(context, file);
    const parent =
      context.containerNodes.get(file.container_sha256) ?? context.root;
    if (entry === undefined)
      addArtifactContainsEdge(context, {
        source: parent,
        target,
        file,
        operation: "inventory-file",
      });
    else {
      addArtifactContainsEdge(context, {
        source: parent,
        target: entry,
        file,
        operation: "inventory-entry",
      });
      context.accumulator.addEdge({
        source_node_id: entry.node_id,
        target_node_id: target.node_id,
        relation: "maps_to",
        properties: { sha256: file.sha256 },
        evidence: artifactObservationEvidence({
          sha256: file.container_sha256,
          path: file.path,
          operation: "map-entry-content",
          coverage: completeApplicationCoverage(),
        }),
      });
    }
    if (!file.text.included && file.text.reason === "outside-analysis-scope")
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "select-javascript-sources",
        limitation:
          "Source is outside the caller-selected source_paths; it was inventoried but not analyzed.",
      });
    else if (analyzed.analysis_failure !== undefined)
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "analyze-javascript-source",
        limitation: analyzed.analysis_failure.reason,
        failure: analyzed.analysis_failure.error,
        limits: javaScriptAnalysisCoverageLimits(
          analyzed.analysis_failure.limits,
        ),
      });
    else if (
      file.kind === "javascript" &&
      (analyzed.javascript === null ||
        analyzed.javascript.parse_status === "failed")
    )
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "parse-javascript",
        limitation: file.text.included
          ? "JavaScript syntax could not be parsed."
          : `JavaScript text was unavailable: ${file.text.reason}.`,
      });
    if (analyzed.application_projection_failure !== undefined)
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "project-javascript-application",
        limitation: analyzed.application_projection_failure.reason,
        failure: analyzed.application_projection_failure.error,
        limits: javaScriptAnalysisCoverageLimits(
          analyzed.application_projection_failure.limits,
        ),
      });
    const packageValue = packagesByPath.get(file.path);
    if (packageValue !== undefined && packageValue.status !== "included")
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "parse-package-json",
        limitation: packageValue.limitation,
      });
    if (jsonValue !== undefined && jsonValue.status !== "included")
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "parse-json-module",
        limitation: jsonValue.limitation,
      });
    const sourceMap = sourceMapsByPath.get(file.path);
    if (sourceMap !== undefined && sourceMap.status === "invalid")
      addUnavailableStaticParseScope(context, {
        file,
        asset: target,
        operation: "parse-local-source-map",
        limitation: sourceMap.limitation,
      });
  }
};

const sourceFileRange = (
  file: JavaScriptArtifactFile,
): JavaScriptSourceRange => {
  if (!file.text.included)
    throw new TypeError("Retained static facts require their source text");
  let line = 1;
  let lastLineStart = 0;
  for (const match of file.text.value.matchAll(/\r\n|[\n\r\u2028\u2029]/gu)) {
    line += 1;
    lastLineStart = match.index + match[0].length;
  }
  return {
    start: { line: 1, column: 0 },
    end: { line, column: file.text.value.length - lastLineStart },
  };
};

/** Project package metadata and its declared Electron roles. */
export const addJavaScriptPackageNodes = (
  context: JavaScriptArtifactGraphContext,
): ApplicationNode[] => {
  const roots: ApplicationNode[] = [];
  const rootPath = applicationManifestPath(context);
  for (const packageValue of context.analysis.packages) {
    const file = context.filesByPath.get(packageValue.path);
    if (file === undefined) continue;
    const node = context.accumulator.addNode({
      kind: "package",
      identity: artifactLocalIdentity(file.sha256, "package-json", file.path),
      observations: [
        {
          label: packageValue.name ?? file.path,
          properties: {
            path: file.path,
            name: packageValue.name,
            version: packageValue.version,
            main: packageValue.main,
            renderer: packageValue.renderer,
            parse_status: packageValue.status,
          },
          evidence: artifactObservationEvidence({
            sha256: file.sha256,
            path: file.path,
            operation: "parse-package-json",
            coverage: completeApplicationCoverage(),
            limitations:
              packageValue.limitation === null ? [] : [packageValue.limitation],
          }),
        },
      ],
    });
    if (file.path === rootPath) {
      roots.push(node);
      context.accumulator.addEdge({
        source_node_id: node.node_id,
        target_node_id: context.root.node_id,
        relation: "contains",
        properties: { basis: "package-metadata-within-artifact" },
        evidence: staticInferenceEvidence({
          sha256: file.sha256,
          path: file.path,
          operation: "associate-package-artifact",
          coverage: completeApplicationCoverage(),
        }),
      });
    } else
      addArtifactContainsEdge(context, {
        source: context.root,
        target: node,
        file,
        operation: "inventory-package",
      });
    if (dependencyManifest(file.path)) continue;
    addPackageRole(context, {
      packageNode: node,
      packageFile: file,
      kind: "electron-main",
      declaredPath: packageValue.main,
    });
    addPackageRole(context, {
      packageNode: node,
      packageFile: file,
      kind: "electron-renderer",
      declaredPath: packageValue.renderer,
    });
  }
  return roots;
};

/** Dependency-only artifacts retain their inventory root. */
const applicationManifestPath = (
  context: JavaScriptArtifactGraphContext,
): string | undefined =>
  context.analysis.packages
    .map(({ path }) => path)
    .filter(
      (path) => context.filesByPath.has(path) && !dependencyManifest(path),
    )
    .sort(
      (left, right) =>
        left.split("/").length - right.split("/").length ||
        compareUnicodeCodePoints(left, right),
    )[0];

const dependencyManifest = (path: string): boolean =>
  path.split("/").slice(0, -1).includes("node_modules");

const createFileTarget = (
  context: JavaScriptArtifactGraphContext,
  file: JavaScriptArtifactFile,
  javascript: JavaScriptModuleArtifactAnalysis["files"][number]["javascript"],
  json: JavaScriptModuleArtifactAnalysis["json_modules"][number] | undefined,
): ApplicationNode => {
  const kind = artifactFileNodeKind(file.kind);
  return context.accumulator.addNode({
    kind,
    identity: {
      strategy: "content-digest",
      stability: "global-exact",
      sha256: file.sha256,
    },
    observations: [
      {
        label: file.path,
        properties: {
          path: file.path,
          bytes: file.bytes,
          inventory_artifact_id: file.inventory_artifact_id,
          unpacked: file.unpacked,
          file_kind: file.kind,
          text_status: file.text.included ? "included" : file.text.reason,
          parse_status: javascript?.parse_status ?? null,
          json_parse_status: json?.status ?? null,
          json_top_level_keys: json?.top_level_keys ?? [],
          omitted_json_top_level_keys: json?.omitted_top_level_keys ?? 0,
          vendor_markers: javascript?.vendors ?? [],
        },
        evidence: artifactObservationEvidence({
          sha256: file.sha256,
          path: file.path,
          operation: "inventory-relevant-file",
          coverage: completeApplicationCoverage(),
        }),
      },
    ],
  });
};

const indexFirstByPath = <Value extends { readonly path: string }>(
  values: readonly Value[],
): ReadonlyMap<string, Value> => {
  const index = new Map<string, Value>();
  for (const value of values) {
    if (!index.has(value.path)) index.set(value.path, value);
  }
  return index;
};

const createAsarEntry = (
  context: JavaScriptArtifactGraphContext,
  file: JavaScriptArtifactFile,
): ApplicationNode | undefined => {
  const isAsar =
    context.snapshot.manifest.root_format === "asar" ||
    file.container_sha256 !== context.snapshot.manifest.root_sha256;
  if (!isAsar) return undefined;
  return context.accumulator.addNode({
    kind: "asar-entry",
    identity: {
      strategy: "canonical-path",
      stability: "artifact-version",
      artifact_sha256: file.container_sha256,
      path: file.path,
    },
    observations: [
      {
        label: file.path,
        properties: {
          entry_sha256: file.sha256,
          bytes: file.bytes,
          unpacked: file.unpacked,
          inventory_artifact_id: file.inventory_artifact_id,
        },
        evidence: artifactObservationEvidence({
          sha256: file.container_sha256,
          path: file.path,
          operation: "inventory-asar-entry",
          coverage: completeApplicationCoverage(),
        }),
      },
    ],
  });
};

const addPackageRole = (
  context: JavaScriptArtifactGraphContext,
  input: PackageRoleInput,
): void => {
  if (input.declaredPath === null) return;
  const resolution = resolveArtifactPathByContext({
    declaredPath: input.declaredPath,
    sourcePath: input.packageFile.path,
    context: "package-entrypoint",
    files: context.filesByPath,
  });
  const role = createElectronRoleNode(context, {
    kind: input.kind,
    anchor: input.packageFile,
    resolution,
    mechanism:
      input.kind === "electron-main"
        ? "package.json:main"
        : "package.json:renderer",
  });
  context.accumulator.addEdge({
    source_node_id: input.packageNode.node_id,
    target_node_id: role.node_id,
    relation: "loads",
    properties: {
      declared_path: input.declaredPath,
      resolution_context: resolution.resolution_context,
      resolved_path: resolution.resolved_path,
      resolution_status: resolution.resolution_status,
      limitations: resolution.limitations,
    },
    evidence: staticInferenceEvidence({
      sha256: input.packageFile.sha256,
      path: input.packageFile.path,
      operation: "discover-package-entrypoint",
      coverage: completeApplicationCoverage(),
      limitations: resolution.limitations,
    }),
  });
  linkElectronRoleToAsset(context, {
    role,
    anchor: input.packageFile,
    resolution,
  });
};

const containerProperties = (container: JavaScriptArtifactContainer) => ({
  path: container.path,
  format: "asar",
  bytes: container.bytes,
  inventory_artifact_id: container.inventory_artifact_id,
});
import { javaScriptAnalysisCoverageLimits } from "../../domain/javascript/javascriptAnalysisResourceControls.js";
