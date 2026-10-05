import { afterEach, describe, expect, it, vi } from "vitest";
import { apiPatch, apiPollGet } from "./api";

// Goal 17: a poll that raced a write must not hand back (possibly) pre-write state.

type Resolver = (body: unknown) => void;

function deferredFetch() {
  const pending: Resolver[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          pending.push((body) =>
            resolve(new Response(JSON.stringify(body), { status: 200 })),
          );
        }),
    ),
  );
  return pending;
}

const tick = () => new Promise((r) => setTimeout(r, 0));

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiPollGet", () => {
  it("returns data when no write overlaps the poll", async () => {
    const pending = deferredFetch();
    const poll = apiPollGet<{ ok: number }>("/tasks");
    await tick();
    pending[0]({ ok: 1 });
    expect(await poll).toEqual({ ok: 1 });
  });

  it("drops the result when a write starts while the poll is out", async () => {
    const pending = deferredFetch();
    const poll = apiPollGet("/tasks");
    await tick();
    const write = apiPatch("/tasks/L/T", { status: "completed" });
    await tick();
    pending[1]({});
    await write;
    pending[0]({ stale: true });
    expect(await poll).toBeNull();
  });

  it("drops the result when a write was already in flight", async () => {
    const pending = deferredFetch();
    const write = apiPatch("/tasks/L/T", { status: "completed" });
    await tick();
    const poll = apiPollGet("/tasks");
    await tick();
    pending[1]({ stale: true });
    expect(await poll).toBeNull();
    pending[0]({});
    await write;
  });
});
