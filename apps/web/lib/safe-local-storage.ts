// Shared `window.localStorage` accessor for every per-browser preference
// flag in this app (notification-sound.ts's mute toggle,
// desktop-notification-preferences.ts's per-type opt-outs, ...) — PR #811
// review follow-up: this used to be duplicated per-module.
//
// Wrapped in a try/catch because merely EVALUATING `window.localStorage`
// (not just calling getItem/setItem on it) throws a SecurityError in some
// environments — blocked third-party storage, certain privacy modes,
// sandboxed iframes, disabled cookies.

export interface SafeStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export function getSafeLocalStorage(): SafeStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}
