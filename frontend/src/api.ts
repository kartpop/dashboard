export const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8010";

// Session cookie flows on every request (goal 8). In dev the frontend (:5173) and
// backend (:8010) are the same site (localhost), so the SameSite=Lax cookie is sent;
// in prod they share an origin. `credentials: "include"` is required for both.
const CREDENTIALS: RequestCredentials = "include";

// A 401 anywhere means the session lapsed — the AuthProvider registers a handler here
// to flip the whole app back to the sign-in screen instead of each panel guessing.
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(response: Response, path: string): Promise<T> {
  if (!response.ok) {
    if (response.status === 401) unauthorizedHandler?.();
    const body = (await response.json().catch(() => null)) as {
      error?: { message?: string };
    } | null;
    throw new HttpError(
      response.status,
      body?.error?.message ?? `${path} failed with status ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

// Write tracking (goal 17). A background poll that raced a write can hand back the
// pre-write state and briefly undo an optimistic change (a completed task popping
// back). Polls go through `apiPollGet`, which drops its result if any write was in
// flight when it started or ran while it was out.
let writesInFlight = 0;
let writeGen = 0;

function tracked<T>(request: Promise<T>): Promise<T> {
  writesInFlight += 1;
  writeGen += 1;
  return request.finally(() => {
    writesInFlight -= 1;
    writeGen += 1;
  });
}

/** A GET for background polling: resolves to null when its answer may be stale. */
export async function apiPollGet<T>(path: string): Promise<T | null> {
  const startGen = writesInFlight > 0 ? null : writeGen;
  const data = await apiGet<T>(path);
  return startGen !== null && startGen === writeGen && writesInFlight === 0
    ? data
    : null;
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(
    await fetch(`${API_BASE_URL}${path}`, { credentials: CREDENTIALS }),
    path,
  );
}

async function send<T>(method: string, path: string, body?: unknown): Promise<T> {
  return handle<T>(
    await fetch(`${API_BASE_URL}${path}`, {
      method,
      ...(body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }),
      credentials: CREDENTIALS,
    }),
    path,
  );
}

export function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return tracked(send<T>("PATCH", path, body));
}

export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return tracked(send<T>("POST", path, body));
}

export function apiPut<T>(path: string, body: unknown): Promise<T> {
  return tracked(send<T>("PUT", path, body));
}

export function apiDelete<T>(path: string): Promise<T> {
  return tracked(send<T>("DELETE", path));
}
