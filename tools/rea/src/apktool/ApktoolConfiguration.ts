import { AnalysisCancelledError } from "../domain/analysisErrorCore.js";
import { isAbsolute } from "node:path";

import {
  execFileOutput,
  execFileOutputFailure,
} from "../process/ExecFileOutput.js";
import type { ProviderAvailability } from "../application/AnalysisProvider.js";
import type { ProviderRejectionCode } from "../contracts/providerSelection.js";
import type { JsonValue } from "../domain/jsonValue.js";
import {
  isWindowsBatchFile,
  windowsBatchCommand,
  WindowsBatchCommandError,
} from "../process/WindowsBatchCommand.js";

/** Environment key selecting the caller's apktool launcher. */
export const APKTOOL_COMMAND_ENV = "REA_APKTOOL_COMMAND";

const VERSION_TIMEOUT_MS = 20_000;
const VERSION_MAX_OUTPUT_BYTES = 64 * 1024;

/** How the apktool launcher was selected for this provider instance. */
export interface ApktoolCommandSelection {
  readonly command: string;
  readonly commandSource: "environment" | "path";
}

/**
 * Resolve the caller-supplied apktool launcher. `REA_APKTOOL_COMMAND` must
 * be absolute so the selection stays explicit; without it, apktool is
 * resolved from `PATH` at run time. Both the upstream wrapper script and
 * `java -jar` launcher scripts work: REA only runs `<command> --version`
 * and `<command> d ...`.
 */
export const resolveApktoolCommand = (
  environment: Readonly<Record<string, string | undefined>>,
): ApktoolCommandSelection => {
  const configured = environment[APKTOOL_COMMAND_ENV];
  if (configured === undefined || configured.trim() === "")
    return { command: "apktool", commandSource: "path" };
  if (!isAbsolute(configured))
    throw new ApktoolConfigurationFailure(
      "not_absolute",
      `${APKTOOL_COMMAND_ENV} must be an absolute path to the apktool launcher`,
      { configured_command: configured },
    );
  return { command: configured, commandSource: "environment" };
};

export type ApktoolConfigurationFailureCode =
  | "not_absolute"
  | "command_missing"
  | "launcher_unsupported"
  | "version_unresolved";

export class ApktoolConfigurationFailure extends Error {
  constructor(
    readonly code: ApktoolConfigurationFailureCode,
    message: string,
    readonly diagnostics: Readonly<Record<string, JsonValue>> = {},
    options?: ErrorOptions,
  ) {
    super(message, options);
  }
}

export interface ApktoolClientIdentity {
  readonly version: string | null;
}

/** Process creation for one apktool run on the selected host. */
export interface ApktoolInvocation {
  readonly command: string;
  readonly arguments: readonly string[];
  readonly windowsVerbatimArguments: boolean;
}

/**
 * Run the selected launcher directly, or through cmd.exe when Windows can
 * only execute it there (the Chocolatey `apktool.bat` and `java -jar`
 * wrapper scripts). Throws WindowsBatchCommandError when a token cannot be
 * passed to cmd.exe safely.
 */
export const apktoolInvocation = (
  selection: ApktoolCommandSelection,
  arguments_: readonly string[],
  environment: Readonly<NodeJS.ProcessEnv>,
  platform: NodeJS.Platform = process.platform,
): ApktoolInvocation =>
  platform === "win32" && isWindowsBatchFile(selection.command)
    ? {
        ...windowsBatchCommand({
          environment,
          batchPath: selection.command,
          arguments: arguments_,
        }),
        windowsVerbatimArguments: true,
      }
    : {
        command: selection.command,
        arguments: arguments_,
        windowsVerbatimArguments: false,
      };

/** Read the version from `apktool --version` output (first non-empty line). */
export const parseApktoolVersionOutput = (
  stdout: string,
): ApktoolClientIdentity => {
  const line = stdout
    .split(/\r?\n/u)
    .map((entry) => entry.trim())
    .find((entry) => entry !== "");
  return { version: line === undefined ? null : line };
};

/** Probe the selected apktool launcher without decoding anything. */
export const inspectApktoolClient = async (
  selection: ApktoolCommandSelection,
  signal?: AbortSignal,
  environment: Readonly<Record<string, string | undefined>> = process.env,
  platform: NodeJS.Platform = process.platform,
): Promise<ApktoolClientIdentity> => {
  const hostEnvironment = { ...process.env, ...environment };
  let invocation: ApktoolInvocation;
  try {
    invocation = apktoolInvocation(
      selection,
      ["--version"],
      hostEnvironment,
      platform,
    );
  } catch (cause) {
    if (!(cause instanceof WindowsBatchCommandError)) throw cause;
    throw new ApktoolConfigurationFailure(
      "launcher_unsupported",
      `The selected apktool launcher cannot be run through cmd.exe: ${cause.message}`,
      {
        selected_command: selection.command,
        command_source: selection.commandSource,
        remediation: `Set ${APKTOOL_COMMAND_ENV} to a launcher path without cmd.exe metacharacters (" % ^ ! & | < >), or to a native apktool executable`,
      },
      { cause },
    );
  }
  try {
    const { stdout } = await execFileOutput(
      invocation.command,
      invocation.arguments,
      {
        windowsVerbatimArguments: invocation.windowsVerbatimArguments,
        timeout: VERSION_TIMEOUT_MS,
        stopSignal: "SIGTERM",
        maxBuffer: VERSION_MAX_OUTPUT_BYTES,
        env: hostEnvironment,
        ...(signal === undefined ? {} : { signal }),
      },
    );
    return parseApktoolVersionOutput(stdout);
  } catch (cause) {
    const failure = execFileOutputFailure(cause);
    if (signal?.aborted || failure?.code === "ABORT_ERR")
      throw new AnalysisCancelledError("inspect_apktool_client", { cause });
    if (failure?.code === "ENOENT")
      throw new ApktoolConfigurationFailure(
        "command_missing",
        `The selected apktool launcher was not found: ${selection.command}`,
        {
          selected_command: selection.command,
          command_source: selection.commandSource,
          remediation: `Set ${APKTOOL_COMMAND_ENV} to an absolute apktool launcher (the upstream wrapper or a java -jar script)`,
        },
        { cause },
      );
    throw new ApktoolConfigurationFailure(
      "version_unresolved",
      "The selected apktool launcher did not report a usable version",
      {
        selected_command: selection.command,
        ...(failure === undefined
          ? {}
          : {
              exit_code: failure.code,
              stderr_excerpt: failure.stderr.slice(0, 512),
            }),
      },
      { cause },
    );
  }
};

/** Availability in the shape shared by every provider. */
const rejectionCodeOf = (
  code: ApktoolConfigurationFailureCode,
): ProviderRejectionCode =>
  code === "command_missing"
    ? "executable_missing"
    : code === "not_absolute" || code === "launcher_unsupported"
      ? "not_configured"
      : "version_unresolved";

export const apktoolConfigurationAvailability = (
  cause: ApktoolConfigurationFailure,
): ProviderAvailability =>
  ({
    status: "unavailable",
    code: rejectionCodeOf(cause.code),
    reason: cause.message,
    diagnostics: { ...cause.diagnostics },
  }) as ProviderAvailability;
