import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

type Held = { getIdTokenResult(): Promise<{ token: string; expirationTime: string }> };
type Listener = (user: Held | null) => void;
const mocks = vi.hoisted(() => ({
  listeners: [] as Listener[],
  signIn: vi.fn(),
}));
vi.mock("@firebase/auth", () => ({
  onIdTokenChanged: (_auth: unknown, cb: Listener) => {
    mocks.listeners.push(cb);
    return () => undefined;
  },
  signInWithEmailAndPassword: (...args: unknown[]) => mocks.signIn(...args),
  signOut: vi.fn(),
  initializeAuth: vi.fn(),
  indexedDBLocalPersistence: {},
}));

import { AuthGate } from "../../src/app/AuthGate";
import { SESSION_COOKIE, auth, writeSessionCookie } from "../../src/app/firebase";

const CLIENT = {} as never;

function held(token: string, expiresInMs: number): Held {
  return {
    getIdTokenResult: async () => ({
      token,
      expirationTime: new Date(Date.now() + expiresInMs + 500).toUTCString(),
    }),
  };
}

function cookieWrites() {
  return vi.spyOn(document, "cookie", "set");
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  mocks.signIn.mockReset();
});

describe("AuthGate", () => {
  it("has no client without Firebase env, and renders the workspace", () => {
    expect(auth).toBeNull();
    render(
      <AuthGate client={null}>
        <p>workspace</p>
      </AuthGate>,
    );
    expect(screen.getByText("workspace")).toBeInTheDocument();
  });

  it("says it is checking before the first callback", () => {
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Checking sign-in…");
  });

  it("asks for email and password when signed out, and signs in with them", async () => {
    mocks.signIn.mockResolvedValue(undefined);
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() => mocks.listeners.at(-1)!(null));
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "a@b.c" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(mocks.signIn).toHaveBeenCalledWith(CLIENT, "a@b.c", "pw"));
    expect(screen.queryByText("workspace")).toBeNull();
  });

  it("shows a generic line, never the provider's text, when sign-in fails", async () => {
    mocks.signIn.mockRejectedValue(new Error("auth/invalid-credential secret detail"));
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() => mocks.listeners.at(-1)!(null));
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "a@b.c" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in failed. Check the email and password.");
    expect(alert).not.toHaveTextContent("secret");
  });

  it("writes the session cookie and shows the workspace once a token arrives", async () => {
    const writes = cookieWrites();
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() => mocks.listeners.at(-1)!(held("tok", 1_800_000)));
    expect(await screen.findByText("workspace")).toBeInTheDocument();
    expect(writes.mock.calls[0]![0]).toMatch(
      /^__session=tok; Path=\/; Secure; SameSite=Strict; Max-Age=(1799|1800)$/,
    );
  });
});

describe("AuthGate session upkeep", () => {
  it("clears the cookie and asks again when the token cannot be read", async () => {
    const writes = cookieWrites();
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() =>
      mocks.listeners.at(-1)!({
        getIdTokenResult: () => Promise.reject(new Error("offline")),
      }),
    );
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(writes).toHaveBeenLastCalledWith(
      "__session=; Path=/; Secure; SameSite=Strict; Max-Age=0",
    );
  });

  it("clears the cookie when the user signs out", async () => {
    const writes = cookieWrites();
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() => mocks.listeners.at(-1)!(held("tok", 60_000)));
    await screen.findByText("workspace");
    act(() => mocks.listeners.at(-1)!(null));
    await screen.findByLabelText("Email");
    expect(writes).toHaveBeenLastCalledWith(
      "__session=; Path=/; Secure; SameSite=Strict; Max-Age=0",
    );
  });

  it("never lets a slower, older token overwrite a newer one", async () => {
    const writes = cookieWrites();
    let release: (value: { token: string; expirationTime: string }) => void = () => undefined;
    render(
      <AuthGate client={CLIENT}>
        <p>workspace</p>
      </AuthGate>,
    );
    act(() =>
      mocks.listeners.at(-1)!({
        getIdTokenResult: () => new Promise((resolve) => (release = resolve)),
      }),
    );
    act(() => mocks.listeners.at(-1)!(held("new", 60_000)));
    await screen.findByText("workspace");
    await act(async () => release({ token: "old", expirationTime: new Date().toUTCString() }));
    expect(writes).toHaveBeenCalledTimes(1);
    expect(writes.mock.calls[0]![0]).toContain("__session=new");
  });

  async function signedIn(getIdToken: ReturnType<typeof vi.fn>, expiresInMs: number) {
    vi.useFakeTimers();
    // A whole second, so the token's UTC expiry (no milliseconds) is exact.
    vi.setSystemTime(new Date("2026-10-06T00:00:00Z"));
    const client = { currentUser: { getIdToken } } as never;
    const view = render(
      <AuthGate client={client}>
        <p>workspace</p>
      </AuthGate>,
    );
    await act(async () => {
      mocks.listeners.at(-1)!(held("tok", expiresInMs));
      await vi.advanceTimersByTimeAsync(0);
    });
    return view;
  }

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("forces a refresh 120 s before a restored token expires", async () => {
    const getIdToken = vi.fn().mockResolvedValue("tok");
    await signedIn(getIdToken, 1_000_000);
    expect(screen.getByText("workspace")).toBeInTheDocument();
    await advance(879_999);
    expect(getIdToken).not.toHaveBeenCalled();
    await advance(1);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    expect(getIdToken).toHaveBeenCalledWith(true);
  });

  it("refreshes at once when 120 s or less are left", async () => {
    const getIdToken = vi.fn().mockResolvedValue("tok");
    await signedIn(getIdToken, 90_000);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    expect(getIdToken).toHaveBeenCalledWith(true);
  });

  it("retries a failed forced refresh 30 s later", async () => {
    const getIdToken = vi.fn().mockRejectedValue(new Error("offline"));
    await signedIn(getIdToken, 120_000);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    await advance(29_999);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(getIdToken).toHaveBeenCalledTimes(2);
  });

  it("forces at most one refresh per 30 s when every new token looks near expiry", async () => {
    // A client clock an hour fast: each fresh token seems to expire within 120 s.
    const getIdToken = vi.fn(async () => {
      mocks.listeners.at(-1)!(held("tok", 90_000));
      return "tok";
    });
    await signedIn(getIdToken, 90_000);
    await advance(1_000);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    await advance(28_999);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(getIdToken).toHaveBeenCalledTimes(2);
  });

  it("asks the SDK for a token when the tab returns", async () => {
    const getIdToken = vi.fn().mockResolvedValue("tok");
    await signedIn(getIdToken, 3_600_000);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(getIdToken).toHaveBeenCalledTimes(1);
    expect(getIdToken).toHaveBeenCalledWith();
  });

  it("stops refreshing once signed out", async () => {
    const getIdToken = vi.fn().mockResolvedValue("tok");
    await signedIn(getIdToken, 1_000_000);
    await act(async () => {
      mocks.listeners.at(-1)!(null);
      await vi.advanceTimersByTimeAsync(0);
    });
    await advance(3_600_000);
    expect(getIdToken).not.toHaveBeenCalled();
  });

  it("stops retrying once signed out while a refresh is failing", async () => {
    let fail: (reason: Error) => void = () => undefined;
    const getIdToken = vi.fn(() => new Promise((_, reject) => (fail = reject)));
    await signedIn(getIdToken, 60_000);
    expect(getIdToken).toHaveBeenCalledTimes(1);
    await act(async () => {
      mocks.listeners.at(-1)!(null);
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => fail(new Error("offline")));
    await advance(3_600_000);
    expect(getIdToken).toHaveBeenCalledTimes(1);
  });

  it("stops refreshing once unmounted", async () => {
    const getIdToken = vi.fn().mockResolvedValue("tok");
    const view = await signedIn(getIdToken, 1_000_000);
    view.unmount();
    await advance(3_600_000);
    expect(getIdToken).not.toHaveBeenCalled();
  });
});

describe("writeSessionCookie", () => {
  it("clears the cookie when there is no token", () => {
    const writes = cookieWrites();
    writeSessionCookie(null);
    expect(SESSION_COOKIE).toBe("__session");
    expect(writes).toHaveBeenCalledWith("__session=; Path=/; Secure; SameSite=Strict; Max-Age=0");
  });
});
