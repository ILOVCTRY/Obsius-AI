import { EventEmitter } from "node:events";

import { expect, it } from "vitest";

import { browserScenarioSchema } from "../domain/browserScenario.js";
import { BrowserScenarioSecrets } from "./BrowserScenarioSecrets.js";
import { PlaywrightScenarioStorage } from "./PlaywrightScenarioStorage.js";

class FakeCdp extends EventEmitter {
  readonly calls: { readonly method: string; readonly params?: unknown }[] = [];
  storageKey = "https://storage.example.test";
  failDetachCount = 0;
  holdDetach = false;
  resolveDetach: (() => void) | undefined;
  holdMethod: string | undefined;
  heldReply: (() => void) | undefined;
  autoReplyNested = false;
  readonly pendingNested: {
    readonly sessionId: string;
    readonly id: number;
  }[] = [];
  detachCalls = 0;

  async send(method: string, params?: unknown): Promise<unknown> {
    this.calls.push({ method, params });
    if (method === this.holdMethod) {
      const response =
        method === "Page.addScriptToEvaluateOnNewDocument"
          ? { identifier: "late-script" }
          : method === "Debugger.setBreakpointByUrl"
            ? { breakpointId: "late-breakpoint" }
            : method === "Storage.getStorageKey"
              ? { storageKey: this.storageKey }
              : {};
      return new Promise((resolve) => {
        this.heldReply = () => resolve(response);
      });
    }
    if (method === "Target.getTargetInfo")
      return { targetInfo: { targetId: "root-target" } };
    if (method === "Target.sendMessageToTarget") {
      const request = params as {
        readonly sessionId: string;
        readonly message: string;
      };
      const { id } = JSON.parse(request.message) as { readonly id: number };
      this.pendingNested.push({ sessionId: request.sessionId, id });
      if (this.autoReplyNested) this.replyNested(request.sessionId, id);
    }
    if (method === "Debugger.setBreakpointByUrl")
      return { breakpointId: "breakpoint" };
    if (method === "Page.addScriptToEvaluateOnNewDocument")
      return { identifier: "script" };
    if (method === "Storage.getStorageKey")
      return { storageKey: this.storageKey };
    if (method === "Debugger.evaluateOnCallFrame") return {};
    return {};
  }

  async detach(): Promise<void> {
    this.detachCalls += 1;
    if (this.failDetachCount > 0) {
      this.failDetachCount -= 1;
      throw new Error("one-shot CDP detach failure");
    }
    if (this.holdDetach)
      await new Promise<void>((resolve) => {
        this.resolveDetach = resolve;
      });
  }

  releaseHeldReply(): void {
    this.heldReply?.();
    this.heldReply = undefined;
    this.holdMethod = undefined;
  }

  releaseDetach(): void {
    this.holdDetach = false;
    this.resolveDetach?.();
    this.resolveDetach = undefined;
  }

  replyNested(sessionId: string, id: number): void {
    queueMicrotask(() =>
      this.emit("Target.receivedMessageFromTarget", {
        sessionId,
        message: JSON.stringify({ id, result: {} }),
      }),
    );
  }
}

const scenario = () =>
  browserScenarioSchema.parse({
    browser: { mode: "launch", executable_path: process.execPath },
    start_url: { url: "https://storage.example.test/" },
    actions: [{ step_id: "wait", action: "wait_for_timeout", duration_ms: 1 }],
    storage: {
      local_storage: [
        {
          origin: "https://storage.example.test",
          entries: [
            {
              name: "local-seed",
              value: { source: "literal", value: "local-value" },
            },
          ],
        },
      ],
      session_storage: [
        {
          origin: "https://storage.example.test",
          entries: [
            {
              name: "session-seed",
              value: { source: "literal", value: "session-value" },
            },
          ],
        },
      ],
    },
  });

const fixture = (cdp: FakeCdp) => {
  const page = new EventEmitter();
  const context = new EventEmitter();
  const browser = new EventEmitter();
  return {
    page,
    context,
    browser,
    contextApi: Object.assign(context, {
      browser: () => browser,
      newCDPSession: async () => cdp,
    }),
  };
};

const waitForCall = async (cdp: FakeCdp, method: string): Promise<void> => {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    if (cdp.calls.some((call) => call.method === method)) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error(`CDP method was not called: ${method}`);
};

const createInitializedStorage = async (
  context: ReturnType<typeof fixture>["contextApi"],
  page: ReturnType<typeof fixture>["page"],
  input = scenario(),
  options: { readonly signal?: AbortSignal } = {},
) => {
  const owner = await PlaywrightScenarioStorage.create(
    { context: context as never, page: page as never },
    input,
    BrowserScenarioSecrets.resolve(input, {})!,
    options,
  );
  await owner?.initialize(page as never, context as never);
  return owner;
};

const emitSeedPause = (cdp: FakeCdp): void => {
  const addScript = cdp.calls.find(
    ({ method }) => method === "Page.addScriptToEvaluateOnNewDocument",
  );
  if (addScript === undefined)
    throw new Error("Storage seed script is missing");
  const sourceUrl = (addScript.params as { readonly worldName: string })
    .worldName;
  cdp.emit("Runtime.executionContextCreated", {
    context: {
      id: 4,
      origin: "https://storage.example.test",
      name: sourceUrl,
      auxData: { frameId: "root-frame" },
    },
  });
  cdp.emit("Debugger.scriptParsed", {
    scriptId: "seed-script",
    executionContextId: 4,
    url: sourceUrl,
  });
  cdp.emit("Debugger.paused", {
    callFrames: [
      {
        callFrameId: "seed-frame",
        location: { scriptId: "seed-script" },
      },
    ],
  });
};

it("seeds a browser storage area once before the document script resumes", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const owner = await createInitializedStorage(contextApi, page);
  expect(owner).toBeDefined();

  cdp.emit("Debugger.paused", {
    callFrames: [
      {
        callFrameId: "caller-debugger-frame",
        location: { scriptId: "caller-owned-script" },
      },
    ],
  });
  await owner?.settle();
  expect(
    cdp.calls.some(({ method }) => method === "Debugger.evaluateOnCallFrame"),
  ).toBe(false);
  expect(cdp.calls.some(({ method }) => method === "Debugger.resume")).toBe(
    false,
  );

  emitSeedPause(cdp);
  await owner?.settle();
  emitSeedPause(cdp);
  await owner?.settle();

  const evaluations = cdp.calls.filter(
    ({ method }) => method === "Debugger.evaluateOnCallFrame",
  );
  expect(evaluations).toHaveLength(1);
  expect(evaluations[0]?.params).toMatchObject({
    callFrameId: "seed-frame",
    expression: expect.stringContaining('"local-seed","local-value"'),
  });
  expect(evaluations[0]?.params).toMatchObject({
    expression: expect.stringContaining('"session-seed","session-value"'),
  });
  expect(
    cdp.calls.findIndex(
      ({ method }) => method === "Debugger.evaluateOnCallFrame",
    ),
  ).toBeLessThan(
    cdp.calls.findIndex(({ method }) => method === "Debugger.resume"),
  );
  await owner?.close();
});

it("seeds session storage once per namespace and StorageKey", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const base = scenario();
  const input = browserScenarioSchema.parse({
    ...base,
    storage: {
      local_storage: [],
      session_storage: base.storage.session_storage,
    },
  });
  const owner = await createInitializedStorage(contextApi, page, input);

  emitSeedPause(cdp);
  await owner?.settle();
  emitSeedPause(cdp);
  await owner?.settle();
  cdp.storageKey = "https://storage.example.test/^0https://top-level.example";
  emitSeedPause(cdp);
  await owner?.settle();

  expect(
    cdp.calls.filter(({ method }) => method === "Debugger.evaluateOnCallFrame"),
  ).toHaveLength(2);
  await owner?.close();
});

it("retries the owning CDP detach after a failed attempt", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const owner = await createInitializedStorage(contextApi, page);
  cdp.failDetachCount = 1;

  await expect(owner?.close()).rejects.toMatchObject({
    _tag: "BrowserObservationError",
    reason: "cleanup_failed",
  });
  expect(cdp.detachCalls).toBe(1);
  expect(cdp.listenerCount("Debugger.paused")).toBe(1);

  await owner?.close();
  expect(cdp.detachCalls).toBe(2);
  expect(cdp.listenerCount("Debugger.paused")).toBe(0);
});

it("does not create storage instrumentation when there are no storage seeds", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const empty = browserScenarioSchema.parse({
    browser: { mode: "launch", executable_path: process.execPath },
    start_url: { url: "https://storage.example.test/" },
    actions: [{ step_id: "wait", action: "wait_for_timeout", duration_ms: 1 }],
  });
  expect(
    await PlaywrightScenarioStorage.create(
      { context: contextApi as never, page: page as never },
      empty,
      BrowserScenarioSecrets.resolve(empty, {})!,
    ),
  ).toBeUndefined();
  expect(cdp.calls).toEqual([]);
});

it("detaches after setup cancellation even while script installation is pending", async () => {
  const cdp = new FakeCdp();
  cdp.holdMethod = "Page.addScriptToEvaluateOnNewDocument";
  const { page, contextApi } = fixture(cdp);
  const input = scenario();
  const controller = new AbortController();
  const owner = await PlaywrightScenarioStorage.create(
    { context: contextApi as never, page: page as never },
    input,
    BrowserScenarioSecrets.resolve(input, {})!,
    { signal: controller.signal },
  );
  expect(owner).toBeDefined();
  const initialization = owner?.initialize(page as never, contextApi as never);
  await waitForCall(cdp, "Page.addScriptToEvaluateOnNewDocument");
  controller.abort();
  await expect(initialization).rejects.toMatchObject({
    _tag: "AnalysisCancelledError",
  });

  await owner?.close();
  expect(cdp.detachCalls).toBe(1);
  expect(
    cdp.calls.some(
      ({ method }) => method === "Page.removeScriptToEvaluateOnNewDocument",
    ),
  ).toBe(false);
  cdp.releaseHeldReply();
});

it("cancels a pending storage-key reply and still releases its CDP owner", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const input = scenario();
  const controller = new AbortController();
  const owner = await createInitializedStorage(contextApi, page, input, {
    signal: controller.signal,
  });
  expect(owner).toBeDefined();
  cdp.holdMethod = "Storage.getStorageKey";
  emitSeedPause(cdp);
  const settling = owner?.settle();
  await waitForCall(cdp, "Storage.getStorageKey");
  controller.abort();
  await expect(settling).rejects.toMatchObject({
    _tag: "AnalysisCancelledError",
  });

  cdp.releaseHeldReply();
  await owner?.close();
  expect(cdp.detachCalls).toBe(1);
});

it("releases the CDP owner when an attached target reply remains pending", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const input = scenario();
  const owner = await createInitializedStorage(contextApi, page, input);
  expect(owner).toBeDefined();
  cdp.emit("Target.attachedToTarget", {
    sessionId: "iframe-session",
    targetInfo: { type: "iframe", targetId: "iframe-target" },
  });
  await waitForCall(cdp, "Target.sendMessageToTarget");
  await owner?.close();
  expect(cdp.detachCalls).toBe(1);
});

it("does not fail root storage capture when an OOPIF detaches during setup", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const owner = await createInitializedStorage(contextApi, page);
  expect(owner).toBeDefined();
  cdp.emit("Target.attachedToTarget", {
    sessionId: "navigated-away-iframe",
    targetInfo: { type: "iframe", targetId: "iframe-target" },
  });
  await waitForCall(cdp, "Target.sendMessageToTarget");

  cdp.emit("Target.detachedFromTarget", {
    sessionId: "navigated-away-iframe",
  });
  await owner?.settle();

  cdp.autoReplyNested = true;
  emitSeedPause(cdp);
  await owner?.settle();
  expect(
    cdp.calls.some(({ method }) => method === "Debugger.evaluateOnCallFrame"),
  ).toBe(true);
  await owner?.close();
  expect(cdp.detachCalls).toBe(1);
});

it("retains the exact in-flight detach after the bounded wait expires", async () => {
  const cdp = new FakeCdp();
  const { page, contextApi } = fixture(cdp);
  const owner = await createInitializedStorage(contextApi, page);
  expect(owner).toBeDefined();
  cdp.holdDetach = true;

  await expect(owner?.close()).rejects.toMatchObject({
    _tag: "BrowserObservationError",
    reason: "cleanup_failed",
  });
  expect(cdp.detachCalls).toBe(1);
  const retry = owner?.close();
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(cdp.detachCalls).toBe(1);
  cdp.releaseDetach();
  await retry;
  expect(cdp.detachCalls).toBe(1);
});
