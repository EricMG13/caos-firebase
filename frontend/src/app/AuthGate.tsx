import { onIdTokenChanged, signInWithEmailAndPassword, type Auth } from "firebase/auth";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { auth, writeSessionCookie } from "./firebase";

type Gate = "checking" | "signed-out" | "signed-in";

/** Children when no project is configured or a token is held; else the sign-in form. */
export function AuthGate({
  children,
  client = auth,
}: {
  children: ReactNode;
  client?: Auth | null;
}) {
  const [gate, setGate] = useState<Gate>(client ? "checking" : "signed-in");
  useEffect(() => {
    if (!client) return undefined;
    return onIdTokenChanged(client, (user) => {
      void (user ? user.getIdToken() : Promise.resolve(null)).then((token) => {
        writeSessionCookie(token);
        setGate(token ? "signed-in" : "signed-out");
      });
    });
  }, [client]);
  if (gate === "signed-in") return <>{children}</>;
  if (gate === "checking" || !client) return <p role="status">Checking sign-in…</p>;
  return <SignIn client={client} />;
}

function SignIn({ client }: { client: Auth }) {
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setFailed(false);
    signInWithEmailAndPassword(client, String(form.get("email")), String(form.get("password")))
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  }
  return (
    <main className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-4 p-6">
      <h1 className="text-lg font-semibold">Sign in to CAOS</h1>
      <form className="flex flex-col gap-3" onSubmit={submit}>
        <label className="flex flex-col gap-1 text-sm font-medium" htmlFor="email">
          Email
          <Input id="email" name="email" type="email" autoComplete="username" required />
        </label>
        <label className="flex flex-col gap-1 text-sm font-medium" htmlFor="password">
          Password
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
          />
        </label>
        {failed ? (
          <p role="alert" className="text-sm text-destructive">
            Sign-in failed. Check the email and password.
          </p>
        ) : null}
        <Button type="submit" aria-busy={busy || undefined}>
          Sign in
        </Button>
      </form>
    </main>
  );
}
