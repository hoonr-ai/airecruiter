import { redirect } from "next/navigation";

// /jobs/[jobId]/candidates was a dead route — candidates live at /rankings.
export default async function CandidatesRedirect({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = await params;
  redirect(`/jobs/${jobId}/rankings`);
}
