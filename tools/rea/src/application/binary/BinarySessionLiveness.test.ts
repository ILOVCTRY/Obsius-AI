import { describe, expect, it } from "vitest";

import {
  createAnalysisExecution,
  type AnalysisClient,
  type AnalysisOperation,
  type AnalysisProvider,
  type AnalysisProviderCandidate,
  type CapabilityDescriptor,
  type ProviderAvailability,
  type ProviderIdentity,
  type ProviderTargetSupport,
} from "../AnalysisProvider.js";
import { createAnalysisProfile } from "../../domain/analysisProfile.js";
import { ProviderAdapterError } from "../../domain/providerAdapterError.js";
import type { ProviderOperationHealth } from "../../domain/providerOperationHealth.js";
import { err, ok } from "../../domain/result.js";
import { InvestigationRecords } from "../investigation/InvestigationRecords.js";
import { AnalysisProviderRegistry } from "./AnalysisProviderRegistry.js";
import { BinarySession } from "./BinarySession.js";
import { SessionProviderRouter } from "./SessionProviderRouter.js";

/**
 * Regression coverage for https://github.com/morluto/rea/issues/1719:
 * `BinarySession#open` must not short-circuit on the `sameTarget &&
 * sameProfile` fast path when the retained provider client is dead. It must
 * start a fresh client instead.
 *
 * The fixtures are production-composed: a deep provider plus an auxiliary
 * provider, so `SessionProviderRouter` wraps them in a real
 * `CompositeProvider` exactly like `src/composition/binary.ts` does for the
 * CLI/MCP runtime. The composite's `health` answers unconditionally ok
 * without touching a child, so a probe that only calls `health` cannot see a
 * dead child — the liveness signal must reach the actual child through
 * `operationHealthSnapshot`, the way `HopperClient.operationHealth` reports a
 * recorded process exit.
 */
describe("BinarySession open liveness", () => {
  it("reuses the retained client while the child stays live", async () => {
    const { session, deep } = openSession();
    expect((await session.open(process.execPath)).ok).toBe(true);
    // Instantiate the deep child inside the composite, like a real query would.
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);

    const second = await session.open(process.execPath);
    expect(second.ok).toBe(true);
    // Still the same composite and child: nothing was retired or recreated.
    expect(deep.created).toHaveLength(1);
    expect(deep.created[0]?.closeCalled).toBe(false);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);
  });

  it("starts a fresh client when the retained child died", async () => {
    const { session, deep } = openSession();
    expect((await session.open(process.execPath)).ok).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);

    // Kill the provider process out from under the session, then re-open
    // the same target: the session must not short-circuit on the dead
    // child — it must retire it and start a real one.
    const dead = deep.created[0];
    dead?.kill();

    const second = await session.open(process.execPath);
    expect(second.ok).toBe(true);
    // The dead binding was retired, not retained.
    expect(dead?.closeCalled).toBe(true);
    // The next query runs against a freshly created child, not the dead one.
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(2);
  });

  it("treats an uncharacterized operation failure as dead", async () => {
    // A recorded failure the provider could not characterize ("unknown",
    // e.g. a Hopper spawn failure with no observed exit code) is never
    // evidence of health: the gate fails closed.
    const { session, deep } = openSession();
    expect((await session.open(process.execPath)).ok).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);

    const dead = deep.created[0];
    dead?.kill("unknown");

    const second = await session.open(process.execPath);
    expect(second.ok).toBe(true);
    expect(dead?.closeCalled).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(2);
  });

  it("retires a retained client whose startup never completed", async () => {
    // A Hopper startup timeout leaves the operation-health snapshot in
    // "not_started" while the retained resources block the next start()
    // until close() retries cleanup. Classifying that state as live would return success
    // without entering the retire path, so subsequent analysis still hits
    // the wedged client.
    const { session, deep } = openSession();
    expect((await session.open(process.execPath)).ok).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);

    const wedged = deep.created[0];
    wedged?.kill("not_started");

    const second = await session.open(process.execPath);
    expect(second.ok).toBe(true);
    // The wedged binding was retired (close ran, so cleanup is retried),
    // not reused.
    expect(wedged?.closeCalled).toBe(true);
    // The next query runs against a freshly created child, not the wedged one.
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(2);
  });

  it("falls back to the health probe when the child reports no operation health", async () => {
    // Without an operation-health hook there is no child-liveness signal,
    // so the probe keeps the previous `health` admission behavior.
    const { session, deep } = openSession({ operationHealth: false });
    expect((await session.open(process.execPath)).ok).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);
    expect(deep.created).toHaveLength(1);

    const second = await session.open(process.execPath);
    expect(second.ok).toBe(true);
    // The composite's `health` answers ok, so the live client is reused.
    expect(deep.created).toHaveLength(1);
    expect(deep.created[0]?.closeCalled).toBe(false);
  });

  it("reports cancellation without touching the session when aborted mid-probe", async () => {
    const { session, deep, onProbe } = openSession();
    expect((await session.open(process.execPath)).ok).toBe(true);
    expect((await session.execute("address_name", {})).ok).toBe(true);

    const controller = new AbortController();
    // Fire the abort while the fast-path liveness probe is in flight: the
    // hook runs synchronously inside the snapshot read, so the abort lands
    // after the probe started but before the fast-path decision completes.
    let fired = false;
    onProbe(() => {
      if (!fired) {
        fired = true;
        controller.abort();
      }
    });

    const cancelled = await session.open(process.execPath, {
      signal: controller.signal,
    });
    expect(cancelled).toMatchObject({
      ok: false,
      error: { _tag: "AnalysisCancelledError" },
    });
    // The dead-or-live question is moot: the session must be untouched.
    expect(deep.created).toHaveLength(1);
    expect(deep.created[0]?.closeCalled).toBe(false);
    const reopened = await session.open(process.execPath);
    expect(reopened.ok).toBe(true);
    expect(deep.created).toHaveLength(1);
  });
});

interface KillableChild extends AnalysisClient {
  kill(state?: "exited" | "unreachable" | "unknown" | "not_started"): void;
  closeCalled: boolean;
}

const deepIdentity: ProviderIdentity = {
  id: "liveness-deep",
  name: "Liveness deep provider",
  version: "1",
};

const auxIdentity: ProviderIdentity = {
  id: "liveness-aux",
  name: "Liveness auxiliary provider",
  version: "1",
};

const capability = (
  identity: ProviderIdentity,
  operation: Exclude<AnalysisOperation, "health">,
): CapabilityDescriptor => ({
  provider: identity,
  operation,
  available: true,
  reason: null,
  effects: {
    mutatesArtifact: false,
    launchesProcess: false,
    mayShowUi: false,
    mayAccessNetwork: false,
    mayWriteFilesystem: false,
    changesPermissions: false,
    requiresRoot: false,
  },
  limitations: [],
});

const openSession = (
  options: { operationHealth?: boolean } = {},
): {
  session: BinarySession;
  deep: { created: KillableChild[] };
  onProbe: (hook: () => void) => void;
} => {
  const { provider, created, onProbe } = killableDeepProvider(options);
  // Mirror the production composition: a deep provider plus auxiliary
  // providers, so the route wraps them in a real CompositeProvider (whose
  // trivially-ok `health` must not mask a dead child).
  const aux: AnalysisProvider = {
    identity: () => auxIdentity,
    capabilities: () => [],
    createClient: (): AnalysisClient => ({
      execute: (operation) =>
        Promise.resolve(
          ok(createAnalysisExecution(`aux:${operation}`, auxIdentity)),
        ),
      close: () => Promise.resolve(ok(null)),
    }),
  };
  const session = new BinarySession(
    SessionProviderRouter.selectable(new AnalysisProviderRegistry([provider]), [
      aux,
    ]),
    new InvestigationRecords(),
  );
  return { session, deep: { created }, onProbe };
};

const killableDeepProvider = (options: {
  operationHealth?: boolean;
}): {
  provider: AnalysisProviderCandidate;
  created: KillableChild[];
  onProbe: (hook: () => void) => void;
} => {
  const created: KillableChild[] = [];
  let probeHook: (() => void) | undefined;
  const withOperationHealth = options.operationHealth !== false;
  const provider: AnalysisProviderCandidate = {
    identity: () => deepIdentity,
    capabilities: () => [capability(deepIdentity, "address_name")],
    inspectAvailability: (): ProviderAvailability => ({
      status: "available",
      code: null,
      reason: null,
      diagnostics: {},
    }),
    inspectTargetSupport: (): ProviderTargetSupport => ({
      status: "supported",
      code: null,
      reason: null,
      diagnostics: {},
    }),
    resolveAnalysisProfile: () =>
      Promise.resolve(
        ok({
          profile: createAnalysisProfile(
            { id: deepIdentity.id, name: deepIdentity.name, version: "1" },
            { fixture: "liveness" },
          ),
        }),
      ),
    createClient: (): KillableChild => {
      let alive = true;
      let closeCalled = false;
      let deadState: "exited" | "unreachable" | "unknown" | "not_started" =
        "exited";
      const client: KillableChild = {
        execute: (operation) => {
          return alive
            ? Promise.resolve(
                ok(
                  createAnalysisExecution(
                    operation === "health" ? null : `liveness:${operation}`,
                    deepIdentity,
                  ),
                ),
              )
            : Promise.resolve(
                err(
                  new ProviderAdapterError(deepIdentity.id, operation, {
                    diagnostics: { reason: "process_killed" },
                  }),
                ),
              );
        },
        // Genuine child-liveness signal, like HopperClient.operationHealth:
        // a recorded process exit stays visible after the kill.
        ...(withOperationHealth
          ? {
              operationHealthSnapshot: (): ProviderOperationHealth => {
                probeHook?.();
                return {
                  state: alive ? "idle" : deadState,
                  stage: alive ? null : "connection",
                  retryAction: alive ? null : "restart_provider",
                  exitCode: alive ? null : 1,
                  requests: [],
                };
              },
            }
          : {}),
        close: () => {
          closeCalled = true;
          return Promise.resolve(ok(null));
        },
        get closeCalled() {
          return closeCalled;
        },
        kill: (
          state:
            | "exited"
            | "unreachable"
            | "unknown"
            | "not_started" = "exited",
        ) => {
          alive = false;
          deadState = state;
        },
      };
      created.push(client);
      return client;
    },
  };
  return {
    provider,
    created,
    onProbe: (hook: () => void) => {
      probeHook = hook;
    },
  };
};
