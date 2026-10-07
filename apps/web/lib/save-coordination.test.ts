import { describe, it } from 'node:test';
import assert from 'node:assert';
import { shouldSkipForInFlightManualSave, isSupersededAbort } from './save-coordination.ts';

describe('shouldSkipForInFlightManualSave', () => {
  it('skips an incoming auto-save when a manual save is in flight', () => {
    assert.strictEqual(
      shouldSkipForInFlightManualSave({ inFlightIsAuto: false, incomingIsAuto: true }),
      true
    );
  });

  it('does not skip an incoming manual save, even if a manual save is in flight', () => {
    assert.strictEqual(
      shouldSkipForInFlightManualSave({ inFlightIsAuto: false, incomingIsAuto: false }),
      false
    );
  });

  it('does not skip when the in-flight save is itself an auto-save', () => {
    assert.strictEqual(
      shouldSkipForInFlightManualSave({ inFlightIsAuto: true, incomingIsAuto: true }),
      false
    );
    assert.strictEqual(
      shouldSkipForInFlightManualSave({ inFlightIsAuto: true, incomingIsAuto: false }),
      false
    );
  });
});

describe('isSupersededAbort', () => {
  it('is true only for an AbortError whose reason is "superseded"', () => {
    assert.strictEqual(isSupersededAbort(true, "superseded"), true);
  });

  it('is false for a timeout abort', () => {
    assert.strictEqual(isSupersededAbort(true, "timeout"), false);
  });

  it('is false when the error was not an abort at all', () => {
    assert.strictEqual(isSupersededAbort(false, "superseded"), false);
  });
});
