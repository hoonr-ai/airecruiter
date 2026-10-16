// Pure helpers behind the recruiter handoff join page (app/handoff/[interviewId]).
// Kept out of the page so the node --test suite can cover them.

/** The candidate in the room: not the PAIR bot or its avatar (LiveKit agent
 * participants), and not a recruiter - Pair-bot mints recruiter tokens as
 * recruiter_{interview_id}_{hash}. Web candidates join as candidate_{id};
 * phone candidates under a SIP identity. */
export const isCandidateParticipant = (p: { identity: string; isAgent: boolean }): boolean =>
  !p.isAgent && p.identity !== "" && !p.identity.startsWith("recruiter_");

export type JoinView =
  | { kind: "waiting" }
  | { kind: "over"; reason: string }
  | { kind: "not_yours" }
  | { kind: "none" };

/** What the page shows before the recruiter joins, from Pair-bot's state. */
export function joinViewFor(state: {
  state: string | null;
  // null: the API couldn't tell (no recruiter on Pair-bot's reply), so
  // claim decides; only an explicit false means another recruiter's.
  is_your_handoff?: boolean | null;
  claimed_by_email?: string | null;
  close_reason?: string | null;
}): JoinView {
  if (!state.state) return { kind: "none" };
  if (state.is_your_handoff === false) return { kind: "not_yours" };
  switch (state.state) {
    case "requested":
    case "claimed":
    case "joined":
      // claimed/joined by you is still joinable: a reload or a dropped
      // connection re-claims the same seat.
      return { kind: "waiting" };
    case "missed":
      return { kind: "over", reason: "Nobody joined in time, so PAIR closed the call." };
    case "cancelled":
      return { kind: "over", reason: "The candidate left the call." };
    case "closed":
      return { kind: "over", reason: "This interview has ended." };
    default:
      return { kind: "none" };
  }
}

/** States Pair-bot never moves on from (contract: a later state never moves back). */
export const isHandoffOver = (state: string | null | undefined): boolean =>
  state === "closed" || state === "missed" || state === "cancelled";

/** A readable message for a failed claim/close (Pair-bot's own status codes). */
export function describeHandoffError(err: unknown): string {
  const status = (err as { status?: number })?.status;
  switch (status) {
    case 403:
      return "Only the recruiter who launched this interview can join it.";
    case 404:
      return "This interview could not be found.";
    case 409:
      return "This candidate isn't waiting for a recruiter right now.";
    case 410:
      return "Too late - the candidate is no longer waiting.";
    case 502:
      return "PAIR-bot can't be reached. Try again in a moment.";
    default:
      return (err as Error)?.message || "Something went wrong. Try again.";
  }
}

/** "4:05" for the waiting countdown. */
export function formatSecondsLeft(expiresAt: string | null | undefined, now: number): string | null {
  if (!expiresAt) return null;
  const left = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
  if (!Number.isFinite(left)) return null;
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}
