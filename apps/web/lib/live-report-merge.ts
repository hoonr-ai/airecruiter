import type { Snapshot, JobBlockData, CandidateRow, Anomaly } from "@/app/admin/live-report/types";

export interface MergeSnapshotsOptions {
  jobTitle?: string;
}

/**
 * Merge and deduplicate multiple launch snapshots for a single jobdiva_id.
 * Snapshots are sorted chronologically so newer launches take precedence.
 * Events are deduplicated on (ts | type | subtype | interviewId) and sorted by timestamp.
 *
 * NOTE: Retained for client-side offline merging, test suites, or any multi-launch aggregations
 * if per-launch snapshot replay is ever utilized. Primary live report UI now uses single
 * server-side job snapshot via getJobSnapshot.
 */
export function mergeLaunchSnapshots(
  snapshots: Snapshot[],
  jdId: string,
  options?: MergeSnapshotsOptions
): Snapshot {
  // Sort snapshots chronologically by created_at ascending so later runs take precedence
  const sortedSnapshots = [...snapshots].sort((a, b) => {
    const timeA = a.created_at ? new Date(a.created_at).getTime() : 0;
    const timeB = b.created_at ? new Date(b.created_at).getTime() : 0;
    return timeA - timeB;
  });

  const candidateMap = new Map<number, CandidateRow>();
  const anomalyList: Anomaly[] = [];
  const seenAnomalyKeys = new Set<string>();

  for (const snap of sortedSnapshots) {
    if (!snap) continue;
    for (const a of snap.anomalies || []) {
      const k = `${a.kind}_${a.interview_id}`;
      if (!seenAnomalyKeys.has(k)) {
        seenAnomalyKeys.add(k);
        anomalyList.push(a);
      }
    }

    for (const j of snap.jobs || []) {
      for (const c of j.candidates || []) {
        const existing = candidateMap.get(c.interview_id);
        if (!existing) {
          // Sort initial events by timestamp
          const sortedEvents = [...(c.events || [])].sort((e1, e2) => {
            const t1 = e1.ts ? new Date(e1.ts).getTime() : 0;
            const t2 = e2.ts ? new Date(e2.ts).getTime() : 0;
            return t1 - t2;
          });
          candidateMap.set(c.interview_id, { ...c, events: sortedEvents });
        } else {
          // Merge events deduplicated on full key (ts | type | subtype) via Set
          const seenEventKeys = new Set<string>();
          const combinedEvents = [...(existing.events || []), ...(c.events || [])];
          const mergedEvents = combinedEvents.filter((evt) => {
            const key = `${evt.ts || ""}|${evt.type || ""}|${evt.subtype || ""}`;
            if (seenEventKeys.has(key)) return false;
            seenEventKeys.add(key);
            return true;
          });

          mergedEvents.sort((e1, e2) => {
            const t1 = e1.ts ? new Date(e1.ts).getTime() : 0;
            const t2 = e2.ts ? new Date(e2.ts).getTime() : 0;
            return t1 - t2;
          });

          // Non-null / non-empty fields in newer launch overwrite older, while preserving non-empty older data
          const mergedCandidate: CandidateRow = {
            ...existing,
            ...c,
            name: c.name || existing.name,
            phase: c.phase || existing.phase,
            outreach_status: c.outreach_status ?? existing.outreach_status,
            call_outcome: c.call_outcome ?? existing.call_outcome,
            terminal_reason: c.terminal_reason ?? existing.terminal_reason,
            overall_score: c.overall_score ?? existing.overall_score,
            events: mergedEvents,
          };

          candidateMap.set(c.interview_id, mergedCandidate);
        }
      }
    }
  }

  const aggregatedCandidates = Array.from(candidateMap.values());
  const newestSnap = sortedSnapshots[sortedSnapshots.length - 1];
  const oldestSnap = sortedSnapshots[0];

  // Prefer title and customer_name from the newest snapshot
  const primaryTitle =
    newestSnap?.jobs?.[0]?.title || oldestSnap?.jobs?.[0]?.title || options?.jobTitle || "Untitled job";
  const primaryCustomer =
    newestSnap?.jobs?.[0]?.customer_name || oldestSnap?.jobs?.[0]?.customer_name || null;
  const primaryBulkId = newestSnap?.bulk_id || oldestSnap?.bulk_id || "aggregated";

  return {
    bulk_id: primaryBulkId,
    created_at: newestSnap?.created_at || oldestSnap?.created_at || null,
    state: sortedSnapshots.some((s) => s.state === "live") ? "live" : "archived",
    jobs: [
      {
        bulk_jd_id: null,
        jobdiva_id: jdId,
        title: primaryTitle,
        customer_name: primaryCustomer,
        candidates: aggregatedCandidates,
      },
    ],
    anomalies: anomalyList,
  };
}
