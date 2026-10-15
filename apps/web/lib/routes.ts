// Links into the job wizard (app/jobs/new/page.tsx), in one place so a change
// to its query params or step numbers is made once.

/** Step 5 of the wizard: Source & Launch PAIR. */
export const SOURCE_AND_LAUNCH_STEP = 5;

export function sourceAndLaunchHref(jobRef: string | number): string {
  return `/jobs/new?jobId=${encodeURIComponent(String(jobRef))}&mode=source&step=${SOURCE_AND_LAUNCH_STEP}`;
}

/** A job's rankings page: where a finished launch's results live. */
export function jobRankingsHref(jobRef: string | number): string {
  return `/jobs/${encodeURIComponent(String(jobRef))}/rankings`;
}
