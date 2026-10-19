import type { BigIntStats, Dir, Dirent } from "node:fs";
import { opendir, readlink, realpath } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { ArtifactReaderFailure } from "../artifacts/ArtifactReader.js";
import type { ArtifactCleanupAttempt } from "../artifacts/ArtifactResourceScope.js";
import { OwnedDirectoryHandle } from "../filesystem/OwnedDirectoryHandle.js";
import { err, ok } from "../domain/result.js";

import {
  cancelled,
  entryFailure,
  failure,
  filesystemFailureDetail,
  safeSize,
} from "./ReferenceSourceReaderErrors.js";
import { isPathWithinRoot } from "../domain/localPath.js";
import { pathFromRoot } from "./ReferenceSourceReaderPaths.js";
import { readStableFile } from "./ReferenceSourceReaderFile.js";
import {
  isAborted,
  sameFile,
  validateDirectory,
  bigLstat,
} from "./ReferenceSourceReaderValidate.js";
import type {
  PendingDirectory,
  ReferenceSourceEntry,
  ReferenceSourceEntryKind,
  ReferenceSourceResult,
  TraversalState,
} from "./ReferenceSourceReaderTypes.js";

export const traverseDirectory = async (
  state: TraversalState,
  current: PendingDirectory,
): Promise<ReferenceSourceResult<undefined>> => {
  const before = await validateDirectory(
    state.root,
    state.rootIdentity,
    current.path,
    state.signal,
  );
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  if (!before.ok) {
    state.entries.push(
      entryFailure(
        pathFromRoot(state.root, current.path),
        "directory",
        before.code,
        before.message,
      ),
    );
    return { ok: true, value: undefined };
  }
  const directories: PendingDirectory[] = [];
  let readFailure: string | undefined;
  let handle: Dir | undefined;
  try {
    handle = await opendir(current.path);
  } catch (cause: unknown) {
    readFailure = directoryReadFailure(cause);
  }
  if (handle !== undefined) {
    const owner = new OwnedDirectoryHandle(handle);
    let result: ReferenceSourceResult<undefined> = ok(undefined);
    let cleanup: ArtifactCleanupAttempt;
    try {
      for (;;) {
        let child: Dirent | null;
        try {
          child = await handle.read();
        } catch (cause: unknown) {
          readFailure = directoryReadFailure(cause);
          break;
        }
        if (child === null) break;
        result = await processEntry(state, current, child.name, directories);
        if (!result.ok) break;
      }
    } finally {
      cleanup = await state.resources.release({
        kind: "directory-handle",
        handle: owner,
        resource: current.path,
      });
    }
    if (cleanup.kind === "failed") {
      if (result.ok && !isAborted(state.signal) && readFailure !== undefined)
        state.entries.push(
          entryFailure(
            pathFromRoot(state.root, current.path),
            "directory",
            "io",
            readFailure,
          ),
        );
      const observation = ArtifactReaderFailure.cleanupObservation(
        cleanup.cause,
        current.path,
      );
      const previous = result.ok ? undefined : result.error.cleanup;
      const primary = result.ok
        ? isAborted(state.signal)
          ? cancelled()
          : failure(
              "io",
              readFailure ??
                `Reference directory cleanup failed: ${current.path}`,
            )
        : result.error;
      return err({
        ...primary,
        cleanup:
          previous === undefined
            ? observation
            : {
                reason: `${previous.reason}; ${observation.reason}`,
                resources: [
                  ...new Set([...previous.resources, ...observation.resources]),
                ],
              },
        cause: primary.cause ?? cleanup.cause,
      });
    }
    if (!result.ok) return result;
  }
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  if (readFailure !== undefined) {
    state.entries.push(
      entryFailure(
        pathFromRoot(state.root, current.path),
        "directory",
        "io",
        readFailure,
      ),
    );
    return { ok: true, value: undefined };
  }
  const after = await validateDirectory(
    state.root,
    state.rootIdentity,
    current.path,
    state.signal,
  );
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  if (!after.ok || !sameFile(before.stats, after.stats)) {
    state.entries.push(
      entryFailure(
        pathFromRoot(state.root, current.path),
        "directory",
        "changed",
        "Directory changed while it was read",
      ),
    );
    return { ok: true, value: undefined };
  }
  if (current.path !== state.root)
    state.entries.push({
      status: "read",
      kind: "directory",
      path: pathFromRoot(state.root, current.path),
    });
  directories.reverse();
  state.pending.push(...directories);
  return { ok: true, value: undefined };
};

const directoryReadFailure = (cause: unknown): string => {
  const message = filesystemFailureDetail(cause, "Directory could not be read");
  if (message === undefined) throw cause;
  return message;
};

const readMetadata = async (
  path: string,
): Promise<
  | { readonly ok: true; readonly value: BigIntStats }
  | { readonly ok: false; readonly message: string }
> => {
  try {
    return { ok: true, value: await bigLstat(path) };
  } catch (cause: unknown) {
    const message = filesystemFailureDetail(
      cause,
      "Entry metadata could not be read",
    );
    if (message === undefined) throw cause;
    return { ok: false, message };
  }
};

const processEntry = async (
  state: TraversalState,
  current: PendingDirectory,
  name: string,
  directories: PendingDirectory[],
): Promise<ReferenceSourceResult<undefined>> => {
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  const absolute = join(current.path, name);
  const path = pathFromRoot(state.root, absolute);
  const metadata = await readMetadata(absolute);
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  if (!metadata.ok) {
    state.entries.push(entryFailure(path, "unknown", "io", metadata.message));
    return { ok: true, value: undefined };
  }
  const kind = metadata.value.isSymbolicLink()
    ? "symlink"
    : metadata.value.isDirectory()
      ? "directory"
      : metadata.value.isFile()
        ? "file"
        : "other";
  const excluded = applyExclusion(state.shouldExclude, path, kind);
  if (!excluded.ok)
    return {
      ok: false,
      error: {
        tag: "reference-source-reader",
        code: "io",
        message: "Reference source exclusion check failed",
      },
    };
  if (excluded.value) return { ok: true, value: undefined };
  if (isAborted(state.signal)) return { ok: false, error: cancelled() };
  if (kind === "symlink")
    state.entries.push(
      await readStableSymlinkTarget(
        absolute,
        path,
        metadata.value,
        state.signal,
        () => readSymlinkTarget(state.root, absolute, path),
      ),
    );
  else if (kind === "directory") {
    directories.push({ path: absolute });
  } else if (kind !== "file")
    state.entries.push(
      entryFailure(
        path,
        "other",
        "unsupported",
        "Entry is not a regular file",
        safeSize(metadata.value.size),
      ),
    );
  else return processFileEntry(state, absolute, path, metadata.value);
  return { ok: true, value: undefined };
};

const processFileEntry = async (
  state: TraversalState,
  absolute: string,
  path: string,
  metadata: BigIntStats,
): Promise<ReferenceSourceResult<undefined>> => {
  const result = await readStableFile({
    resources: state.resources,
    root: state.root,
    rootIdentity: state.rootIdentity,
    absolute,
    path,
    expected: metadata,
    ...(state.signal === undefined ? {} : { signal: state.signal }),
  });
  const { entry } = result;
  if (entry.status === "read" && entry.kind === "file")
    state.bytesRead += entry.bytes.byteLength;
  state.entries.push(entry);
  if (result.cleanup !== undefined)
    return err({
      tag: "reference-source-reader",
      code:
        entry.status === "failed" && entry.code === "cancelled"
          ? "cancelled"
          : "io",
      message:
        entry.status === "failed"
          ? entry.message
          : `Reference file cleanup failed: ${path}`,
      cleanup: result.cleanup,
    });
  return ok(undefined);
};

/** Bracket target observation with the symlink identity captured at enumeration. */
export const readStableSymlinkTarget = async (
  absolute: string,
  path: string,
  expected: BigIntStats,
  signal: AbortSignal | undefined,
  readTarget: () => Promise<ReferenceSourceEntry>,
): Promise<ReferenceSourceEntry> => {
  try {
    const before = await bigLstat(absolute);
    if (isAborted(signal)) return cancelledSymlink(path);
    if (!before.isSymbolicLink() || !sameFile(expected, before))
      return changedSymlink(path);
    const observed = await readTarget();
    if (isAborted(signal)) return cancelledSymlink(path);
    const after = await bigLstat(absolute);
    if (isAborted(signal)) return cancelledSymlink(path);
    if (!after.isSymbolicLink() || !sameFile(expected, after))
      return changedSymlink(path);
    return observed;
  } catch (cause: unknown) {
    if (isAborted(signal)) return cancelledSymlink(path);
    const code =
      cause instanceof Error ? Reflect.get(cause, "code") : undefined;
    if (code === "ENOENT" || code === "ENOTDIR") return changedSymlink(path);
    const message = filesystemFailureDetail(
      cause,
      "Symbolic link target could not be read safely",
    );
    if (message === undefined) throw cause;
    return entryFailure(path, "symlink", "io", message);
  }
};

const cancelledSymlink = (path: string) =>
  entryFailure(path, "symlink", "cancelled", "Symlink inspection cancelled");

const changedSymlink = (path: string) =>
  entryFailure(
    path,
    "symlink",
    "changed",
    "Symbolic link changed while its target was read",
  );

const readSymlinkTarget = async (
  root: string,
  absolute: string,
  path: string,
): Promise<ReferenceSourceEntry> => {
  const rawTarget = await readlink(absolute);
  const lexicalTarget = resolve(dirname(absolute), rawTarget);
  if (!isPathWithinRoot(root, lexicalTarget))
    return {
      status: "read",
      kind: "symlink",
      path,
      target: lexicalTarget,
      targetState: "external",
    };
  let canonicalTarget: string;
  try {
    canonicalTarget = await realpath(lexicalTarget);
  } catch (cause: unknown) {
    const code =
      cause instanceof Error ? Reflect.get(cause, "code") : undefined;
    if (code !== "ENOENT") throw cause;
    return {
      status: "read",
      kind: "symlink",
      path,
      target: pathFromRoot(root, lexicalTarget),
      targetState: "missing",
    };
  }
  const internal = isPathWithinRoot(root, canonicalTarget);
  return {
    status: "read",
    kind: "symlink",
    path,
    target: internal ? pathFromRoot(root, canonicalTarget) : canonicalTarget,
    targetState: internal ? "internal" : "external",
  };
};

const applyExclusion = (
  shouldExclude:
    | ((path: string, kind: ReferenceSourceEntryKind) => boolean)
    | undefined,
  path: string,
  kind: ReferenceSourceEntryKind,
): { readonly ok: true; readonly value: boolean } | { readonly ok: false } => {
  try {
    return { ok: true, value: shouldExclude?.(path, kind) === true };
  } catch (cause: unknown) {
    // Exclusion predicates are caller-supplied; a throwing predicate fails
    // closed and the caller-visible `{ ok: false }` preserves the rejection
    // without propagating an arbitrary predicate cause.
    void cause;
    return { ok: false };
  }
};
