import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { NOTIFICATION_SOUND_MUTED_KEY, isSoundMuted, setSoundMuted } from "./notification-sound.ts";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
  };
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
