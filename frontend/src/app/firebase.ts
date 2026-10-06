import { initializeApp } from "firebase/app";
import { type Auth, indexedDBLocalPersistence, initializeAuth } from "firebase/auth";

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
};

// No popup or redirect resolver: email/password is plain HTTPS to Google, so no
// third-party script or frame loads and the policy keeps Trusted Types (D6).
export const auth: Auth | null =
  config.apiKey && config.projectId
    ? initializeAuth(initializeApp(config), { persistence: indexedDBLocalPersistence })
    : null;

export const SESSION_COOKIE = "__session";

/** The cookie the server reads where a request cannot carry a header (the run
    stream). `maxAge` is the token's own remaining seconds, so the cookie never
    outlives it. */
export function writeSessionCookie(token: string | null, maxAge = 3600): void {
  document.cookie = token
    ? `${SESSION_COOKIE}=${token}; Path=/; Secure; SameSite=Strict; Max-Age=${maxAge}`
    : `${SESSION_COOKIE}=; Path=/; Secure; SameSite=Strict; Max-Age=0`;
}
