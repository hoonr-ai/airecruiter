// JobDiva URL builders.
//
// Candidate profile pages live at `viewcandidate2_real.jsp` — the live format.
// (The older `servlet/jd?uid=` form is dead / 404s.) Building the candidate URL
// from the JobDiva candidate id always resolves without a backend round-trip,
// which is how the Step-5 candidate detail modal opens JobDiva profiles.

const JOBDIVA_BASE = "https://www1.jobdiva.com";

/**
 * Build the JobDiva candidate profile URL from a JobDiva candidate id.
 * Returns "" when no id is provided.
 */
export function buildJobDivaCandidateUrl(
  candidateId: string | number | null | undefined,
): string {
  const id = String(candidateId ?? "").trim();
  if (!id) return "";
  return `${JOBDIVA_BASE}/employers/myreports/viewcandidate2_real.jsp?docids=-1&candidateid=${encodeURIComponent(id)}`;
}
