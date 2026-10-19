import { EventEmitter } from "node:events";

import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  openBrowser: vi.fn(),
  storage: undefined as
    | {
        initialize: ReturnType<typeof vi.fn>;
        settle: ReturnType<typeof vi.fn>;
        close: ReturnType<typeof vi.fn>;
      }
    | undefined,
  finishEvents: vi.fn(),
}));

vi.mock("./PlaywrightScenarioBrowser.js", () => ({
  openPlaywrightScenarioBrowser: mocks.openBrowser,
  failBrowserScenarioOperation: async (
    cleanup: () => Promise<void>,
    cause: unknown,
  ) => {
    try {
      await cleanup();
    } catch (cleanupCause: unknown) {
      throw new AggregateError([cause, cleanupCause]);
    }
    throw cause;
  },
}));

vi.mock("./PlaywrightScenarioStorage.js", () => ({
  PlaywrightScenarioStorage: {
    create: vi.fn(async () => mocks.storage),
  },
}));

vi.mock("./PlaywrightScenarioEvents.js", () => ({
  PlaywrightScenarioEvents: class {
    finish() {
      mocks.finishEvents();
    }

    result() {
      return { events: [] };
    }

    limitations() {
      return [];
    }

    setStep() {}

    nextSequence() {
      return 1;
    }

    lastSequence() {
      return 0;
    }
  },
}));

import { browserScenarioSchema } from "../domain/browserScenario.js";
import { PlaywrightScenarioSession } from "./PlaywrightScenarioSession.js";

const scenario = browserScenarioSchema.parse({
  browser: { mode: "launch", executable_path: process.execPath },
  start_url: { url: "https://storage.example.test/" },
  actions: [{ step_id: "wait", action: "wait_for_timeout", duration_ms: 1 }],
  storage: {
    local_storage: [
      {
        origin: "https://storage.example.test",
        entries: [{ name: "seed", value: { source: "literal", value: "v" } }],
      },
    ],
  },
});

const fixture = () => {
  const page = Object.assign(new EventEmitter(), {
    url: () => "about:blank",
    goto: vi.fn(async () => undefined),
    context: () => context,
  });
  const context = Object.assign(new EventEmitter(), {
    setDefaultTimeout: vi.fn(),
    setDefaultNavigationTimeout: vi.fn(),
    addCookies: vi.fn(async () => undefined),
    browser: () => browser,
  });
  const browser = Object.assign(new EventEmitter(), {
    version: () => "Chrome 154",
  });
  let browserClosed = false;
  const close = vi.fn(async (finishEvents?: () => Promise<void>) => {
    await finishEvents?.();
    browserClosed = true;
  });
  const cleanup = {
    close,
    get browserClosed() {
      return browserClosed;
    },
  };
  mocks.openBrowser.mockResolvedValue({
    page,
    context,
    browser,
    cleanup,
    profilePath: "/tmp/profile",
  });
  mocks.storage = {
    initialize: vi.fn(async () => undefined),
    settle: vi.fn(async () => undefined),
    close: vi.fn(async () => undefined),
  };
  return { cleanup, storage: mocks.storage };
};

beforeEach(() => {
  vi.clearAllMocks();
});

it("uses browser disconnect as terminal storage release after detach failure", async () => {
  const { cleanup, storage } = fixture();
  const detachFailure = new Error("CDP detach rejected");
  storage.close.mockRejectedValueOnce(detachFailure);
  const session = await PlaywrightScenarioSession.open(scenario, {}, {});

  await expect(session.close()).resolves.toBe("terminated-owned-process");
  expect(storage.close).toHaveBeenCalledTimes(1);
  expect(cleanup.close).toHaveBeenCalledTimes(1);
  expect(mocks.finishEvents).toHaveBeenCalledTimes(1);
  await expect(session.close()).resolves.toBe("terminated-owned-process");
  expect(storage.close).toHaveBeenCalledTimes(1);
});

it("preserves both cleanup failures and retries retained owners", async () => {
  const { cleanup, storage } = fixture();
  const detachFailure = new Error("CDP detach rejected");
  const transportFailure = new Error("Playwright transport close failed");
  storage.close
    .mockRejectedValueOnce(detachFailure)
    .mockResolvedValueOnce(undefined);
  cleanup.close
    .mockRejectedValueOnce(transportFailure)
    .mockImplementationOnce(async (finishEvents?: () => Promise<void>) => {
      await finishEvents?.();
    });
  const session = await PlaywrightScenarioSession.open(scenario, {}, {});

  let failure: unknown;
  try {
    await session.close();
  } catch (cause: unknown) {
    failure = cause;
  }
  expect(failure).toBeInstanceOf(AggregateError);
  expect((failure as AggregateError).errors).toEqual([
    detachFailure,
    transportFailure,
  ]);
  expect(storage.close).toHaveBeenCalledTimes(1);
  expect(cleanup.close).toHaveBeenCalledTimes(1);

  await expect(session.close()).resolves.toBe("terminated-owned-process");
  expect(storage.close).toHaveBeenCalledTimes(2);
  expect(cleanup.close).toHaveBeenCalledTimes(2);
});
