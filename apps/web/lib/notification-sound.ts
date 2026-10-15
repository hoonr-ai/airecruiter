// Subtle audio chime that accompanies a desktop (OS-level) notification
// popup — see context/notifications-context.tsx's showBrowserNotification,
// which calls playNotificationChime() right after a popup is actually
// shown. Mirrors lib/role-cache.ts / lib/notifications-cache.ts: the
// mute-preference reader/writer is pure (storage passed in) so it has unit
// test coverage without a DOM. The Web Audio synthesis itself is also
// testable here (unlike lib/browser-notifications.ts's real
// `new Notification(...)` call) by injecting a fake AudioContext factory —
// see the `factory` parameters below and notification-sound.test.ts.

export interface SoundStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

// Versioned like the other pair.* keys (lib/notifications-cache.ts's
// NOTIFICATIONS_CACHE_KEY, lib/role-cache.ts's ROLE_CACHE_KEY) and a
// distinct name from NOTIFICATIONS_CACHE_KEY ("pair.notifications.v1") so
// the two are never confused for related/nested data.
export const NOTIFICATION_SOUND_MUTED_KEY = "pair.notificationSoundMuted.v1";

/** `window.localStorage` wrapped in a try/catch: merely EVALUATING that
 * property (not just calling getItem/setItem on it) throws a SecurityError
 * in some environments — blocked third-party storage, certain privacy
 * modes, sandboxed iframes, disabled cookies. Every call site in this file
 * and in app/notifications/page.tsx goes through this instead of touching
 * `window.localStorage` directly.
 *
 * Structurally identical to lib/safe-local-storage.ts's getSafeLocalStorage
 * (used by lib/desktop-notification-preferences.ts) — kept as its own copy
 * here rather than importing that module, because this file's value-level
 * import would need an explicit `.ts` specifier for `node --test` to
 * resolve it directly, which this project's tsconfig (moduleResolution:
 * "bundler", no allowImportingTsExtensions) doesn't permit in application
 * source. Not worth a project-wide tsconfig change for a 6-line function. */
export function getSafeStorage(): SoundStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** False (chime on) by default — the toggle is opt-out, not opt-in, since
 * the chime only ever plays alongside a desktop popup the user already
 * explicitly enabled. Anything other than the literal "true" string reads
 * as unmuted, so a cleared/corrupted value fails open to "on" rather than
 * silently muting someone who never asked for that. */
export function isSoundMuted(storage: SoundStorage | null | undefined): boolean {
  if (!storage) return false;
  try {
    return storage.getItem(NOTIFICATION_SOUND_MUTED_KEY) === "true";
  } catch {
    return false;
  }
}

export function setSoundMuted(storage: SoundStorage | null | undefined, muted: boolean): void {
  if (!storage) return;
  try {
    storage.setItem(NOTIFICATION_SOUND_MUTED_KEY, muted ? "true" : "false");
  } catch {
    // storage full or blocked (private browsing, quota): best-effort only
  }
}

// The minimal surface playNotificationChime/unlockNotificationSound need
// from an AudioContext — lets a test inject a plain-object fake instead of
// needing a real one (unavailable under Node).
export interface AudioContextLike {
  state: string;
  currentTime: number;
  destination: unknown;
  resume: () => Promise<void>;
  createOscillator: () => {
    type: string;
    frequency: { setValueAtTime: (value: number, time: number) => void };
    connect: (dest: unknown) => void;
    start: (time: number) => void;
    stop: (time: number) => void;
  };
  createGain: () => {
    gain: {
      setValueAtTime: (value: number, time: number) => void;
      linearRampToValueAtTime: (value: number, time: number) => void;
      exponentialRampToValueAtTime: (value: number, time: number) => void;
    };
    connect: (dest: unknown) => void;
  };
}

export type AudioContextFactory = () => AudioContextLike | null;

function _defaultAudioContextFactory(): AudioContextLike | null {
  if (typeof window === "undefined") return null;
  const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    return new Ctor() as unknown as AudioContextLike;
  } catch {
    return null;
  }
}

// Lazily created and reused (not one AudioContext per chime) so that
// unlockNotificationSound() — called from a real user gesture — can resume
// it once and have that unlocked state carry over to later chimes
// triggered asynchronously by an SSE push, which is not itself a user
// gesture and so couldn't reliably start a suspended context on its own
// under strict autoplay policies. Only the real default factory's result is
// cached: an explicitly-passed `factory` (tests only) always runs fresh, so
// tests never share state through this module-level singleton.
let _audioCtx: AudioContextLike | null = null;

function _getAudioContext(factory?: AudioContextFactory): AudioContextLike | null {
  if (factory) {
    try {
      return factory();
    } catch {
      return null;
    }
  }
  if (_audioCtx) return _audioCtx;
  const ctx = _defaultAudioContextFactory();
  if (ctx) _audioCtx = ctx;
  return _audioCtx;
}

/** Call from a real user gesture to resume the shared AudioContext while
 * that gesture is still on the stack:
 *   - the desktop-notifications "Enable" button's click (a first-time
 *     grant);
 *   - a one-time pointerdown/keydown on the page when permission was
 *     already granted in an earlier session (see
 *     context/notifications-context.tsx) — that recruiter never clicks
 *     "Enable" this session, so without this they'd never unlock audio at
 *     all and the chime would silently never play;
 *   - unmuting via the page's toggle.
 * Safe to call repeatedly; never throws. */
export function unlockNotificationSound(factory?: AudioContextFactory): void {
  const ctx = _getAudioContext(factory);
  if (!ctx) return;
  if (ctx.state === "suspended") {
    ctx.resume().catch(() => {});
  }
}

// Several notifications can arrive in quick succession (e.g. a batch
// reconnect replaying history); without a cooldown each one's chime
// overlaps the last, turning "subtle" into noise.
const CHIME_COOLDOWN_MS = 1500;
let _lastChimeAtMs = 0;

/** Test-only: clears the cooldown timestamp so tests don't have to wait out
 * a real 1.5s window or depend on execution order. */
export function _resetChimeCooldownForTests(): void {
  _lastChimeAtMs = -Infinity;
}

/** Two quick, soft notes (not a harsh/attention-grabbing alert tone) — a
 * short attack/decay envelope on each keeps it "subtle" per the product
 * ask, rather than a sharp on/off click. No-op (silently) when muted,
 * cooling down, unsupported, or anything about Web Audio fails — a sound
 * effect must never be able to break the notification flow it's
 * decorating. `factory` and `now` are test-only injection points; real call
 * sites never pass them. */
export function playNotificationChime(
  storage: SoundStorage | null | undefined = getSafeStorage(),
  factory?: AudioContextFactory,
  now: () => number = Date.now
): void {
  if (isSoundMuted(storage)) return;
  const nowMs = now();
  if (nowMs - _lastChimeAtMs < CHIME_COOLDOWN_MS) return;
  const ctx = _getAudioContext(factory);
  if (!ctx) return;
  try {
    if (ctx.state === "suspended") {
      ctx.resume().catch(() => {});
    }
    _lastChimeAtMs = nowMs;
    const start = ctx.currentTime;
    const notes: Array<{ freq: number; offset: number; duration: number }> = [
      { freq: 880, offset: 0, duration: 0.14 }, // A5
      { freq: 1318.51, offset: 0.1, duration: 0.22 }, // E6
    ];
    for (const note of notes) {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(note.freq, start + note.offset);
      // Gentle envelope: quick fade in, slower fade out, low peak volume —
      // "subtle chime," not a beep.
      gain.gain.setValueAtTime(0, start + note.offset);
      gain.gain.linearRampToValueAtTime(0.12, start + note.offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + note.offset + note.duration);
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      oscillator.start(start + note.offset);
      oscillator.stop(start + note.offset + note.duration + 0.02);
    }
  } catch (err) {
    console.error("Failed to play notification chime:", err);
  }
}
