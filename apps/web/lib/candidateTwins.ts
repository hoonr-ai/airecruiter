// "Provisioned twin" handling for candidate rows of one job.
//
// Launch PAIR records a JobDiva application for every person it provisions,
// stamping the resulting JobDiva profile id on the person's ORIGIN row
// (`data.jobdiva_candidate_id` on the LinkedIn-Exa / Dice / ... row). JobDiva's
// pools then return that same person under the profile id with a JobDiva-*
// label, and until the applicant sync matched them back, each one landed as
// a second row: `(job, <profile id>, "JobDiva-Applicants")`. Both rows are the
// same person. The stamped origin row is the one that must survive -- with its
// origin label -- while the twin's engage bookkeeping is folded into it.
//
// Pure functions so the rule is unit-testable (node --test) and shared by the
// rank list and any other per-job candidate view.

/** The fields the twin rule reads. Every candidate row shape in the app has them (optionally). */
export type TwinRow = {
  candidate_id?: string | number | null;
  id?: string | number | null;
  source?: string | null;
  jobdiva_candidate_id?: string | number | null;
  jobdiva_application_origin?: string | null;
  data?: Record<string, unknown> | null;
};

const isJobDivaLabel = (source: unknown): boolean =>
  String(source || "").trim().toLowerCase().startsWith("jobdiva");

const rowCandidateId = (c: TwinRow | null | undefined): string =>
  String(c?.candidate_id ?? c?.id ?? "").trim();

/** The JobDiva profile id a row links to, if any (top-level or in `data`). */
export const stampedJobDivaId = (c: TwinRow | null | undefined): string =>
  String(c?.jobdiva_candidate_id || c?.data?.jobdiva_candidate_id || "").trim();

/**
 * True when `twin` is a JobDiva-labelled row whose candidate_id is the JobDiva
 * profile id `origin` (a non-JobDiva-labelled row) stores -- i.e. the same
 * person, re-imported from JobDiva after Launch PAIR provisioned them.
 */
export function isProvisionedTwinOf(
  twin: TwinRow | null | undefined,
  origin: TwinRow | null | undefined,
): boolean {
  if (!twin || !origin || twin === origin) return false;
  if (!isJobDivaLabel(twin.source) || isJobDivaLabel(origin.source)) return false;
  const twinId = rowCandidateId(twin);
  const stamp = stampedJobDivaId(origin);
  return Boolean(twinId) && twinId === stamp && rowCandidateId(origin) !== twinId;
}

/**
 * Collapse provisioned twins into their origin rows. `fold(origin, twin)`
 * returns the surviving row (typically a copy of `origin` with the twin's
 * engage fields filled in where the origin has none). Row order is preserved
 * for survivors; twins are removed. Rows without a twin pass through untouched.
 */
export function mergeProvisionedTwins<T extends TwinRow>(
  rows: readonly T[],
  fold: (origin: T, twin: T) => T,
): T[] {
  // JobDiva profile id -> the first origin row that stores it.
  const originByStamp = new Map<string, number>();
  rows.forEach((row, idx) => {
    if (isJobDivaLabel(row?.source)) return;
    const stamp = stampedJobDivaId(row);
    if (stamp && !originByStamp.has(stamp)) originByStamp.set(stamp, idx);
  });
  if (originByStamp.size === 0) return [...rows];

  const survivors: (T | null)[] = [...rows];
  rows.forEach((row, idx) => {
    if (!isJobDivaLabel(row?.source)) return;
    const originIdx = originByStamp.get(rowCandidateId(row));
    if (originIdx === undefined || originIdx === idx) return;
    const origin = survivors[originIdx];
    if (origin === null || !isProvisionedTwinOf(row, origin)) return;
    survivors[originIdx] = fold(origin, row);
    survivors[idx] = null;
  });
  return survivors.filter((r): r is T => r !== null);
}

export type JobDivaLinkage = {
  inJobDiva: boolean;
  /** "pair" = Launch PAIR filed the application; "organic" = applied in JobDiva; "" = unknown. */
  applicationOrigin: "pair" | "organic" | "";
  /** Short caption for the source cell, empty when there is nothing to add. */
  caption: string;
};

/**
 * Secondary "where does this person stand in JobDiva" fact for an external-
 * origin row. JobDiva-labelled rows get no caption: their label already says
 * it. `jobdiva_application_origin` is stamped by the provisioner ("pair") and
 * the applicant sync ("organic"); legacy rows carry neither and read "In JobDiva".
 */
export function jobDivaLinkage(c: TwinRow | null | undefined): JobDivaLinkage {
  const raw = String(
    c?.jobdiva_application_origin || c?.data?.jobdiva_application_origin || "",
  ).trim().toLowerCase();
  const applicationOrigin: JobDivaLinkage["applicationOrigin"] =
    raw === "pair" || raw === "organic" ? raw : "";
  const inJobDiva = Boolean(stampedJobDivaId(c)) || isJobDivaLabel(c?.source);
  if (!inJobDiva || isJobDivaLabel(c?.source)) {
    return { inJobDiva, applicationOrigin, caption: "" };
  }
  const caption =
    applicationOrigin === "pair"
      ? "In JobDiva · via PAIR"
      : applicationOrigin === "organic"
        ? "In JobDiva · applied directly"
        : "In JobDiva";
  return { inJobDiva, applicationOrigin, caption };
}
