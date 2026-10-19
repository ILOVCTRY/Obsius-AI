import type {
  ProcessOwnershipHost,
  ProcessRunTokenObservation,
  ProcessTableEntry,
} from "./ProcessOwnership.js";

const errorMessage = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause);

/** Read only ownership tokens, using per-process fallback for missing batch rows. */
export async function* readProcessRunTokens(
  host: ProcessOwnershipHost,
  processes: readonly ProcessTableEntry[],
  signal?: AbortSignal,
): AsyncGenerator<{
  readonly process: ProcessTableEntry;
  readonly observation: ProcessRunTokenObservation;
}> {
  signal?.throwIfAborted();
  if (processes.length === 0) return;
  let batch: ReadonlyMap<number, ProcessRunTokenObservation> | undefined;
  let batchFailure: string | undefined;
  if (host.runTokens !== undefined) {
    try {
      batch =
        signal === undefined
          ? await host.runTokens(processes)
          : await host.runTokens(processes, signal);
    } catch (cause: unknown) {
      signal?.throwIfAborted();
      batchFailure = errorMessage(cause);
    }
  }
  for (const process of processes) {
    signal?.throwIfAborted();
    let observation = batch?.get(process.pid);
    if (observation === undefined) {
      try {
        const environment =
          signal === undefined
            ? await host.environment(process.pid)
            : await host.environment(process.pid, signal);
        observation = {
          state: "readable",
          runId: environment.REA_PROCESS_RUN_ID,
        };
      } catch (cause: unknown) {
        signal?.throwIfAborted();
        const reason = errorMessage(cause);
        observation = {
          state: "unavailable",
          reason:
            batchFailure === undefined
              ? reason
              : `${reason}; run-token batch failed: ${batchFailure}`,
        };
      }
    }
    signal?.throwIfAborted();
    yield { process, observation };
  }
}
