import {
  AnalysisError,
  type AnalysisErrorOptions,
} from "./analysisErrorBase.js";
import type {
  HopperProcessProviderState,
  ProviderFailureStage,
} from "./providerOperationHealth.js";
import {
  hopperStartupFailure,
  type HopperStartupFailureCode,
  type HopperStartupFailureDiagnostic,
} from "./hopperStartupFailure.js";

/** Base class for failures produced specifically by the Hopper provider. */
export abstract class HopperError extends AnalysisError {}

/** Observed launcher outcome; a helper exit does not establish GUI lifetime. */
export type HopperLauncherOutcome = {
  readonly exit_code: number | null;
  readonly signal: string | null;
  /** Total bytes drained from the stream; retained_bytes reports captured bytes. */
  readonly stdout: {
    readonly text: string;
    readonly bytes: number;
    readonly retained_bytes: number;
  };
  readonly stderr: {
    readonly text: string;
    readonly bytes: number;
    readonly retained_bytes: number;
  };
  readonly output_closed: boolean;
  readonly diagnostic_truncated: boolean;
};

/** Hopper did not respond within the configured operation deadline. */
export class HopperTimeoutError extends HopperError {
  readonly _tag = "HopperTimeoutError";

  constructor(
    readonly timeoutMs: number,
    readonly operation?: string,
    readonly requestId?: number,
    readonly providerState: "busy" | "not_started" = "not_started",
    readonly stage: ProviderFailureStage = operation === undefined
      ? "launch"
      : "analysis",
    readonly launcherOutcome?: HopperLauncherOutcome,
  ) {
    super(
      `Hopper ${operation === undefined ? "startup" : operation} timed out after ${String(timeoutMs)}ms`,
    );
  }
}

/** The caller cancelled a Hopper operation before it completed. */
export class HopperCancelledError extends HopperError {
  readonly _tag = "HopperCancelledError";

  constructor() {
    super("Hopper operation was cancelled");
  }
}

/** Hopper returned bytes or JSON that do not satisfy the NDJSON RPC contract. */
export class HopperProtocolError extends HopperError {
  readonly _tag = "HopperProtocolError";
}

export type HopperDiagnosticType =
  | "remote"
  | "authorization"
  | "invalid_request"
  | "capability_unavailable"
  | "bridge_exception";

/** Hopper's JSON-RPC endpoint returned an expected remote error response. */
export interface HopperRemoteErrorContext extends AnalysisErrorOptions {
  readonly diagnosticType?: HopperDiagnosticType | undefined;
  readonly operation?: string | undefined;
  readonly requestId?: number | undefined;
}

export class HopperRemoteError extends HopperError {
  readonly _tag = "HopperRemoteError";
  readonly diagnosticType: HopperDiagnosticType;
  readonly operation: string | undefined;
  readonly requestId: number | undefined;

  constructor(
    readonly code: number,
    readonly safeMessage: string,
    context: HopperRemoteErrorContext = {},
  ) {
    super(`Hopper request failed (${String(code)}): ${safeMessage}`, context);
    this.diagnosticType = context.diagnosticType ?? "remote";
    this.operation = context.operation;
    this.requestId = context.requestId;
  }
}

/** The owned Hopper bridge stopped before its client was closed. */
export interface HopperProcessErrorContext extends AnalysisErrorOptions {
  readonly diagnostic?: HopperStartupFailureDiagnostic | undefined;
  readonly operation?: string | undefined;
  readonly requestId?: number | undefined;
  readonly providerState?: HopperProcessProviderState | undefined;
  readonly stage?: ProviderFailureStage | undefined;
  readonly launcherFailure?: HopperLauncherOutcome | undefined;
}

export class HopperProcessError extends HopperError {
  readonly _tag = "HopperProcessError";
  readonly failureCode: HopperStartupFailureCode | undefined;
  override readonly userMessage: string | undefined;
  readonly providerState: HopperProcessProviderState;
  readonly stage: ProviderFailureStage;
  readonly diagnostic: HopperStartupFailureDiagnostic | undefined;
  readonly operation: string | undefined;
  readonly requestId: number | undefined;
  readonly launcherFailure: HopperLauncherOutcome | undefined;

  constructor(
    readonly exitCode: number | null,
    context: HopperProcessErrorContext = {},
  ) {
    super(
      `Hopper bridge stopped unexpectedly with code ${String(exitCode)}`,
      context,
    );
    this.diagnostic = context.diagnostic;
    this.operation = context.operation;
    this.requestId = context.requestId;
    this.launcherFailure = context.launcherFailure;
    const failure = hopperStartupFailure(exitCode);
    this.stage =
      context.stage ??
      (failure === undefined
        ? context.operation === undefined
          ? "connection"
          : "analysis"
        : "launch");
    this.failureCode = failure?.code;
    this.userMessage = failure?.message;
    this.providerState =
      context.providerState ?? (exitCode === null ? "unknown" : "exited");
  }
}

/** Hopper or the repository bridge could not be started. */
export class HopperStartError extends HopperError {
  readonly _tag = "HopperStartError";
  override readonly userMessage: string | undefined;
  readonly ownerRunId: string | undefined;
  readonly launcherFailure: HopperLauncherOutcome | undefined;

  constructor(
    options?: ErrorOptions & {
      readonly userMessage?: string;
      readonly ownerRunId?: string;
      readonly launcherFailure?: HopperLauncherOutcome;
    },
  ) {
    super("Hopper application bridge could not be started", options);
    this.userMessage = options?.userMessage;
    this.ownerRunId = options?.ownerRunId;
    this.launcherFailure = options?.launcherFailure;
  }
}
