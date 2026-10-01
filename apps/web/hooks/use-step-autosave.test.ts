import { describe, it } from 'node:test';
import assert from 'node:assert';
import { haveDepsChanged, shouldMarkDirty } from './use-step-autosave.ts';

describe('haveDepsChanged', () => {
    it('returns false when every dependency is referentially equal', () => {
        const a = { id: 1 };
        assert.strictEqual(haveDepsChanged([a, 'x', 1], [a, 'x', 1]), false);
    });

    it('returns true when any dependency differs', () => {
        assert.strictEqual(haveDepsChanged(['x', 1], ['x', 2]), true);
        assert.strictEqual(haveDepsChanged([{ id: 1 }], [{ id: 1 }]), true); // new object reference
    });

    it('returns true when a dependency is added (undefined -> value)', () => {
        assert.strictEqual(haveDepsChanged(['a', 'b'], ['a']), true);
    });
});

describe('shouldMarkDirty', () => {
    const base = { currentStep: 2, targetStep: 2, isReadOnly: false, isHydrated: true, depsChanged: true };

    it('marks dirty only when on the target step, editable, hydrated and changed', () => {
        assert.strictEqual(shouldMarkDirty(base), true);
    });

    it('does not mark dirty on a different step', () => {
        assert.strictEqual(shouldMarkDirty({ ...base, currentStep: 3 }), false);
    });

    it('does not mark dirty in read-only mode', () => {
        assert.strictEqual(shouldMarkDirty({ ...base, isReadOnly: true }), false);
    });

    it('does not mark dirty before the step data has hydrated', () => {
        assert.strictEqual(shouldMarkDirty({ ...base, isHydrated: false }), false);
    });

    it('does not mark dirty when nothing actually changed (e.g. hydration-only render)', () => {
        assert.strictEqual(shouldMarkDirty({ ...base, depsChanged: false }), false);
    });
});
