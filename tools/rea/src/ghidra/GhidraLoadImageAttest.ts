import type { AnalysisOperation } from "../application/AnalysisProvider.js";
import { AnalysisCancelledError } from "../domain/analysisErrorCore.js";
import type { BinaryTarget } from "../domain/binaryTargetTypes.js";
import type { AnalysisError } from "../domain/analysisErrorBase.js";
import { jsonValueSchema, type JsonValue } from "../domain/jsonValue.js";
import {
  nativeLoadImageObservationSchema,
  nativeLoadImageSchema,
} from "../domain/native/nativeLoadImage.js";
import { ProviderAdapterError } from "../domain/providerAdapterError.js";
import { err, ok, type Result } from "../domain/result.js";
import {
  attestGhidraDosComLoadImage,
  attestGhidraDosLoadImage,
  attestGhidraRawBinaryLoadImage,
} from "./GhidraLoadImageValues.js";
import type { NativeAotPeResult } from "../domain/native/nativeAotPe.js";
import type { GhidraClient } from "./GhidraClient.js";
import type { GhidraSessionError } from "./GhidraSessionError.js";

type LoadImageSnapshotClient = Partial<
  Pick<GhidraClient, "hashTargetSnapshot" | "readTargetSnapshot">
>;

type NativeLoadImageAttestationInput = {
  readonly target: BinaryTarget;
  readonly operation: AnalysisOperation;
  readonly measured: JsonValue;
  readonly client: LoadImageSnapshotClient;
  readonly mapSessionError: (failure: GhidraSessionError) => AnalysisError;
  readonly nativeAotResult?: NativeAotPeResult | undefined;
  readonly signal?: AbortSignal | undefined;
};

const isSignalAborted = (signal: AbortSignal | undefined): boolean =>
  signal?.aborted === true;

/** Independently verify measured load-image observations for admitted DOS and raw-binary targets. */
export const attestGhidraNativeLoadImage = async (
  input: NativeLoadImageAttestationInput,
): Promise<Result<JsonValue, AnalysisError>> => {
  const {
    target,
    operation,
    measured,
    client,
    mapSessionError,
    nativeAotResult,
    signal,
  } = input;
  if (isSignalAborted(signal))
    return err(new AnalysisCancelledError(operation));
  const observations = nativeLoadImageObservationSchema.parse(measured);
  if (target.format === "pe") {
    if (nativeAotResult === undefined)
      return err(
        new ProviderAdapterError("ghidra", operation, {
          diagnostics: {
            reason:
              "The Ghidra provider did not produce read-only NativeAOT metadata for this admitted PE snapshot.",
          },
        }),
      );
    if (isSignalAborted(signal))
      return err(new AnalysisCancelledError(operation));
    try {
      return ok(
        jsonValueSchema.parse(
          nativeLoadImageSchema.parse({
            status: "unsupported",
            reason:
              "Independent load-image verification currently supports DOS MZ, explicit DOS COM, and caller-supplied raw-binary targets only.",
            observations: {
              ...observations,
              metadata_recovery: [
                ...(observations.metadata_recovery ?? []),
                nativeAotResult.summary,
              ],
            },
            limitations: [
              "Load-image verification is not performed for PE targets; NativeAOT metadata observations are derived separately from the exact admitted snapshot.",
            ],
          }),
        ),
      );
    } catch (cause: unknown) {
      return err(
        new ProviderAdapterError("ghidra", operation, {
          diagnostics: {
            reason:
              cause instanceof Error
                ? cause.message
                : "NativeAOT metadata could not be represented in the load-image result",
          },
        }),
      );
    }
  }
  if (
    target.format !== "dos-mz" &&
    target.format !== "dos-com" &&
    target.format !== "raw-binary"
  ) {
    return ok(
      jsonValueSchema.parse(
        nativeLoadImageSchema.parse({
          status: "unsupported",
          reason:
            "Independent load-image verification currently supports DOS MZ, explicit DOS COM, and caller-supplied raw-binary targets only.",
          observations,
          limitations: [
            "Other formats expose measured mappings and source identities; no format-specific verification was performed.",
          ],
        }),
      ),
    );
  }
  if (target.format === "raw-binary") {
    if (client.hashTargetSnapshot === undefined)
      return err(
        new ProviderAdapterError("ghidra", operation, {
          diagnostics: {
            reason:
              "The Ghidra client does not expose its immutable target snapshot identity for independent raw-binary load-image verification.",
          },
        }),
      );
    const snapshot = await client.hashTargetSnapshot(signal);
    if (!snapshot.ok) return err(mapSessionError(snapshot.error));
    if (isSignalAborted(signal))
      return err(new AnalysisCancelledError(operation));
    const attested = attestGhidraRawBinaryLoadImage(
      snapshot.value,
      target,
      observations,
    );
    return attested.ok
      ? ok(jsonValueSchema.parse(attested.value))
      : err(
          new ProviderAdapterError("ghidra", operation, {
            diagnostics: { reason: attested.error },
          }),
        );
  }
  if (client.readTargetSnapshot === undefined)
    return err(
      new ProviderAdapterError("ghidra", operation, {
        diagnostics: {
          reason:
            "The Ghidra client does not expose its immutable target snapshot for independent load-image verification.",
        },
      }),
    );
  const snapshot = await client.readTargetSnapshot(undefined, signal);
  if (!snapshot.ok) return err(mapSessionError(snapshot.error));
  if (snapshot.value.kind !== "captured")
    return err(
      new ProviderAdapterError("ghidra", operation, {
        diagnostics: {
          reason:
            "The uncapped load-image snapshot was unexpectedly not captured.",
        },
      }),
    );
  const attested = (
    target.format === "dos-com"
      ? attestGhidraDosComLoadImage
      : attestGhidraDosLoadImage
  )(snapshot.value.bytes, target.sha256 ?? "", observations);
  if (!attested.ok)
    return err(
      new ProviderAdapterError("ghidra", operation, {
        diagnostics: { reason: attested.error },
      }),
    );
  return ok(jsonValueSchema.parse(attested.value));
};
