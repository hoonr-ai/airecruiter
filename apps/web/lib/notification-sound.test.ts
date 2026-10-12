import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  NOTIFICATION_SOUND_MUTED_KEY,
  isSoundMuted,
  setSoundMuted,
  playNotificationChime,
  unlockNotificationSound,
  _resetChimeCooldownForTests,
  type AudioContextLike,
} from "./notification-sound.ts";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

/** A plain-object stand-in for a real AudioContext — records every call so
 * tests can assert on it without a browser. */
function fakeAudioContext(state: string = "running") {
  const calls = {
    createOscillator: 0,
    createGain: 0,
    oscillatorStart: 0,
    oscillatorStop: 0,
    resume: 0,
  };
  const node = () => ({
    gain: {
      setValueAtTime: () => {},
      linearRampToValueAtTime: () => {},
      exponentialRampToValueAtTime: () => {},
    },
    frequency: { setValueAtTime: () => {} },
    connect: () => {},
    start: () => {
      calls.oscillatorStart += 1;
    },
    stop: () => {
      calls.oscillatorStop += 1;
    },
    type: "sine",
  });
  const ctx: AudioContextLike = {
    state,
    currentTime: 0,
    destination: {},
    resume: () => {
      calls.resume += 1;
      ctx.state = "running";
      return Promise.resolve();
    },
    createOscillator: () => {
      calls.createOscillator += 1;
      return node() as unknown as ReturnType<AudioContextLike["createOscillator"]>;
    },
    createGain: () => {
      calls.createGain += 1;
      return node() as unknown as ReturnType<AudioContextLike["createGain"]>;
    },
  };
  return { ctx, calls };
}

describe("isSoundMuted", () => {
  it("defaults to false (chime on) with no stored value", () => {
    assert.equal(isSoundMuted(memoryStorage()), false);
  });

  it("defaults to false with no storage at all (SSR, blocked storage)", () => {
    assert.equal(isSoundMuted(null), false);
    assert.equal(isSoundMuted(undefined), false);
  });

  it("is true only when the stored value is exactly the string 'true'", () => {
    const storage = memoryStorage();
    storage.setItem(NOTIFICATION_SOUND_MUTED_KEY, "true");
    assert.equal(isSoundMuted(storage), true);
  });

  it("treats anything other than 'true' as unmuted — fails open to sound on", () => {
    const storage = memoryStorage();
    storage.setItem(NOTIFICATION_SOUND_MUTED_KEY, "false");
    assert.equal(isSoundMuted(storage), false);

    storage.setItem(NOTIFICATION_SOUND_MUTED_KEY, "garbage");
    assert.equal(isSoundMuted(storage), false);
  });

  it("never throws when storage.getItem throws", () => {
    const angry = {
      getItem: () => {
        throw new Error("blocked");
      },
    };
    assert.equal(isSoundMuted(angry), false);
  });
});

describe("setSoundMuted", () => {
  it("round-trips true and false through isSoundMuted", () => {
    const storage = memoryStorage();
    setSoundMuted(storage, true);
    assert.equal(isSoundMuted(storage), true);

    setSoundMuted(storage, false);
    assert.equal(isSoundMuted(storage), false);
  });

  it("never throws when storage.setItem throws (quota, private browsing)", () => {
    const angry = {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota exceeded");
      },
    };
    assert.doesNotThrow(() => setSoundMuted(angry, true));
  });

  it("is a no-op with no storage at all", () => {
    assert.doesNotThrow(() => setSoundMuted(null, true));
    assert.doesNotThrow(() => setSoundMuted(undefined, true));
  });
});

describe("playNotificationChime", () => {
  it("does nothing when muted — never even touches the AudioContext", () => {
    _resetChimeCooldownForTests();
    const storage = memoryStorage();
    setSoundMuted(storage, true);
    let factoryCalls = 0;
    playNotificationChime(storage, () => {
      factoryCalls += 1;
      return fakeAudioContext().ctx;
    });
    assert.equal(factoryCalls, 0);
  });

  it("plays two notes when unmuted", () => {
    _resetChimeCooldownForTests();
    const { ctx, calls } = fakeAudioContext();
    playNotificationChime(memoryStorage(), () => ctx);
    assert.equal(calls.createOscillator, 2);
    assert.equal(calls.createGain, 2);
    assert.equal(calls.oscillatorStart, 2);
    assert.equal(calls.oscillatorStop, 2);
  });

  it("resumes a suspended context before playing", () => {
    _resetChimeCooldownForTests();
    const { ctx, calls } = fakeAudioContext("suspended");
    playNotificationChime(memoryStorage(), () => ctx);
    assert.equal(calls.resume, 1);
  });

  it("respects the cooldown — a second call right after the first plays nothing", () => {
    _resetChimeCooldownForTests();
    const { ctx, calls } = fakeAudioContext();
    let now = 1_000;
    playNotificationChime(memoryStorage(), () => ctx, () => now);
    assert.equal(calls.createOscillator, 2);

    now += 200; // well inside the 1500ms cooldown
    playNotificationChime(memoryStorage(), () => ctx, () => now);
    assert.equal(calls.createOscillator, 2, "second call within the cooldown must not play again");

    now += 2000; // now outside the cooldown
    playNotificationChime(memoryStorage(), () => ctx, () => now);
    assert.equal(calls.createOscillator, 4, "a call after the cooldown elapses plays again");
  });

  it("does not throw when the AudioContext factory throws", () => {
    _resetChimeCooldownForTests();
    assert.doesNotThrow(() =>
      playNotificationChime(memoryStorage(), () => {
        throw new Error("no AudioContext in this browser");
      })
    );
  });

  it("does not throw when a node call on the context throws", () => {
    _resetChimeCooldownForTests();
    const { ctx } = fakeAudioContext();
    ctx.createOscillator = () => {
      throw new Error("boom");
    };
    assert.doesNotThrow(() => playNotificationChime(memoryStorage(), () => ctx));
  });

  it("does nothing when no AudioContext is available at all", () => {
    _resetChimeCooldownForTests();
    let factoryCalls = 0;
    assert.doesNotThrow(() =>
      playNotificationChime(memoryStorage(), () => {
        factoryCalls += 1;
        return null;
      })
    );
    assert.equal(factoryCalls, 1);
  });
});

describe("unlockNotificationSound", () => {
  it("resumes a suspended context", () => {
    const { ctx, calls } = fakeAudioContext("suspended");
    unlockNotificationSound(() => ctx);
    assert.equal(calls.resume, 1);
  });

  it("does nothing to an already-running context", () => {
    const { ctx, calls } = fakeAudioContext("running");
    unlockNotificationSound(() => ctx);
    assert.equal(calls.resume, 0);
  });

  it("never throws when no AudioContext is available", () => {
    assert.doesNotThrow(() => unlockNotificationSound(() => null));
  });
});
