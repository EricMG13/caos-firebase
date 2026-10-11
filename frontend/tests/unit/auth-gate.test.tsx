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

  it("asks the SDK for a token every five minutes and when the tab returns", async () => {
    vi.useFakeTimers();
    const getIdToken = vi.fn().mockResolvedValue("tok");
    const client = { currentUser: { getIdToken } } as never;
    render(
      <AuthGate client={client}>
        <p>workspace</p>
      </AuthGate>,
    );
    await act(async () => {
      mocks.listeners.at(-1)!(held("tok", 3_600_000));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("workspace")).toBeInTheDocument();
    expect(getIdToken).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    expect(getIdToken).toHaveBeenCalledTimes(1);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(getIdToken).toHaveBeenCalledTimes(2);
  });

  it("stops refreshing once signed out", async () => {
    vi.useFakeTimers();
    const getIdToken = vi.fn().mockResolvedValue("tok");
    const client = { currentUser: { getIdToken } } as never;
    render(
      <AuthGate client={client}>
        <p>workspace</p>
      </AuthGate>,
    );
    await act(async () => {
      mocks.listeners.at(-1)!(held("tok", 3_600_000));
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      mocks.listeners.at(-1)!(null);
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
    });
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
