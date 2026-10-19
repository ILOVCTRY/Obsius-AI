import { fc, it } from "@fast-check/vitest";
import { describe, expect, vi } from "vitest";

import { inspectManagedArtifactBytes } from "./ManagedArtifactInspector.js";
import { inspectManagedMembersBytes } from "./ManagedMemberInspector.js";
import { managedDecodeBudget } from "./ManagedDecodeBudget.js";
import { memberDeclaringTypeRowLookup } from "./ManagedMemberOwnership.js";
import { memberListIndexSize } from "./ManagedMemberPointers.js";
import { readManagedMetadataLayout } from "./ManagedMetadataLayout.js";
import {
  buildManagedMemberPointerFixture,
  buildManagedPeFixture,
  managedPeFixtureTarget,
} from "./ManagedPe.fixture.js";
import { readManagedPeLayout } from "./ManagedPeReader.js";
import { ManagedReaderFailure } from "./ManagedReaderFailure.js";

const metadataLayout = (bytes: Buffer) => {
  const pe = readManagedPeLayout(bytes);
  if (pe.cli === null) throw new Error("Fixture omitted CLI metadata");
  return readManagedMetadataLayout(
    bytes,
    pe.rvaToOffset(pe.cli.metadata.rva, pe.cli.metadata.size, "cli.metadata"),
    pe.cli.metadata.size,
  );
};

describe("managed declaring type ownership", () => {
  it.prop([
    fc.tuple(
      fc.tuple(
        ...Array.from({ length: 3 }, () => fc.integer({ min: 0, max: 6 })),
      ),
      fc.tuple(
        ...Array.from({ length: 3 }, () => fc.integer({ min: 0, max: 6 })),
      ),
    ),
  ])(
    "retains first-match field and method owners for arbitrary metadata lists",
    ([fields, methods]) => {
      const bytes = buildManagedMemberPointerFixture();
      const layout = metadataLayout(bytes);
      const types = layout.table(2);
      if (types === undefined) throw new Error("Fixture omitted TypeDef");
      for (let row = 0; row < 3; row++) {
        const fieldOffset =
          types.offset +
          row * types.rowSize +
          4 +
          2 * layout.stringIndexSize +
          layout.codedIndexSize("TypeDefOrRef");
        bytes.writeUInt16LE(fields[row] ?? 0, fieldOffset);
        bytes.writeUInt16LE(
          methods[row] ?? 0,
          fieldOffset + memberListIndexSize(layout, 4),
        );
      }
      const target = managedPeFixtureTarget(bytes);
      const members = inspectManagedMembersBytes(bytes, target);
      const artifact = inspectManagedArtifactBytes(bytes, target);
      for (const [table, starts] of [
        ["fields", fields],
        ["methods", methods],
      ] as const) {
        // The fixture's pointers reorder physical rows 1,2 to list positions 2,1.
        const expected = [2, 1].map((position) => {
          const owner = starts.findIndex(
            (start, index) =>
              position >= start && position < (starts[index + 1] ?? 3),
          );
          return owner < 0 ? null : members.types[owner]?.full_name;
        });
        expect(members[table].map((member) => member.declaring_type)).toEqual(
          expected,
        );
      }
      expect(artifact.attributes[0]?.type_name).toBe(
        members.methods[0]?.declaring_type,
      );
    },
  );

  it("indexes large unordered member lists once per layout", () => {
    const count = 100_000;
    const bytes = buildManagedPeFixture({
      additionalEmptyTypes: count - 1,
      methodRowCount: count,
    });
    const layout = metadataLayout(bytes);
    const types = layout.table(2);
    if (types === undefined) throw new Error("Fixture omitted TypeDef");
    const methodOffset =
      types.offset +
      4 +
      2 * layout.stringIndexSize +
      layout.codedIndexSize("TypeDefOrRef") +
      memberListIndexSize(layout, 4);
    // The first reversed range forces first-match semantics, with the owner last.
    // 100,000 MethodDef rows require a four-byte method-list index.
    bytes.writeUInt32LE(2, methodOffset);
    const start = performance.now();
    const lookup = memberDeclaringTypeRowLookup(bytes, layout, 6);
    expect(
      Array.from({ length: count }, (_, row) => lookup(row + 1)).every(
        (owner) => owner === count,
      ),
    ).toBe(true);
    expect(performance.now() - start).toBeLessThan(2000);
    expect(memberDeclaringTypeRowLookup(bytes, layout, 6)).toBe(lookup);
    expect([lookup(0), lookup(count + 1)]).toEqual([0, 0]);
  });

  it("admits ownership storage before allocation and retains failed admission", () => {
    const bytes = buildManagedPeFixture();
    const layout = metadataLayout(bytes);
    const budget = managedDecodeBudget(layout);
    const capacityFailure = new ManagedReaderFailure({
      code: "resource-limit",
      scope: "metadata.representation",
      offset: layout.table(2)?.offset ?? null,
      detail: "No remaining ownership index capacity",
    });
    const admission = vi.spyOn(budget, "reserve").mockImplementation(() => {
      throw capacityFailure;
    });
    let allocations = 0;
    vi.stubGlobal(
      "Uint32Array",
      new Proxy(Uint32Array, {
        construct(target, args, newTarget) {
          allocations++;
          return Reflect.construct(target, args, newTarget);
        },
      }),
    );
    try {
      expect(() => memberDeclaringTypeRowLookup(bytes, layout, 6)).toThrow(
        capacityFailure,
      );
      expect(() => memberDeclaringTypeRowLookup(bytes, layout, 6)).toThrow(
        capacityFailure,
      );
      expect(admission).toHaveBeenCalledTimes(1);
      expect(allocations).toBe(0);
    } finally {
      vi.unstubAllGlobals();
      admission.mockRestore();
    }
  });
});
