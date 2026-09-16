import { isLockedDefaultQuestion, resolveLockedFlag } from "../campaigns";

describe("campaigns lib", () => {
  describe("isLockedDefaultQuestion", () => {
    it("returns true for exact matches of protected questions", () => {
      expect(isLockedDefaultQuestion("Are you authorized to work indefinitely for any employer in the United States?")).toBe(true);
      expect(isLockedDefaultQuestion("Will you now or in the future require visa sponsorship to continue working in the United States?")).toBe(true);
      expect(isLockedDefaultQuestion("Which types of working arrangements are you open to and eligible for? Select all that apply: W2 Employee, Subcontractor to Pyramid through your current employer, Independent Contractor")).toBe(true);
    });

    it("returns true for case-insensitive matches", () => {
      expect(isLockedDefaultQuestion("ARE YOU AUTHORIZED TO WORK INDEFINITELY FOR ANY EMPLOYER IN THE UNITED STATES?")).toBe(true);
    });

    it("returns false for non-protected questions", () => {
      expect(isLockedDefaultQuestion("What is your expected compensation for this role?")).toBe(false);
      expect(isLockedDefaultQuestion("")).toBe(false);
    });
  });

  describe("resolveLockedFlag", () => {
    it("returns true if backend flag is true", () => {
      expect(resolveLockedFlag({ is_locked: true, question_text: "Some random question" })).toBe(true);
    });

    it("returns true if backend flag is false but text matches protected question", () => {
      expect(resolveLockedFlag({ is_locked: false, question_text: "Are you authorized to work indefinitely for any employer in the United States?" })).toBe(true);
    });

    it("returns false if backend flag is false and text does not match", () => {
      expect(resolveLockedFlag({ is_locked: false, question_text: "What is your expected compensation for this role?" })).toBe(false);
    });
  });
});
