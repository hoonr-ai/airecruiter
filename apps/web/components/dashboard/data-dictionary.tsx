"use client";

// Definitions behind every number on the PAIR Dashboard, in words. They must
// say what services/pair_dashboard.py computes; change both together.

import { useState } from "react";
import { BookOpen } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";

type Entry = { term: string; text: string };
type Section = { title: string; entries: Entry[] };

const CORE: Section = {
  title: "Core concepts",
  entries: [
    {
      term: "PAIR Requirement",
      text: "A JobDiva job added to PAIR. All PAIR versions of a job (v1, v2, …) are one requirement.",
    },
    { term: "PAIR Launch", text: "A requirement's first successful PAIR launch." },
    {
      term: "PAIR Candidate",
      text: "A candidate PAIR launched on a requirement (Engage initiated): the people the Rankings page lists as launched.",
    },
    { term: "Engage Pass", text: "A Pass on PAIR Engage, by the Rankings page's own rule." },
    {
      term: "External Submission",
      text: "A JobDiva submittal to the client. Internal submittals (to a Pyramid hiring manager) are not counted.",
    },
    {
      term: "Direct",
      text: "An external submission, client interview or start on the same requirement the candidate passed PAIR on, after that requirement's PAIR launch.",
    },
    {
      term: "Cross-Sub",
      text: "The same on a different requirement, when the candidate's first submittal to it came within 90 days after their Engage Pass (a later client interview or start on that submittal still counts). It is credited to the requirement they passed on, so a team sees the cross-subs its own passes produced.",
    },
    {
      term: "Non-PAIR",
      text: "Activity on a PAIR requirement, after its launch, by a candidate without an Engage Pass there (or anywhere in the 90 days before) — including PAIR candidates who were launched but failed or have not finished.",
    },
    {
      term: "Activity-based",
      text: "Overview and Productivity numbers count what happened in the selected period, whichever requirement it happened on: a start this week can come from a requirement launched a month ago. \"vs prev\" compares with the same number of days just before. Weeks run Monday–Sunday; all dates are US Eastern.",
    },
    {
      term: "Data sources",
      text: "Candidates, launches, passes and recruiter feedback come from PAIR. Requirements, submittals, interviews and starts come from JobDiva, copied every 30 minutes.",
    },
  ],
};

const TABS: { key: string; label: string; sections: Section[] }[] = [
  {
    key: "overview",
    label: "Overview",
    sections: [
      {
        title: "PAIR pipeline",
        entries: [
          {
            term: "Net Openings",
            text: "Openings on requirements added to PAIR in the period (JobDiva's Openings; 1 when blank), leaving out requirements whose JobDiva status is Cancelled, On Hold, Declined or Ignored. The line under the value is the number of requirements.",
          },
          {
            term: "PAIR Volume as a % of Total",
            text: "Of all the openings Pyramid received in the period (by JobDiva issue date, same exclusions), the share on requirements launched with PAIR. With a Recruiting Manager selected, the total is the requirements anyone on that team is tagged on in JobDiva plus its PAIR requirements; one another team launched still counts as launched with PAIR.",
          },
          { term: "PAIR Candidates Launched", text: "Candidates whose first PAIR launch on a requirement falls in the period." },
          {
            term: "PAIR Candidates Passed",
            text: "Candidates who reached an Engage Pass in the period, dated by when PAIR finished their screen.",
          },
        ],
      },
      {
        title: "PAIR outcomes",
        entries: [
          {
            term: "PAIR Submissions",
            text: "External submissions made in the period by PAIR candidates, Direct plus Cross-Sub. \"Recorded in PAIR\" is the external Submits recruiters pressed on the Rankings page in the period; JobDiva can trail it.",
          },
          {
            term: "PAIR Interviews",
            text: "Client interviews dated in the period (JobDiva interview types ending \"External\", or with no type), Direct plus Cross-Sub.",
          },
          { term: "PAIR Starts", text: "Starts (JobDiva hires) whose start date falls in the period, Direct plus Cross-Sub." },
          { term: "PAIR Fill Ratio", text: "PAIR Starts ÷ Net Openings, both in the period." },
          { term: "Submit → Start", text: "PAIR Starts ÷ PAIR Submissions, both in the period." },
        ],
      },
      {
        title: "Non-PAIR comparison",
        entries: [
          {
            term: "Non-PAIR Submissions on Reqs Assigned to PAIR",
            text: "External submissions in the period on PAIR requirements (after their launch) of candidates without an Engage Pass — see Non-PAIR above.",
          },
          { term: "Non-PAIR Starts on Reqs Assigned to PAIR", text: "Starts in the period from those submissions." },
          { term: "PAIR Share of All Subs", text: "PAIR Submissions ÷ (PAIR Submissions + Non-PAIR Submissions)." },
        ],
      },
    ],
  },
  {
    key: "funnel",
    label: "Funnel & Speed",
    sections: [
      {
        title: "Candidate funnel",
        entries: [
          {
            term: "Which requirements",
            text: "PAIR requirements that were launched and posted (JobDiva issue date) in the Job Posted range, after the filters above. Every stage counts people over those requirements' whole life.",
          },
          {
            term: "Engage Initiated",
            text: "PAIR candidates, by where PAIR found them: Applied (applied to the job in JobDiva), JobDiva (talent search, job agent, database), LinkedIn (LinkedIn and Exa), Other (Dice, uploaded résumés, …).",
          },
          { term: "Engage Completed", text: "Candidates whose Engage ended in a Pass or a Fail." },
          { term: "Engage Passed", text: "Candidates with an Engage Pass." },
          {
            term: "PAIR – Submission / Interview / Start",
            text: "Passed candidates who reached that stage in JobDiva, Direct or Cross-Sub. A person counts once per stage; Direct wins over Cross-Sub.",
          },
          {
            term: "Non-PAIR stages",
            text: "Distinct JobDiva candidates without an Engage Pass (see Non-PAIR) who reached the stage on these requirements after their launch. Shown for comparison.",
          },
        ],
      },
      {
        title: "Speed to pipeline",
        entries: [
          { term: "Time to PAIR Launch", text: "JobDiva issue date → the requirement's first successful PAIR launch." },
          { term: "Time to First Pass", text: "First PAIR launch → the first Engage Pass on the requirement." },
          {
            term: "Time to First Submit",
            text: "First PAIR launch (or job posted) → the first external submission of a PAIR candidate on the requirement, in JobDiva or recorded in PAIR, whichever came first.",
          },
          {
            term: "How it rolls up",
            text: "Each requirement gets its own time; the tile shows the median across requirements, with the average beneath. Requirements that have not reached the milestone are left out.",
          },
        ],
      },
      {
        title: "Feedback",
        entries: [
          {
            term: "Rejection Reasons",
            text: "The reasons recruiters chose when rejecting PAIR candidates on the Rankings page.",
          },
          {
            term: "Pending Feedback",
            text: "Passed candidates with no recruiter decision yet (Submit, Reject or Unreachable). The clock starts at the pass. Click the tile to see who is waiting, on which requirement, and for how long.",
          },
        ],
      },
    ],
  },
  {
    key: "productivity",
    label: "Productivity",
    sections: [
      {
        title: "Recruiter productivity",
        entries: [
          {
            term: "Recruiters",
            text: "The members of each Recruiting Manager's team on the Teams page. The RM (the team's lead) is not counted as a recruiter, unless the team has no members yet. All Teams = the members of every team. There is no per-recruiter view: JobDiva assigns requirements to whole teams.",
          },
          {
            term: "Reqs Assigned (per week)",
            text: "Requirements anyone on the team (RM included) is tagged on in JobDiva, plus the team's PAIR requirements, that were open during each week, per recruiter, averaged over the weeks of the period. All requirements, not only PAIR ones.",
          },
          {
            term: "Client Subs (per week)",
            text: "External submissions made by the team's recruiters (the submitting user in JobDiva), per recruiter per week, split PAIR / Non-PAIR.",
          },
          {
            term: "Starts (per week)",
            text: "Starts on those submissions, counted in the week of the start date, per recruiter per week, split PAIR / Non-PAIR.",
          },
          {
            term: "Why these three",
            text: "If PAIR makes recruiters more productive, each should work more requirements first, then submit more, then start more.",
          },
        ],
      },
    ],
  },
];

export function DataDictionaryButton({ initialTab = "overview" }: { initialTab?: string }) {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState(initialTab);
  const active = TABS.find((t) => t.key === tab) ?? TABS[0];
  return (
    <>
      <button
        type="button"
        onClick={() => {
          setTab(initialTab);
          setOpen(true);
        }}
        className="inline-flex items-center gap-1.5 rounded-md bg-sky-50 px-2.5 py-1 text-[12px] font-semibold text-sky-700 ring-1 ring-inset ring-sky-200 hover:bg-sky-100"
      >
        <BookOpen className="h-3.5 w-3.5" />
        Data Dictionary
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl bg-white">
          <DialogHeader>
            <DialogTitle className="text-[17px] font-bold text-slate-900">Data Dictionary</DialogTitle>
            <DialogDescription className="text-[12.5px] text-slate-500">
              How every number on the PAIR Dashboard is defined.
            </DialogDescription>
          </DialogHeader>
          <div className="inline-flex w-fit rounded-lg bg-slate-100 p-0.5">
            {TABS.map((t) => (
              <button
                key={t.key}
                type="button"
                onClick={() => setTab(t.key)}
                className={`rounded-md px-3 py-1.5 text-[12.5px] font-semibold ${
                  tab === t.key ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          <div className="max-h-[60vh] space-y-5 overflow-y-auto pr-1">
            {[CORE, ...active.sections].map((section) => (
              <section key={section.title}>
                <h3 className="mb-2 text-[11px] font-bold uppercase tracking-wider text-slate-400">{section.title}</h3>
                <dl className="divide-y divide-slate-100 rounded-lg border border-slate-200">
                  {section.entries.map((entry) => (
                    <div key={entry.term} className="grid gap-1 px-3 py-2.5 sm:grid-cols-[200px_1fr] sm:gap-4">
                      <dt className="text-[12.5px] font-semibold text-slate-800">{entry.term}</dt>
                      <dd className="text-[12.5px] leading-relaxed text-slate-600">{entry.text}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
