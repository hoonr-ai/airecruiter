import { redirect } from "next/navigation";

// /jobs/[jobId]/candidates was a dead route — candidates live at /rankings.
export default function CandidatesRedirect({
  params,
}: {
  params: { jobId: string };
}) {
  redirect(`/jobs/${params.jobId}/rankings`);
}
