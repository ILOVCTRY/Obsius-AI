import { digestCanonicalValue } from "./canonicalDigest.js";
import { compareUnicodeCodePoints } from "./unicodeCodePointOrder.js";
import type { WebPageInspection } from "./browserObservationSchemas.js";
import {
  webCaptureDiffSchema,
  type CompareWebCapturesInput,
  type WebCaptureChange,
  type WebCaptureDiff,
  type WebCaptureDimension,
} from "./webCaptureDiffSchemas.js";
import type { WebMcpDiscovery } from "./webMcpDiscovery.js";

type Dimension = WebCaptureDimension;
type Change = WebCaptureChange;

/** Compare normalized observations without treating incomplete absence as proof. */
export const compareWebCaptures = (
  input: CompareWebCapturesInput,
): WebCaptureDiff => {
  const before = input.before.inspection;
  const after = input.after.inspection;
  const dimensions = compareWebCaptureDimensions(input);
  const statuses = Object.values(dimensions).map(({ status }) => status);
  return webCaptureDiffSchema.parse({
    overall_status: statuses.includes("changed")
      ? "changed"
      : statuses.includes("unknown")
        ? "unknown"
        : "unchanged",
    before_target: {
      target_id: before.target.target_id,
      url: before.target.url,
    },
    after_target: {
      target_id: after.target.target_id,
      url: after.target.url,
    },
    dimensions,
    limitations: [
      "A changed status proves an observed difference; an unknown status means absence could not be established from capture completeness.",
      "Network comparison covers only activity observed after each CDP attachment. WebSocket connections with the same URL are compared as an unordered collection of ordered frame streams, retaining connection counts; their individual identities are not stable across captures.",
      "Accessibility roles, ignored state, text, and hierarchy are compared only when the accessibility tree was fully captured and text capture was selected and not truncated.",
      "Storage key inventories are compared only when selected and complete; usage and quota are compared only when reported. Redacted content is compared through complete SHA-256 fingerprints.",
    ],
  });
};

const compareIdentities = (
  before: ReadonlyMap<string, string>,
  after: ReadonlyMap<string, string>,
): Change[] => {
  const changes: Change[] = [];
  for (const [identity, fingerprint] of before) {
    const next = after.get(identity);
    if (next === undefined) changes.push({ identity, change: "removed" });
    else if (next !== fingerprint)
      changes.push({ identity, change: "modified" });
  }
  for (const identity of after.keys())
    if (!before.has(identity)) changes.push({ identity, change: "added" });
  return changes.sort(
    (left, right) =>
      compareUnicodeCodePoints(left.identity, right.identity) ||
      compareUnicodeCodePoints(left.change, right.change),
  );
};

const compareDimension = (
  before: ReadonlyMap<string, string>,
  after: ReadonlyMap<string, string>,
  complete: boolean,
  reason: string,
): Dimension => {
  const all = compareIdentities(before, after).filter(
    ({ change }) => complete || change === "modified",
  );
  if (all.length > 0)
    return {
      status: "changed",
      total_changes: all.length,
      changes: all,
      reason: null,
    };
  return complete
    ? {
        status: "unchanged",
        total_changes: 0,
        changes: [],
        reason: null,
      }
    : {
        status: "unknown",
        total_changes: 0,
        changes: [],
        reason,
      };
};

const accessibilityDimension = (
  before: WebPageInspection,
  after: WebPageInspection,
): Dimension => {
  const beforeAccess = accessibilityComparable(before);
  const afterAccess = accessibilityComparable(after);
  const textComparable = beforeAccess.text && afterAccess.text;
  const nodesComparable = beforeAccess.nodes && afterAccess.nodes;
  return compareDimension(
    singleton(
      "accessibility_tree",
      digestCanonicalValue(
        accessibilityProjection(
          before.accessibility,
          textComparable,
          nodesComparable,
        ),
      ),
    ),
    singleton(
      "accessibility_tree",
      digestCanonicalValue(
        accessibilityProjection(
          after.accessibility,
          textComparable,
          nodesComparable,
        ),
      ),
    ),
    beforeAccess.complete && afterAccess.complete,
    "Accessibility tree or text capture was incomplete in at least one observation.",
  );
};

const storageDimension = (
  before: WebPageInspection,
  after: WebPageInspection,
): Dimension => {
  const keysComplete =
    storageKeysComparable(before) && storageKeysComparable(after);
  const usageComplete =
    before.storage.usage_bytes !== null &&
    after.storage.usage_bytes !== null &&
    before.storage.quota_bytes !== null &&
    after.storage.quota_bytes !== null;
  const comparableFingerprints = storageFingerprintIdentities(
    before.storage,
    after.storage,
  );
  return compareDimension(
    storageMap(
      before.storage,
      keysComplete,
      usageComplete,
      comparableFingerprints,
    ),
    storageMap(
      after.storage,
      keysComplete,
      usageComplete,
      comparableFingerprints,
    ),
    storageComparable(before, after, keysComplete, usageComplete),
    "Storage fingerprints, inventories, usage, or quota were unavailable or truncated in at least one observation.",
  );
};

const compareWebCaptureDimensions = (
  input: CompareWebCapturesInput,
): WebCaptureDiff["dimensions"] => {
  const before = input.before.inspection;
  const after = input.after.inspection;
  return {
    dom_structure: compareDimension(
      singleton("document", digestCanonicalValue(domProjection(before))),
      singleton("document", digestCanonicalValue(domProjection(after))),
      sectionsComplete(before, ["frames", "dom"]) &&
        sectionsComplete(after, ["frames", "dom"]),
      "DOM or frame capture was incomplete in at least one observation.",
    ),
    scripts: compareDimension(
      keyed(
        before.scripts.items.map(scriptProjection),
        (item) => item.script_key,
      ),
      keyed(
        after.scripts.items.map(scriptProjection),
        (item) => item.script_key,
      ),
      sectionsComplete(before, ["scripts"]) &&
        sectionsComplete(after, ["scripts"]),
      "Script inventory was incomplete in at least one observation.",
    ),
    resources: compareDimension(
      keyed(before.resources, (item) => item.resource_key),
      keyed(after.resources, (item) => item.resource_key),
      sectionsComplete(before, ["resources"]) &&
        sectionsComplete(after, ["resources"]),
      "Resource inventory was incomplete in at least one observation.",
    ),
    network: networkDimension(before, after),
    metadata: compareDimension(
      singleton("metadata", digestCanonicalValue(metadataProjection(before))),
      singleton("metadata", digestCanonicalValue(metadataProjection(after))),
      sectionsComplete(before, ["metadata"]) &&
        sectionsComplete(after, ["metadata"]),
      "Safe metadata capture was incomplete in at least one observation.",
    ),
    webmcp: compareDimension(
      webMcpMap(input.before.webmcp),
      webMcpMap(input.after.webmcp),
      webMcpComplete(input.before.webmcp) && webMcpComplete(input.after.webmcp),
      "WebMCP discovery was unavailable or incomplete in at least one capture.",
    ),
    accessibility: accessibilityDimension(before, after),
    storage: storageDimension(before, after),
  };
};

const keyed = <T>(
  values: readonly T[],
  identity: (value: T) => string,
): ReadonlyMap<string, string> =>
  new Map(
    values.map((value) => [identity(value), digestCanonicalValue(value)]),
  );

type BodyShapeSources = {
  readonly request: boolean;
  readonly response: boolean;
};

const bodyShapesSelected = (inspection: WebPageInspection): boolean =>
  !inspection.completeness.excluded.some(
    ({ section, reason }) =>
      section === "json_body_shapes" && reason === "not_approved",
  ) &&
  (inspection.network.requests.length === 0 ||
    inspection.network.requests.some(
      ({ body_shapes }) => body_shapes.status !== "not_approved",
    ));

const webSocketShapesSelected = (inspection: WebPageInspection): boolean =>
  !inspection.completeness.excluded.some(
    ({ section, reason }) =>
      section === "websocket_shapes" && reason === "not_approved",
  );

const networkIdentity = (
  request: WebPageInspection["network"]["requests"][number],
): string =>
  `net_${digestCanonicalValue({ method: request.method, url: request.url, resource_type: request.resource_type })}`;

const networkResponseFields = [
  "status",
  "mime_type",
  "encoded_data_length",
] as const;
type NetworkResponseField = (typeof networkResponseFields)[number];

const sharedNetworkResponseFields = (
  before: WebPageInspection,
  after: WebPageInspection,
): ReadonlyMap<string, ReadonlySet<NetworkResponseField>> => {
  const shared = new Map<string, Set<NetworkResponseField>>();
  for (const inspection of [before, after])
    for (const request of inspection.network.requests) {
      const identity = networkIdentity(request);
      const fields = shared.get(identity) ?? new Set(networkResponseFields);
      for (const field of networkResponseFields)
        if (request[field] === null) fields.delete(field);
      shared.set(identity, fields);
    }
  return shared;
};

const networkDimension = (
  before: WebPageInspection,
  after: WebPageInspection,
): Dimension => {
  const responseFields = sharedNetworkResponseFields(before, after);
  const responseFieldsComplete = [...responseFields.values()].every(
    (fields) => fields.size === networkResponseFields.length,
  );
  const selected = bodyShapesSelected(before) && bodyShapesSelected(after);
  const webSocketShapes =
    webSocketShapesSelected(before) && webSocketShapesSelected(after);
  const sources = new Map<string, BodyShapeSources>();
  if (selected) {
    const observedSources = (inspection: WebPageInspection) => {
      const result = new Map<string, BodyShapeSources>();
      for (const request of inspection.network.requests) {
        const identity = networkIdentity(request);
        const previous = result.get(identity);
        result.set(identity, {
          request:
            previous?.request !== false && request.body_shapes.request !== null,
          response:
            previous?.response !== false &&
            request.body_shapes.response !== null,
        });
      }
      return result;
    };
    const left = observedSources(before);
    const right = observedSources(after);
    for (const [identity, coverage] of left) {
      const next = right.get(identity);
      sources.set(identity, {
        request: coverage.request && next?.request === true,
        response: coverage.response && next?.response === true,
      });
    }
  }
  const shapesComplete =
    !selected ||
    [before, after].every(
      (inspection) =>
        sectionsComplete(inspection, ["json_body_shapes"]) &&
        inspection.network.requests.every(
          ({ body_shapes }) => body_shapes.status === "included",
        ),
    );
  const webSocketShapesComplete =
    !webSocketShapes ||
    [before, after].every(
      (inspection) =>
        sectionsComplete(inspection, ["websocket_shapes"]) &&
        inspection.network.websocket_connections.every((connection) =>
          connection.events.every((event) => event.payload_shape !== null),
        ),
    );
  const webSocketPayloadBytesComplete = [before, after].every((inspection) =>
    inspection.network.websocket_connections.every((connection) =>
      connection.events.every((event) => event.payload_bytes !== null),
    ),
  );
  const beforeWebSockets = webSocketGroups(before);
  const afterWebSockets = webSocketGroups(after);
  const [beforeWebSocketPayload, afterWebSocketPayload] = webSocketPayloadMaps(
    beforeWebSockets,
    afterWebSockets,
    webSocketShapes,
  );
  const compared = compareDimension(
    new Map([
      ...networkMap(before, sources, responseFields),
      ...webSocketMetadataMap(beforeWebSockets),
      ...beforeWebSocketPayload,
    ]),
    new Map([
      ...networkMap(after, sources, responseFields),
      ...webSocketMetadataMap(afterWebSockets),
      ...afterWebSocketPayload,
    ]),
    [before, after].every((inspection) =>
      sectionsComplete(inspection, [
        "network_requests",
        "websocket_connections",
        "websocket_frames",
      ]),
    ) &&
      shapesComplete &&
      webSocketPayloadBytesComplete &&
      webSocketShapesComplete,
    "Network capture or selected HTTP/WebSocket payload-shape coverage is attach-window limited, unavailable, or incomplete.",
  );
  return compared.status === "unchanged" && !responseFieldsComplete
    ? {
        ...compared,
        status: "unknown",
        reason:
          "HTTP response status, MIME type, or byte-count evidence is incomplete.",
      }
    : compared;
};

const networkMap = (
  inspection: WebPageInspection,
  sources: ReadonlyMap<string, BodyShapeSources>,
  responseFields: ReadonlyMap<string, ReadonlySet<NetworkResponseField>>,
): ReadonlyMap<string, string> => {
  const grouped = new Map<string, unknown[]>();
  for (const request of inspection.network.requests) {
    const identity = networkIdentity(request);
    const shapes = sources.get(identity);
    const values = grouped.get(identity) ?? [];
    const response: Record<string, unknown> = {};
    for (const field of responseFields.get(identity) ?? [])
      response[field] = request[field];
    values.push({
      ...response,
      redirects: (request.redirects ?? []).map(
        ({
          request_timestamp: _requestTimestamp,
          redirect_event_timestamp: _redirectEventTimestamp,
          ...redirect
        }) => redirect,
      ),
      initiator: request.initiator,
      ...(shapes?.request === true
        ? { request_shape: request.body_shapes.request }
        : {}),
      ...(shapes?.response === true
        ? { response_shape: request.body_shapes.response }
        : {}),
    });
    grouped.set(identity, values);
  }
  return new Map(
    [...grouped].map(([identity, values]) => [
      identity,
      digestCanonicalValue(
        values.map((value) => digestCanonicalValue(value)).sort(),
      ),
    ]),
  );
};

type WebSocketGroup = {
  readonly url: string;
  readonly stream: string;
  readonly connections: WebPageInspection["network"]["websocket_connections"];
};

const webSocketGroups = (
  inspection: WebPageInspection,
): ReadonlyMap<string, WebSocketGroup> => {
  const groups = new Map<string, WebSocketGroup>();
  for (const connection of inspection.network.websocket_connections) {
    const stream = digestCanonicalValue(
      connection.events.map(({ direction, opcode }) => ({
        direction,
        opcode,
      })),
    );
    const key = digestCanonicalValue({ url: connection.url, stream });
    const previous = groups.get(key);
    if (previous !== undefined) previous.connections.push(connection);
    else
      groups.set(key, {
        url: connection.url,
        stream,
        connections: [connection],
      });
  }
  return groups;
};

const webSocketMetadataMap = (
  groups: ReadonlyMap<string, WebSocketGroup>,
): ReadonlyMap<string, string> => {
  const byUrl = new Map<string, string[]>();
  for (const group of groups.values()) {
    const streams = byUrl.get(group.url) ?? [];
    for (let index = 0; index < group.connections.length; index += 1)
      streams.push(group.stream);
    byUrl.set(group.url, streams);
  }
  return new Map(
    [...byUrl].map(([url, streams]) => [
      `ws_${digestCanonicalValue(url)}`,
      digestCanonicalValue(streams.sort(compareUnicodeCodePoints)),
    ]),
  );
};

const webSocketPayloadMaps = (
  beforeGroups: ReadonlyMap<string, WebSocketGroup>,
  afterGroups: ReadonlyMap<string, WebSocketGroup>,
  selected: boolean,
): readonly [ReadonlyMap<string, string>, ReadonlyMap<string, string>] => {
  const left = new Map<string, string>();
  const right = new Map<string, string>();
  for (const [groupKey, beforeGroup] of beforeGroups) {
    const afterGroup = afterGroups.get(groupKey);
    if (
      afterGroup === undefined ||
      beforeGroup.connections.length !== afterGroup.connections.length
    )
      continue;
    const eventCount = beforeGroup.connections[0]?.events.length ?? 0;
    const comparableBytes = new Set<number>();
    const comparableShapes = new Set<number>();
    for (let index = 0; index < eventCount; index += 1) {
      if (
        beforeGroup.connections.every(
          (connection) => connection.events[index]?.payload_bytes !== null,
        ) &&
        afterGroup.connections.every(
          (connection) => connection.events[index]?.payload_bytes !== null,
        )
      )
        comparableBytes.add(index);
      // Compare only when every same-metadata connection has shape evidence
      // on both sides; this avoids assigning an unknown shape to a known one.
      if (
        selected &&
        beforeGroup.connections.every(
          (connection) => connection.events[index]?.payload_shape !== null,
        ) &&
        afterGroup.connections.every(
          (connection) => connection.events[index]?.payload_shape !== null,
        )
      )
        comparableShapes.add(index);
    }
    if (comparableBytes.size === 0 && comparableShapes.size === 0) continue;
    const project = (group: WebSocketGroup) =>
      digestCanonicalValue(
        group.connections
          .map((connection) =>
            connection.events.map((event, index) => {
              const comparablePayload: Record<string, unknown> = {};
              if (comparableBytes.has(index))
                comparablePayload.payload_bytes = event.payload_bytes;
              if (comparableShapes.has(index))
                comparablePayload.payload_shape = event.payload_shape;
              return Object.keys(comparablePayload).length === 0
                ? null
                : comparablePayload;
            }),
          )
          .map((stream) => digestCanonicalValue(stream))
          .sort(compareUnicodeCodePoints),
      );
    const identity = `wss_${groupKey}`;
    left.set(identity, project(beforeGroup));
    right.set(identity, project(afterGroup));
  }
  return [left, right];
};

const webMcpMap = (
  discovery: WebMcpDiscovery | null,
): ReadonlyMap<string, string> =>
  discovery === null
    ? new Map()
    : keyed(discovery.tools.items, (tool) => tool.tool_key);

const webMcpComplete = (discovery: WebMcpDiscovery | null): boolean =>
  discovery !== null &&
  discovery.status === "available" &&
  !incompleteSections(discovery.completeness).has("webmcp_tools");

const sectionsComplete = (
  inspection: WebPageInspection,
  sections: readonly string[],
): boolean => {
  const incomplete = incompleteSections(inspection.completeness);
  return sections.every((section) => !incomplete.has(section));
};

const incompleteSections = (completeness: {
  readonly policy_filtered_sections: readonly string[];
  readonly attach_limited_sections: readonly string[];
  readonly truncated_sections: readonly string[];
  readonly unavailable_sections: readonly string[];
}): ReadonlySet<string> =>
  new Set([
    ...completeness.policy_filtered_sections,
    ...completeness.attach_limited_sections,
    ...completeness.truncated_sections,
    ...completeness.unavailable_sections,
  ]);

const domProjection = (inspection: WebPageInspection) => ({
  frames: inspection.frames
    .map(({ url, origin }) => ({ url, origin }))
    .sort(
      (left, right) =>
        compareUnicodeCodePoints(left.url, right.url) ||
        compareUnicodeCodePoints(left.origin ?? "", right.origin ?? "") ||
        Number(left.origin !== null) - Number(right.origin !== null),
    ),
  nodes: inspection.dom.nodes.map(({ index: _index, ...node }) => node),
});

const scriptProjection = (
  script: WebPageInspection["scripts"]["items"][number],
) => ({
  script_key: script.script_key,
  url: script.url,
  cdp_hash: script.cdp_hash,
  length: script.length,
  is_module: script.is_module,
  language: script.language,
  source_map_url: script.source_map_url,
});

const metadataProjection = (inspection: WebPageInspection) => ({
  responses: inspection.metadata.responses
    .map(({ request_id: _requestId, ...response }) => response)
    .map((value) => digestCanonicalValue(value))
    .sort(),
  dom_urls: inspection.metadata.dom_urls
    .map((value) => digestCanonicalValue(value))
    .sort(),
  agent_hints: inspection.metadata.agent_hints
    .map((value) => digestCanonicalValue(value))
    .sort(),
  excluded_dom_urls: inspection.metadata.excluded_dom_urls,
  headers_allowlisted: inspection.metadata.headers_allowlisted,
});

const singleton = (key: string, value: string): ReadonlyMap<string, string> =>
  new Map([[key, value]]);

const accessibilityComparable = (
  inspection: WebPageInspection,
): {
  readonly text: boolean;
  readonly nodes: boolean;
  readonly complete: boolean;
} => {
  const text =
    inspection.accessibility.text_capture.status === "included" &&
    inspection.accessibility.text_capture.excluded_fields === 0;
  const nodes =
    inspection.accessibility.total_nodes ===
    inspection.accessibility.nodes.length;
  const complete =
    text && nodes && sectionsComplete(inspection, ["accessibility"]);
  return { text, nodes, complete };
};

const accessibilityProjection = (
  accessibility: WebPageInspection["accessibility"],
  includeText: boolean,
  includeNodes: boolean,
): unknown => {
  if (!includeNodes) return { total_nodes: accessibility.total_nodes };
  const nodeIndexes = new Map(
    accessibility.nodes.map((node, index) => [node.node_id, index]),
  );
  return {
    total_nodes: accessibility.total_nodes,
    nodes: accessibility.nodes.map((node) => ({
      parent_index:
        node.parent_id === null
          ? null
          : (nodeIndexes.get(node.parent_id) ?? -1),
      role: node.role,
      ignored: node.ignored,
      states: node.states,
      ...(includeText
        ? { name: node.name, description: node.description }
        : {}),
    })),
  };
};

const storageKeysComparable = (inspection: WebPageInspection): boolean =>
  sectionsComplete(inspection, ["storage_keys"]);

const storageComparable = (
  before: WebPageInspection,
  after: WebPageInspection,
  keysComplete: boolean,
  usageComplete: boolean,
): boolean =>
  keysComplete &&
  usageComplete &&
  before.storage.fingerprints_complete &&
  after.storage.fingerprints_complete;

const storageMap = (
  storage: WebPageInspection["storage"],
  includeKeys: boolean,
  includeUsage: boolean,
  fingerprintIdentities: ReadonlySet<string>,
): ReadonlyMap<string, string> => {
  const map = new Map<string, string>([
    [
      "storage:summary",
      digestCanonicalValue({
        origin: storage.origin,
        values_redacted: storage.values_redacted,
        ...(includeUsage
          ? {
              usage_bytes: storage.usage_bytes,
              quota_bytes: storage.quota_bytes,
            }
          : {}),
      }),
    ],
  ]);
  if (!includeKeys) return map;
  const add = (kind: string, keys: readonly string[]) => {
    for (const key of keys) {
      map.set(`storage:${kind}:${key}`, digestCanonicalValue(key));
    }
  };
  add("local_storage", storage.local_storage_keys);
  add("session_storage", storage.session_storage_keys);
  add("indexed_db", storage.indexed_db_names);
  add("cache", storage.cache_names);
  for (const fingerprint of storage.content_fingerprints) {
    const identity = `${fingerprint.scope}:${fingerprint.identity_sha256}`;
    if (!fingerprintIdentities.has(identity)) continue;
    map.set(
      `storage:content:${identity}`,
      digestCanonicalValue(fingerprint.value_sha256),
    );
  }
  return map;
};

const storageFingerprintIdentities = (
  before: WebPageInspection["storage"],
  after: WebPageInspection["storage"],
): ReadonlySet<string> => {
  const beforeComplete = completeStorageFingerprints(before);
  const afterComplete = completeStorageFingerprints(after);
  if (before.fingerprints_complete && after.fingerprints_complete)
    return new Set([...beforeComplete.keys(), ...afterComplete.keys()]);
  return new Set(
    [...beforeComplete.keys()].filter((identity) =>
      afterComplete.has(identity),
    ),
  );
};

const completeStorageFingerprints = (
  storage: WebPageInspection["storage"],
): ReadonlyMap<string, string | null> =>
  new Map(
    storage.content_fingerprints
      .filter(({ complete }) => complete)
      .map(({ scope, identity_sha256, value_sha256 }) => [
        `${scope}:${identity_sha256}`,
        value_sha256,
      ]),
  );
