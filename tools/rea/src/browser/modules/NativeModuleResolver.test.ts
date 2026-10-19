import { describe, expect, it } from "vitest";
import { NativeModuleResolver } from "./NativeModuleResolver.js";
import { projectAnalysisError } from "../../domain/analysisErrorProjection.js";

const input = {
  importerUrl: "https://app.test/main.js",
  importMap: null,
  specifiers: ["./dep.js"],
};
describe("native module resolver cleanup ownership", () => {
  it.each([false, true])(
    "awaits owned cleanup when acquisition is cancelled (cleanup fails: %s)",
    async (cleanupFails) => {
      const controller = new AbortController();
      let closed = false;
      const provider = new NativeModuleResolver(
        { REA_BROWSER_EXECUTABLE: "/browser" },
        () => {
          controller.abort();
          return Promise.resolve({
            version: () => "fixture",
            newContext: () => {
              throw new Error("must not acquire context");
            },
            close: () => {
              closed = true;
              if (cleanupFails)
                return Promise.reject(new Error("fixture cleanup refusal"));
              return Promise.resolve();
            },
          });
        },
      );
      const result = await provider.resolve(input, {
        signal: controller.signal,
      });
      expect(closed).toBe(true);
      if (result.ok) throw new Error("expected cancelled result");
      const projected = projectAnalysisError(result.error);
      expect(projected.code).toBe(
        cleanupFails ? "cleanup_incomplete" : "cancelled",
      );
      if (cleanupFails) {
        expect(projected.details).toMatchObject({
          diagnostics: { primary_error: { code: "cancelled" } },
        });
      }
    },
  );
  it("returns cleanup_incomplete when owned cleanup fails after an engine error", async () => {
    let launches = 0;
    let closes = 0;
    let cleanupAllowed = false;
    const provider = new NativeModuleResolver(
      { REA_BROWSER_EXECUTABLE: "/browser" },
      () => {
        launches += 1;
        return Promise.resolve({
          version: () => "fixture",
          newContext: () =>
            Promise.reject(new Error("fixture context failure")),
          close: async () => {
            closes += 1;
            if (!cleanupAllowed)
              throw new Error("fixture owned cleanup refusal");
          },
        });
      },
    );
    const result = await provider.resolve(input);
    if (result.ok) throw new Error("expected cleanup failure");
    expect(projectAnalysisError(result.error).code).toBe("cleanup_incomplete");
    expect(result.error.cleanupIncomplete).toBe(true);
    expect(projectAnalysisError(result.error).details).toMatchObject({
      diagnostics: {
        primary_error: {
          details: {
            diagnostics: { error_message: "fixture context failure" },
          },
        },
        cleanup_error: {
          details: { diagnostics: { reason: "fixture owned cleanup refusal" } },
        },
      },
    });
    const blocked = await provider.resolve(input);
    expect(blocked.ok).toBe(false);
    if (!blocked.ok) expect(blocked.error.cleanupIncomplete).toBe(true);
    expect(launches).toBe(1);
    await expect(provider.close()).rejects.toMatchObject({
      cleanupIncomplete: true,
    });
    cleanupAllowed = true;
    await provider.close();
    await provider.close();
    expect(closes).toBe(4);
    expect(launches).toBe(1);
  });
  it("awaits acquisition and cleanup while refusing queued work during shutdown", async () => {
    let acquired: () => void = () => {
      throw new Error("acquisition gate missing");
    };
    const acquiring = new Promise<void>((resolve) => {
      acquired = resolve;
    });
    let release: () => void = () => {
      throw new Error("release gate missing");
    };
    const released = new Promise<void>((resolve) => {
      release = resolve;
    });
    let launches = 0;
    let closes = 0;
    const provider = new NativeModuleResolver(
      { REA_BROWSER_EXECUTABLE: "/browser" },
      async () => {
        launches += 1;
        acquired();
        await released;
        return {
          version: () => "fixture",
          newContext: () =>
            Promise.reject(new Error("fixture context failure")),
          close: async () => {
            closes += 1;
          },
        };
      },
    );
    const first = provider.resolve(input);
    await acquiring;
    const queued = provider.resolve(input);
    const closing = provider.close();
    let closed = false;
    void closing.then(() => {
      closed = true;
    });
    await Promise.resolve();
    expect(closed).toBe(false);
    release();
    await first;
    const blocked = await queued;
    expect(blocked.ok).toBe(false);
    await closing;
    expect(closed).toBe(true);
    expect([launches, closes]).toEqual([1, 1]);
    expect((await provider.resolve(input)).ok).toBe(false);
  });
});

describe("optional native resolution acquisition", () => {
  it("does not load the browser engine when the selected operation lacks its configuration", async () => {
    const provider = new NativeModuleResolver({}, () => {
      throw new Error("must not launch");
    });
    const result = await provider.resolve(input);
    expect(result.ok).toBe(false);
    if (!result.ok)
      expect(projectAnalysisError(result.error)).toMatchObject({
        code: "capability_unavailable",
        message: expect.stringContaining("REA_BROWSER_EXECUTABLE"),
      });
  });
  it("does not acquire a browser after caller cancellation", async () => {
    const controller = new AbortController();
    controller.abort();
    const provider = new NativeModuleResolver(
      { REA_BROWSER_EXECUTABLE: "/browser" },
      () => {
        throw new Error("must not launch");
      },
    );
    const result = await provider.resolve(input, { signal: controller.signal });
    if (result.ok) throw new Error("expected cancellation");
    expect(result.error._tag).toBe("AnalysisCancelledError");
  });
  it("retains the selected executable and actual startup diagnostic", async () => {
    const provider = new NativeModuleResolver(
      { REA_BROWSER_EXECUTABLE: "/missing/browser" },
      () => Promise.reject(new Error("spawn EACCES fixture")),
    );
    const result = await provider.resolve(input);
    if (result.ok) throw new Error("expected startup failure");
    expect(projectAnalysisError(result.error).details).toMatchObject({
      diagnostics: {
        executable: "/missing/browser",
        error_message: "spawn EACCES fixture",
      },
    });
  });
});
