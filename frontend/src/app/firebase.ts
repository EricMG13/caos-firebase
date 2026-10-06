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

/** The cookie the server reads where a request cannot carry a header (the run stream). */
export function writeSessionCookie(token: string | null): void {
  document.cookie = token
    ? `${SESSION_COOKIE}=${token}; Path=/; Secure; SameSite=Strict; Max-Age=3600`
    : `${SESSION_COOKIE}=; Path=/; Secure; SameSite=Strict; Max-Age=0`;
}
