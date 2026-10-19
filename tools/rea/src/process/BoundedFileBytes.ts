import type { FileHandle } from "node:fs/promises";

/** Read through EOF, returning undefined on overflow; leave the handle open. */
export const readBoundedFileBytes = async (
  handle: FileHandle,
  maxBytes: number,
  signal?: AbortSignal,
  expectedSize?: number,
): Promise<Buffer | undefined> => {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 0)
    throw new RangeError("File byte limit must be a nonnegative safe integer");
  if (
    expectedSize !== undefined &&
    (!Number.isSafeInteger(expectedSize) ||
      expectedSize < 0 ||
      expectedSize > maxBytes)
  )
    throw new RangeError("Expected file size must fit the byte limit");
  if (expectedSize === undefined) {
    const chunks: Buffer[] = [];
    const buffer = Buffer.allocUnsafe(Math.min(64 * 1_024, maxBytes + 1));
    let total = 0;
    for (;;) {
      signal?.throwIfAborted();
      const { bytesRead } = await handle.read(
        buffer,
        0,
        Math.min(buffer.length, maxBytes - total + 1),
        null,
      );
      if (bytesRead === 0) return Buffer.concat(chunks, total);
      total += bytesRead;
      if (total > maxBytes) return undefined;
      chunks.push(Buffer.from(buffer.subarray(0, bytesRead)));
    }
  }
  const buffer = Buffer.allocUnsafe(expectedSize);
  if (expectedSize === 0) return buffer;
  let total = 0;
  for (;;) {
    signal?.throwIfAborted();
    const { bytesRead } = await handle.read(
      buffer,
      total,
      expectedSize - total,
      null,
    );
    if (bytesRead === 0) return buffer.subarray(0, total);
    total += bytesRead;
    if (total > maxBytes) return undefined;
    if (total === expectedSize) return buffer;
  }
};
