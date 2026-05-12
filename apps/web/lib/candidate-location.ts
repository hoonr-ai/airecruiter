// Helper for rendering candidate location with clear home-vs-work labeling.
// JobDiva's JobApplicantsDetail exposes home (locationCity/locationState) and
// current-work (workCity/workState) as distinct fields. We surface both
// separately so the UI never relabels work location as home.

type CandidateLocationInput = {
  location?: string | null;
  city?: string | null;
  state?: string | null;
  work_location?: string | null;
  work_city?: string | null;
  work_state?: string | null;
};

export function getCandidateLocations(c: CandidateLocationInput): {
  home: string | null;
  work: string | null;
} {
  const homeStr = (c.location || "").trim();
  const home =
    homeStr ||
    [c.city, c.state].filter((p): p is string => !!p && !!p.trim()).join(", ").trim() ||
    null;

  const workStr = (c.work_location || "").trim();
  const work =
    workStr ||
    [c.work_city, c.work_state]
      .filter((p): p is string => !!p && !!p.trim())
      .join(", ")
      .trim() ||
    null;

  return { home: home || null, work: work || null };
}
