import { describe, expect, it } from "vitest";

import {
  APKTOOL_COMMAND_ENV,
  apktoolConfigurationAvailability,
  ApktoolConfigurationFailure,
  apktoolInvocation,
  inspectApktoolClient,
  parseApktoolVersionOutput,
  resolveApktoolCommand,
} from "./ApktoolConfiguration.js";

describe("resolveApktoolCommand", () => {
  it("prefers an absolute REA_APKTOOL_COMMAND", () => {
    expect(
      resolveApktoolCommand({
        [APKTOOL_COMMAND_ENV]: "/usr/local/bin/apktool",
      }),
    ).toEqual({
      command: "/usr/local/bin/apktool",
      commandSource: "environment",
    });
  });

  it("falls back to PATH resolution without the environment key", () => {
    expect(resolveApktoolCommand({})).toEqual({
      command: "apktool",
      commandSource: "path",
    });
  });

  it("rejects a relative environment path explicitly", () => {
    expect(() =>
      resolveApktoolCommand({ [APKTOOL_COMMAND_ENV]: "bin/apktool" }),
    ).toThrow(/must be an absolute path/u);
  });
});

describe("parseApktoolVersionOutput", () => {
  it("reads the version from real apktool output", () => {
    expect(parseApktoolVersionOutput("2.7.0-dirty\n")).toEqual({
      version: "2.7.0-dirty",
    });
  });

  it("reports null when the launcher prints nothing usable", () => {
    expect(parseApktoolVersionOutput(" \n")).toEqual({ version: null });
  });
});

describe("apktoolInvocation", () => {
  const windows = { SystemRoot: "C:\\Windows" };

  it("runs a Windows batch launcher through an explicit cmd.exe call", () => {
    expect(
      apktoolInvocation(
        {
          command: "C:\\ProgramData\\chocolatey\\bin\\apktool.BAT",
          commandSource: "environment",
        },
        ["d", "-o", "C:\\Users\\Analyst\\AppData\\Local\\Temp\\rea out"],
        windows,
        "win32",
      ),
    ).toEqual({
      command: "C:\\Windows\\System32\\cmd.exe",
      arguments: [
        "/d",
        "/e:on",
        "/v:off",
        "/s",
        "/c",
        '""C:\\ProgramData\\chocolatey\\bin\\apktool.BAT" "d" "-o" "C:\\Users\\Analyst\\AppData\\Local\\Temp\\rea out""',
      ],
      windowsVerbatimArguments: true,
    });
  });

  it("runs native executables and POSIX scripts directly", () => {
    for (const [command, platform] of [
      ["C:\\tools\\apktool.exe", "win32"],
      ["/opt/apktool/apktool.cmd", "linux"],
    ] as const)
      expect(
        apktoolInvocation(
          { command, commandSource: "environment" },
          ["--version"],
          windows,
          platform,
        ),
      ).toEqual({
        command,
        arguments: ["--version"],
        windowsVerbatimArguments: false,
      });
  });

  it("reports a batch launcher that cmd.exe would reinterpret before running it", async () => {
    const failure = await inspectApktoolClient(
      {
        command: "C:\\tools & more\\apktool.cmd",
        commandSource: "environment",
      },
      undefined,
      windows,
      "win32",
    ).catch((cause: unknown) => cause);

    expect(failure).toBeInstanceOf(ApktoolConfigurationFailure);
    if (!(failure instanceof ApktoolConfigurationFailure)) return;
    expect(failure.code).toBe("launcher_unsupported");
    expect(apktoolConfigurationAvailability(failure)).toMatchObject({
      status: "unavailable",
      code: "not_configured",
      diagnostics: {
        selected_command: "C:\\tools & more\\apktool.cmd",
        remediation: expect.stringContaining(APKTOOL_COMMAND_ENV),
      },
    });
  });
});
