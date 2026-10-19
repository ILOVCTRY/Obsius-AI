import { managedDecodeBudget } from "./ManagedDecodeBudget.js";
import { metadataRowCursor } from "./ManagedMetadataHeaps.js";
import type { ManagedMetadataLayout } from "./ManagedMetadataLayout.js";
import {
  memberListIndexSize,
  memberListRowLookup,
  memberListTable,
} from "./ManagedMemberPointers.js";
import { ManagedReaderFailure } from "./ManagedReaderFailure.js";

type MemberTable = 4 | 6;
type OwnerLookup = (definitionRow: number) => number;
const lookups = new WeakMap<
  ManagedMetadataLayout,
  Map<MemberTable, OwnerLookup | ManagedReaderFailure>
>();

/** One first-match TypeDef owner per physical member, shared by every inspection facet. */
export const memberDeclaringTypeRowLookup = (
  bytes: Buffer,
  layout: ManagedMetadataLayout,
  table: MemberTable,
): OwnerLookup => {
  let byTable = lookups.get(layout);
  const cached = byTable?.get(table);
  if (cached instanceof ManagedReaderFailure) throw cached;
  if (cached !== undefined) return cached;
  if (byTable === undefined) {
    byTable = new Map();
    lookups.set(layout, byTable);
  }
  try {
    const positionOf = memberListRowLookup(bytes, layout, table);
    const positions = memberListTable(layout, table)?.rowCount ?? 0;
    const types = layout.table(2)?.rowCount ?? 0;
    managedDecodeBudget(layout).reserve(
      (2 * positions + 3) * Uint32Array.BYTES_PER_ELEMENT + 256,
      layout.table(2)?.offset ?? null,
    );
    const owners = new Uint32Array(positions + 1);
    // Skip already-owned positions so overlapping malformed lists remain near-linear.
    const next = Uint32Array.from({ length: positions + 2 }, (_, p) => p);
    const unowned = (position: number): number => {
      let root = position;
      while (next[root] !== root) root = next[root] ?? root;
      for (let p = position; p !== root;) {
        const following = next[p] ?? root;
        next[p] = root;
        p = following;
      }
      return root;
    };
    const startForType = (row: number): number => {
      const cursor = metadataRowCursor(bytes, layout, 2, row);
      cursor.readUInt32();
      cursor.readIndex(layout.stringIndexSize);
      cursor.readIndex(layout.stringIndexSize);
      cursor.readIndex(layout.codedIndexSize("TypeDefOrRef"));
      const fieldStart = cursor.readIndex(memberListIndexSize(layout, 4));
      return table === 4
        ? fieldStart
        : cursor.readIndex(memberListIndexSize(layout, 6));
    };
    let start = types === 0 ? 0 : startForType(1);
    for (let row = 1; row <= types; row += 1) {
      const end = row === types ? positions + 1 : startForType(row + 1);
      const limit = Math.min(end, positions + 1);
      for (
        let p = unowned(Math.min(Math.max(start, 1), positions + 1));
        p < limit;
        p = unowned(p + 1)
      ) {
        owners[p] = row;
        next[p] = p + 1;
      }
      start = end;
    }
    const definitions = layout.table(table)?.rowCount ?? 0;
    const lookup: OwnerLookup = (definition) =>
      definition < 1 || definition > definitions
        ? 0
        : (owners[positionOf(definition)] ?? 0);
    byTable.set(table, lookup);
    return lookup;
  } catch (cause: unknown) {
    // Replaying a malformed layout must neither rebuild nor charge the index again.
    if (cause instanceof ManagedReaderFailure) byTable.set(table, cause);
    throw cause;
  }
};
