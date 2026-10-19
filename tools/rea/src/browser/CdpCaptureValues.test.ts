import { afterEach, describe, expect, it, vi } from "vitest";

import { delayWithCancellation } from "./CdpCaptureValues.js";

describe("cancellable browser observation delays", () => {
  afterEach(() => vi.useRealTimers());

  it("schedules durations beyond the native timer range in consecutive waits", async () => {
    vi.useFakeTimers();
    let completed = false;
    const waiting = delayWithCancellation(
      2_147_483_648,
      "observe_javascript_runtime",
    ).then(() => {
      completed = true;
    });

    expect(vi.getTimerCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(2_147_483_647);
    expect(completed).toBe(false);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(1);
    await waiting;
    expect(completed).toBe(true);
  });
});
