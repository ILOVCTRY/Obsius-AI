import { z } from "zod";

import { compareUnicodeCodePoints } from "./unicodeCodePointOrder.js";

const jsonValueTypeSchema = z.enum([
  "object",
  "array",
  "string",
  "number",
  "boolean",
  "null",
]);
type JsonValueType = z.infer<typeof jsonValueTypeSchema>;

const jsonShapePathSegmentSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("property"), name: z.string() }),
  z.object({ kind: z.literal("array-element") }),
]);
type JsonShapePathSegment = z.infer<typeof jsonShapePathSegmentSchema>;

/** Value-free structural summary of one approved JSON payload. */
export const jsonShapeSchema = z.object({
  root_type: jsonValueTypeSchema,
  node_count: z.number().int().min(1),
  max_depth_observed: z.number().int().min(0),
  properties: z.array(
    z.object({
      path: z.array(jsonShapePathSegmentSchema).min(1),
      types: z.array(jsonValueTypeSchema).min(1),
      observations: z.number().int().min(1),
    }),
  ),
});
export type JsonShape = z.infer<typeof jsonShapeSchema>;

const arrayElementKey = Symbol("array-element");

interface ShapeNode {
  readonly path: JsonShapePathSegment[];
  readonly types: Set<JsonValueType>;
  observations: number;
  children?: Map<string | typeof arrayElementKey, ShapeNode>;
}

/** Parse approved JSON and immediately discard values after iterative shape inference. */
export const inferJsonShape = (text: string): JsonShape | null => {
  let root: unknown;
  try {
    root = JSON.parse(text);
  } catch (cause: unknown) {
    // Invalid JSON is represented by the null return.
    void cause;
    return null;
  }
  const rootShape: ShapeNode = {
    path: [],
    types: new Set(),
    observations: 0,
  };
  // Each structural path owns one accumulator, shared by all matching array rows.
  const childShape = (
    parent: ShapeNode,
    key: string | typeof arrayElementKey,
  ): ShapeNode => {
    const children = (parent.children ??= new Map());
    const existing = children.get(key);
    if (existing !== undefined) return existing;
    const node: ShapeNode = {
      path: [
        ...parent.path,
        typeof key === "string"
          ? { kind: "property", name: key }
          : { kind: "array-element" },
      ],
      types: new Set(),
      observations: 0,
    };
    children.set(key, node);
    return node;
  };
  const pending: Array<{
    readonly value: unknown;
    readonly shape: ShapeNode;
  }> = [{ value: root, shape: rootShape }];
  let nodeCount = 0;
  let maxDepthObserved = 0;
  while (pending.length > 0) {
    const current = pending.pop();
    if (current === undefined) break;
    nodeCount += 1;
    maxDepthObserved = Math.max(maxDepthObserved, current.shape.path.length);
    current.shape.types.add(jsonValueType(current.value));
    current.shape.observations += 1;
    if (Array.isArray(current.value)) {
      if (current.value.length === 0) continue;
      const shape = childShape(current.shape, arrayElementKey);
      for (let index = current.value.length - 1; index >= 0; index -= 1)
        pending.push({ value: current.value[index], shape });
      continue;
    }
    if (!isRecord(current.value)) continue;
    const allEntries = Object.entries(current.value);
    for (let index = allEntries.length - 1; index >= 0; index -= 1) {
      const entry = allEntries[index];
      if (entry === undefined) continue;
      const [name, value] = entry;
      pending.push({ value, shape: childShape(current.shape, name) });
    }
  }
  // Preorder over sorted siblings gives canonical paths without repeatedly
  // comparing their shared prefixes. Each returned path owns its segments.
  const properties: JsonShape["properties"] = [];
  const shapes = orderedChildren(rootShape);
  while (shapes.length > 0) {
    const shape = shapes.pop();
    if (shape === undefined) break;
    properties.push({
      path: shape.path.map((segment) => ({ ...segment })),
      types: [...shape.types].sort(),
      observations: shape.observations,
    });
    for (const child of orderedChildren(shape)) shapes.push(child);
  }
  return {
    root_type: jsonValueType(root),
    node_count: nodeCount,
    max_depth_observed: maxDepthObserved,
    properties,
  };
};

const jsonValueType = (value: unknown): JsonValueType => {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  switch (typeof value) {
    case "string":
      return "string";
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    default:
      return "object";
  }
};

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Reverse sibling order for the traversal stack; array elements sort first. */
const orderedChildren = (node: ShapeNode): ShapeNode[] =>
  [...(node.children ?? [])]
    .sort(([left], [right]) =>
      left === right
        ? 0
        : left === arrayElementKey
          ? 1
          : right === arrayElementKey
            ? -1
            : compareUnicodeCodePoints(right, left),
    )
    .map(([, child]) => child);
