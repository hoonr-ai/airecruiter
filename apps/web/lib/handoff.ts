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

// ---------------------------------------------------------------------------
// Recruiter copilot (Pair-bot docs/recruiter-copilot-design.md)
// ---------------------------------------------------------------------------

/** LiveKit data topic Pair-bot sends transcript/suggestions on (recruiter only),
 * and the page sends its copilot settings on. */
export const COPILOT_TOPIC = "handoff_copilot";

export interface CopilotLine {
  role: string;
  text: string;
  t?: number | string | null;
  seq: number;
}

export interface CopilotSuggestion {
  question: string;
  why?: string | null;
}

export interface CopilotSuggestionBatch {
  id: string;
  items: CopilotSuggestion[];
  t?: number | string | null;
}

export type CopilotPacket =
  | ({ type: "transcript" } & CopilotLine)
  | ({ type: "suggestions" } & CopilotSuggestionBatch);

/** Merge new transcript lines into the current list: one line per `seq`
 * (the live stream and the /transcript backfill overlap), ordered by `seq`.
 * Returns the same array when nothing changed, so React can skip a render. */
export function mergeTranscript(current: CopilotLine[], incoming: CopilotLine[]): CopilotLine[] {
  const seen = new Set(current.map((l) => l.seq));
  const fresh = incoming.filter((l) => Number.isFinite(l.seq) && !seen.has(l.seq) && (seen.add(l.seq), true));
  if (fresh.length === 0) return current;
  return [...current, ...fresh].sort((a, b) => a.seq - b.seq);
}

/** Keep only well-formed suggestion items (stored batches come from Pair-bot's
 * /transcript backfill and may be partial). */
export function cleanSuggestionBatches(raw: unknown): CopilotSuggestionBatch[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((b): b is Record<string, unknown> => !!b && typeof b === "object" && Array.isArray((b as { items?: unknown }).items))
    .map((b) => ({
      id: String(b.id ?? ""),
      t: (b.t as number | string | null | undefined) ?? null,
      items: (b.items as unknown[])
        .filter((i): i is Record<string, unknown> => !!i && typeof i === "object" && typeof (i as { question?: unknown }).question === "string")
        .map((i) => ({ question: i.question as string, why: typeof i.why === "string" ? i.why : null })),
    }))
    .filter((b) => b.items.length > 0);
}

/** Add suggestion batches (live packets or backfill) without duplicating a
 * batch id; keeps the newest `limit`. Same array back when nothing changed. */
export function mergeSuggestions(
  current: CopilotSuggestionBatch[],
  incoming: CopilotSuggestionBatch[],
  limit = 10,
): CopilotSuggestionBatch[] {
  const seen = new Set(current.map((b) => b.id).filter(Boolean));
  const fresh: CopilotSuggestionBatch[] = [];
  for (const b of incoming) {
    if (b.id && seen.has(b.id)) continue;
    if (b.id) seen.add(b.id);
    fresh.push(b);
  }
  if (fresh.length === 0) return current;
  return [...current, ...fresh].slice(-limit);
}

/** Decode a `handoff_copilot` data packet; null for anything off-contract. */
export function parseCopilotPacket(raw: string): CopilotPacket | null {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (d.type === "transcript") {
    const seq = Number(d.seq);
    if (!Number.isFinite(seq) || typeof d.text !== "string") return null;
    return { type: "transcript", role: String(d.role ?? ""), text: d.text, t: d.t as number | null, seq };
  }
  if (d.type === "suggestions" && Array.isArray(d.items)) {
    const items = d.items
      .filter((i): i is Record<string, unknown> => !!i && typeof i === "object" && typeof (i as { question?: unknown }).question === "string")
      .map((i) => ({ question: i.question as string, why: typeof i.why === "string" ? i.why : null }));
    return { type: "suggestions", id: String(d.id ?? ""), items, t: d.t as number | null };
  }
  return null;
}

/** Who said a transcript line, from the recruiter's point of view. */
export function speakerLabel(role: string | null | undefined): string {
  switch ((role ?? "").toLowerCase()) {
    case "recruiter":
      return "You";
    case "assistant":
    case "agent":
    case "bot":
      return "PAIR";
    default:
      return "Candidate";
  }
}

export interface CopilotPrefs {
  showTranscript: boolean;
  suggestions: boolean;
}

/** Both on by default (design doc: Decisions). */
export const DEFAULT_COPILOT_PREFS: CopilotPrefs = { showTranscript: true, suggestions: true };

type PrefStorage = Pick<Storage, "getItem" | "setItem">;

export const copilotPrefsKey = (email: string | null | undefined): string =>
  `pair.handoffCopilot.v1:${(email ?? "").trim().toLowerCase() || "anon"}`;

/** The recruiter's remembered toggles. Storage may be missing or throw
 * (private mode, blocked site data): fall back to the defaults. */
export function loadCopilotPrefs(storage: PrefStorage | null, email: string | null | undefined): CopilotPrefs {
  try {
    const raw = storage?.getItem(copilotPrefsKey(email));
    if (!raw) return { ...DEFAULT_COPILOT_PREFS };
    const p = JSON.parse(raw) as Partial<CopilotPrefs>;
    return {
      showTranscript: typeof p.showTranscript === "boolean" ? p.showTranscript : DEFAULT_COPILOT_PREFS.showTranscript,
      suggestions: typeof p.suggestions === "boolean" ? p.suggestions : DEFAULT_COPILOT_PREFS.suggestions,
    };
  } catch {
    return { ...DEFAULT_COPILOT_PREFS };
  }
}

export function saveCopilotPrefs(storage: PrefStorage | null, email: string | null | undefined, prefs: CopilotPrefs): void {
  try {
    storage?.setItem(copilotPrefsKey(email), JSON.stringify(prefs));
  } catch {
    // Remembering is a convenience; the toggle still works this session.
  }
}

/** "12:05" for the in-call timer. */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Post-handoff review (interview view)
// ---------------------------------------------------------------------------

/** Plain-language outcome of a handoff for the interview view; null when the
 * interview had no handoff (the section is hidden). */
export function handoffOutcomeLabel(state: string | null | undefined, closeReason?: string | null): string | null {
  switch (state) {
    case "requested":
      return "Waiting for a recruiter";
    case "claimed":
    case "joined":
      return "Recruiter on the call";
    case "missed":
      return "Missed - nobody joined in time";
    case "cancelled":
      return "Candidate left before a recruiter joined";
    case "closed":
      switch (closeReason) {
        case "max_duration":
          return "Ended - time limit reached";
        case "recruiter_left":
          return "Ended - recruiter left the call";
        case "candidate_left":
          return "Ended - candidate left the call";
        default:
          return "Ended by the recruiter";
      }
    default:
      return null;
  }
}

/** Who said a line, for someone reviewing the call afterwards (not "You"). */
export function reviewSpeakerLabel(role: string | null | undefined): string {
  const label = speakerLabel(role);
  return label === "You" ? "Recruiter" : label;
}

// The recording URL comes from Pair-bot via Pair's API; only a well-formed
// https URL is put into an <audio src>.
export function safeRecordingUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}
