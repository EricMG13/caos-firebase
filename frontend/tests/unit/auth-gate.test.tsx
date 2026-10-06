import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

type Listener = (user: { getIdToken(): Promise<string> } | null) => void;
const mocks = vi.hoisted(() => ({
  listeners: [] as Listener[],
  signIn: vi.fn(),
}));
vi.mock("firebase/auth", () => ({
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

function cookieWrites() {
  return vi.spyOn(document, "cookie", "set");
}

afterEach(() => {
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
    act(() => mocks.listeners.at(-1)!({ getIdToken: async () => "tok" }));
    expect(await screen.findByText("workspace")).toBeInTheDocument();
    expect(writes).toHaveBeenCalledWith(
      "__session=tok; Path=/; Secure; SameSite=Strict; Max-Age=3600",
    );
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
