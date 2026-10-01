import { describe, it } from 'node:test';
import assert from 'node:assert';
import { canEditQuestionType } from '../lib/question-moderation';

describe('canEditQuestionType', () => {
    it('returns true for custom/other categories without system flags', () => {
        assert.strictEqual(canEditQuestionType({ category: 'custom' }), true);
        assert.strictEqual(canEditQuestionType({ category: 'other' }), true);
        assert.strictEqual(canEditQuestionType({ category: 'CUSTOM' }), true);
        assert.strictEqual(canEditQuestionType({ category: 'oTheR' }), true);
    });

    it('returns false for trusted categories', () => {
        assert.strictEqual(canEditQuestionType({ category: 'role-specific' }), false);
        assert.strictEqual(canEditQuestionType({ category: 'default' }), false);
        assert.strictEqual(canEditQuestionType({ category: 'logistics' }), false);
        assert.strictEqual(canEditQuestionType({ category: null }), true);
        assert.strictEqual(canEditQuestionType({}), true);
    });

    it('returns false for system flagged questions regardless of category', () => {
        assert.strictEqual(canEditQuestionType({ category: 'custom', is_default: true }), false);
        assert.strictEqual(canEditQuestionType({ category: 'other', is_locked: true }), false);
        assert.strictEqual(canEditQuestionType({ category: 'custom', is_default: true, is_locked: true }), false);
    });
});
