/**
 * Step-5 "Search Sources" switchboard — defaults and persisted-draft
 * migration for `sourcing_filters.sources`.
 *
 * Why this exists as a module: the defaults used to be an inline `useState`
 * literal and the draft-restore code spread whatever the saved draft held
 * straight into state. That is how Exa (LinkedIn-Exa) silently stopped
 * running: the April 2026 QA punch list made it opt-in, every draft saved
 * since then carries `exa: false` as the *default* rather than a choice, and
 * a later "default it back on" could not distinguish the two.
 *
 * `SEARCH_SOURCES_VERSION` is persisted next to `sources` so a stored flag
 * can be read for what it is:
 *   - (absent / < 2)  legacy draft — `exa` was default-OFF; a stored `false`
 *                     is the old default, not a recruiter's untick, so the
 *                     current default wins. A stored `true` (explicit opt-in
 *                     under the old regime) is kept.
 *   - 2               Exa is default-ON; every stored flag is honoured.
 */

export type SearchSourceId =
  | "jobdiva_agent"
  | "jobdiva_talent"
  | "linkedin"
  | "dice"
  | "exa";

export type SearchSources = Record<SearchSourceId, boolean>;

export const SEARCH_SOURCE_IDS: readonly SearchSourceId[] = [
  "jobdiva_agent",
  "jobdiva_talent",
  "linkedin",
  "dice",
  "exa",
];

export const SEARCH_SOURCES_VERSION = 2;

/**
 * Both JobDiva pools, LinkedIn (Unipile — round-robins across every attached
 * account, so default-on no longer risks burning one) and Exa are pre-ticked.
 * Dice stays opt-in (and is hidden from the switchboard; backend wiring kept).
 */
export const DEFAULT_SEARCH_SOURCES: Readonly<SearchSources> = {
  jobdiva_agent: true,
  jobdiva_talent: true,
  linkedin: true,
  dice: false,
  exa: true,
};

/**
 * Merge a persisted `sourcing_filters.sources` object onto `current`.
 *
 * - Drops retired flags (`jobdiva_hotlist`) and anything unknown.
 * - Migrates the legacy single `jobdiva` flag onto both JobDiva pools (that
 *   is what it used to run); explicit per-pool flags win over it.
 * - Ignores non-boolean values.
 * - Applies the pre-v2 Exa rule described in the module docstring.
 *
 * Never mutates `saved` or `current`.
 */
export function restoreSavedSearchSources(
  saved: unknown,
  savedVersion: unknown,
  current: Readonly<SearchSources> = DEFAULT_SEARCH_SOURCES,
): SearchSources {
  const out: SearchSources = { ...current };
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return out;
  const raw = saved as Record<string, unknown>;

  const version =
    typeof savedVersion === "number" && Number.isFinite(savedVersion)
      ? savedVersion
      : 0;

  if (typeof raw.jobdiva === "boolean") {
    out.jobdiva_agent = raw.jobdiva;
    out.jobdiva_talent = raw.jobdiva;
  }
  for (const id of SEARCH_SOURCE_IDS) {
    const value = raw[id];
    if (typeof value === "boolean") out[id] = value;
  }

  if (version < SEARCH_SOURCES_VERSION && raw.exa !== true) {
    // Legacy draft: a missing/false Exa flag is the old default-off, not a
    // decision — keep whatever the caller currently has (the new default).
    out.exa = current.exa;
  }
  return out;
}
