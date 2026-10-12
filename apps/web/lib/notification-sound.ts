// Subtle audio chime that accompanies a desktop (OS-level) notification
// popup — see context/notifications-context.tsx's showBrowserNotification,
// which calls playNotificationChime() right after a popup is actually
// shown. Mirrors lib/role-cache.ts / lib/notifications-cache.ts: the
// mute-preference reader/writer is pure (storage passed in) so it has unit
// test coverage without a DOM; the actual Web Audio synthesis can't be
// unit tested (no AudioContext under Node) and isn't — same trade-off
// lib/browser-notifications.ts already makes for the real
// `new Notification(...)` call.

export interface SoundStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const NOTIFICATION_SOUND_MUTED_KEY = "pair.notifications.sound_muted";

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

// Lazily created and reused (not one AudioContext per chime) so that
// unlockNotificationSound() — called from the "Enable" button's click
// handler, a genuine user gesture — can resume it once and have that
// unlocked state carry over to later chimes triggered asynchronously by an
// SSE push, which is not itself a user gesture and so couldn't reliably
// start a suspended context on its own under strict autoplay policies.
let _audioCtx: AudioContext | null = null;

function _getAudioContext(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (_audioCtx) return _audioCtx;
  const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    _audioCtx = new Ctor();
    return _audioCtx;
  } catch {
    return null;
  }
}

/** Call from a real click handler (the desktop-notifications "Enable"
 * button) to resume the shared AudioContext while a user gesture is still
 * on the stack. Safe to call repeatedly; never throws. */
export function unlockNotificationSound(): void {
  const ctx = _getAudioContext();
  if (!ctx) return;
  if (ctx.state === "suspended") {
    ctx.resume().catch(() => {});
  }
}

/** Two quick, soft notes (not a harsh/attention-grabbing alert tone) — a
 * short attack/decay envelope on each keeps it "subtle" per the product
 * ask, rather than a sharp on/off click. No-op (silently) when muted,
 * unsupported, or anything about Web Audio fails — a sound effect must
 * never be able to break the notification flow it's decorating. */
export function playNotificationChime(storage: SoundStorage | null | undefined = typeof window !== "undefined" ? window.localStorage : null): void {
  if (isSoundMuted(storage)) return;
  const ctx = _getAudioContext();
  if (!ctx) return;
  try {
    if (ctx.state === "suspended") {
      ctx.resume().catch(() => {});
    }
    const now = ctx.currentTime;
    const notes: Array<{ freq: number; start: number; duration: number }> = [
      { freq: 880, start: 0, duration: 0.14 }, // A5
      { freq: 1318.51, start: 0.1, duration: 0.22 }, // E6
    ];
    for (const note of notes) {
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.type = "sine";
      oscillator.frequency.setValueAtTime(note.freq, now + note.start);
      // Gentle envelope: quick fade in, slower fade out, low peak volume —
      // "subtle chime," not a beep.
      gain.gain.setValueAtTime(0, now + note.start);
      gain.gain.linearRampToValueAtTime(0.12, now + note.start + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + note.start + note.duration);
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      oscillator.start(now + note.start);
      oscillator.stop(now + note.start + note.duration + 0.02);
    }
  } catch (err) {
    console.error("Failed to play notification chime:", err);
  }
}
