import type { FileHandle } from "node:fs/promises";
import { Readable } from "node:stream";

/** Stream positional reads while leaving descriptor cleanup to its owner. */
export const readFileHandleChunks = (
  handle: Pick<FileHandle, "read">,
  options: {
    readonly start?: number | bigint;
    readonly end?: number | bigint;
    readonly signal?: AbortSignal | undefined;
  } = {},
): Readable => {
  const start = fileOffset(options.start ?? 0);
  const unlimited = options.end === undefined || options.end === Infinity;
  const end = unlimited ? undefined : fileOffset(options.end);
  if (
    start === undefined ||
    (!unlimited && end === undefined) ||
    (end !== undefined && end < start)
  )
    throw new RangeError("Invalid file read range");
  return Readable.from(
    (async function* () {
      let position = start;
      while (end === undefined || position <= end) {
        options.signal?.throwIfAborted();
        const remaining = end === undefined ? undefined : end - position + 1n;
        const chunk = Buffer.allocUnsafe(
          remaining === undefined || remaining > 64n * 1024n
            ? 64 * 1024
            : Number(remaining),
        );
        const { bytesRead } = await handle.read(
          chunk,
          0,
          chunk.length,
          position,
        );
        options.signal?.throwIfAborted();
        if (bytesRead === 0) return;
        position += BigInt(bytesRead);
        yield chunk.subarray(0, bytesRead);
      }
    })(),
    {
      objectMode: false,
      ...(options.signal === undefined ? {} : { signal: options.signal }),
    },
  );
};

const fileOffset = (value: number | bigint): bigint | undefined => {
  if (typeof value === "bigint") return value >= 0n ? value : undefined;
  return Number.isSafeInteger(value) && value >= 0 ? BigInt(value) : undefined;
};
