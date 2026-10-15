import { after, afterEach, before, beforeEach, describe, it, mock } from 'node:test';
import assert from 'node:assert';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { haveDepsChanged, shouldMarkDirty, useStepAutosave, type SaveJobDraft } from './use-step-autosave.ts';

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

describe('useStepAutosave (mounted)', () => {
    // There is no jsdom here. react-dom/client can still mount a component
    // that renders nothing given these stand-ins for window/document/container.
    const fakeDocument = Object.assign(new EventTarget(), {
        nodeType: 9,
        visibilityState: 'visible',
        defaultView: null as unknown,
    });
    const fakeWindow = Object.assign(new EventTarget(), { document: fakeDocument, HTMLIFrameElement: class {} });
    fakeDocument.defaultView = fakeWindow;
    const g = globalThis as Record<string, unknown>;

    before(() => {
        g.IS_REACT_ACT_ENVIRONMENT = true;
        g.window = fakeWindow;
        g.document = fakeDocument;
    });
    after(() => {
        delete g.IS_REACT_ACT_ENVIRONMENT;
        delete g.window;
        delete g.document;
    });

    const STEP = 5;
    type Props = { currentStep: number; data: unknown; saveResult?: { ok: boolean, skipped?: boolean } };
    type Call = { render: number; params: Parameters<SaveJobDraft>[0] };
    let root: Root;
    let renders: number;
    let calls: Call[];

    // Like page.tsx, every render hands the hook a brand-new saveJobDraft.
    // saveResult lets a test simulate a failed or skipped save (PR #809
    // review follow-up: a non-ok flush must not silently drop the edit).
    function Harness({ currentStep, data, saveResult }: Props) {
        const render = ++renders;
        const saveJobDraft: SaveJobDraft = async (params) => {
            calls.push({ render, params });
            return saveResult ?? { ok: true };
        };
        useStepAutosave(currentStep, STEP, false, true, saveJobDraft, [data]);
        return null;
    }
    const render = (props: Props) => act(async () => root.render(createElement(Harness, props)));

    beforeEach(async () => {
        mock.timers.enable({ apis: ['setTimeout'] });
        renders = 0;
        calls = [];
        fakeDocument.visibilityState = 'visible';
        const container = Object.assign(new EventTarget(), {
            nodeType: 1,
            tagName: 'DIV',
            namespaceURI: 'http://www.w3.org/1999/xhtml',
            ownerDocument: fakeDocument,
        });
        root = createRoot(container as unknown as Element);
        await render({ currentStep: STEP, data: 'loaded' });
    });
    afterEach(async () => {
        await act(async () => root.unmount());
        mock.timers.reset();
    });

    it('a re-render with unchanged data does not cancel the pending autosave', async () => {
        await render({ currentStep: STEP, data: 'edited' });
        mock.timers.tick(500);
        await render({ currentStep: STEP, data: 'edited' }); // e.g. a launch-session poll
        mock.timers.tick(500);
        await render({ currentStep: STEP, data: 'edited' });
        mock.timers.tick(500);

        assert.strictEqual(calls.length, 1);
        assert.deepStrictEqual(calls[0].params, { currentStep: STEP, saveType: 'auto', skipToast: true });
        assert.strictEqual(calls[0].render, renders, 'saves through the latest saveJobDraft');
    });

    it('a further edit inside the window still restarts the debounce', async () => {
        await render({ currentStep: STEP, data: 'edit 1' });
        mock.timers.tick(1000);
        await render({ currentStep: STEP, data: 'edit 2' });
        mock.timers.tick(1000);
        assert.strictEqual(calls.length, 0);
        mock.timers.tick(500);
        assert.strictEqual(calls.length, 1);
    });

    it('re-renders without an edit never save', async () => {
        await render({ currentStep: STEP, data: 'loaded' });
        await render({ currentStep: STEP, data: 'loaded' });
        mock.timers.tick(5000);
        assert.strictEqual(calls.length, 0);
    });

    it('leaving the step flushes a pending edit once, right away', async () => {
        await render({ currentStep: STEP, data: 'edited' });
        mock.timers.tick(200);
        await render({ currentStep: STEP - 1, data: 'edited' });
        assert.strictEqual(calls.length, 1);
        assert.deepStrictEqual(calls[0].params, { currentStep: STEP, saveType: 'auto', skipToast: true });
        mock.timers.tick(5000);
        assert.strictEqual(calls.length, 1);
    });

    it('hiding the tab flushes a pending edit with keepalive through the latest saveJobDraft', async () => {
        await render({ currentStep: STEP, data: 'edited' });
        await render({ currentStep: STEP, data: 'edited' });
        fakeDocument.visibilityState = 'hidden';
        fakeDocument.dispatchEvent(new Event('visibilitychange'));
        assert.strictEqual(calls.length, 1);
        assert.deepStrictEqual(calls[0].params, { currentStep: STEP, saveType: 'auto', skipToast: true, keepalive: true });
        assert.strictEqual(calls[0].render, renders);
    });

    it('a step-change flush that is skipped (an explicit save was already in flight) restores the dirty flag, so a later step change retries it', async () => {
        // First flush: skipped (saveJobDraft bailed out — see
        // lib/job-draft-save-dispatch.ts's "skip" action). Without
        // restoring dirtyRef on a non-ok result, this edit would be lost
        // for good — the PR #809 review's medium finding.
        await render({ currentStep: STEP, data: 'edited' });
        mock.timers.tick(200);
        // The render that changes currentStep is the one whose saveJobDraft
        // closure the flush effect actually calls (useEffectEvent always
        // reads the latest one) — the failing result belongs here.
        await render({ currentStep: STEP - 1, data: 'edited', saveResult: { ok: false, skipped: true } });
        assert.strictEqual(calls.length, 1, 'first flush attempted (and skipped)');
        // Let the first flush's `.then(...)` (which restores dirtyRef on a
        // non-ok result) actually run before triggering the next flush.
        await Promise.resolve();
        await Promise.resolve();

        // currentStep changes again; this hook instance's own effect
        // (targetStep === STEP) re-runs on every currentStep change, so a
        // still-dirty flag from the skipped flush retries here.
        await render({ currentStep: STEP - 2, data: 'edited', saveResult: { ok: true } });
        assert.strictEqual(calls.length, 2, 'the restored dirty flag retried the flush');
    });

    it('a tab-hide flush that fails restores the dirty flag, so a later hide retries it', async () => {
        await render({ currentStep: STEP, data: 'edited', saveResult: { ok: false } });
        fakeDocument.visibilityState = 'hidden';
        fakeDocument.dispatchEvent(new Event('visibilitychange'));
        assert.strictEqual(calls.length, 1, 'first flush attempted (and failed)');

        fakeDocument.visibilityState = 'visible';
        fakeDocument.dispatchEvent(new Event('visibilitychange'));
        await render({ currentStep: STEP, data: 'edited', saveResult: { ok: true } });
        fakeDocument.visibilityState = 'hidden';
        fakeDocument.dispatchEvent(new Event('visibilitychange'));
        assert.strictEqual(calls.length, 2, 'the restored dirty flag retried the flush on the next hide');
    });
});
