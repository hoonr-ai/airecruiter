"use client";

import { HelpCircle } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

const STEPS: {
  step: string;
  wait: string;
  ge80: string;
  lt80: string;
  call: string;
}[] = [
  { step: "1", wait: "Right away", ge80: "Phase 1", lt80: "Phase 1", call: "10 min later" },
  { step: "2", wait: "30 minutes later", ge80: "Extra 1", lt80: "Skipped", call: "No call" },
  { step: "3", wait: "30 minutes later", ge80: "Phase 2", lt80: "Phase 2", call: "10 min later" },
  { step: "4", wait: "30 minutes later", ge80: "Extra 2", lt80: "Skipped", call: "No call" },
  { step: "5", wait: "1 hour later", ge80: "Phase 3", lt80: "Phase 3", call: "10 min later" },
  { step: "6", wait: "30 minutes later", ge80: "Extra 3", lt80: "Skipped", call: "No call" },
  { step: "7", wait: "2 hours 30 minutes later", ge80: "Phase 4 (final)", lt80: "Phase 4 (final)", call: "10 min later" },
];

export function PhaseOutreachInfo() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button
          type="button"
          className="ml-1 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-slate-400 hover:bg-indigo-50 hover:text-indigo-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
          aria-label="Explain outreach phases, timing, and extra outreach"
          onClick={(e) => e.stopPropagation()}
        >
          <HelpCircle className="h-3.5 w-3.5" />
        </button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="text-[16px] font-bold text-slate-900">
            What the phases mean
          </DialogTitle>
          <DialogDescription className="text-[13px] text-slate-500">
            PAIR reminds a candidate a few times if they have not started the interview yet. Think of it as a
            short sequence of nudges — not four different interviews.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 text-[13px] text-slate-700 leading-relaxed">
          <div className="space-y-2">
            <p>
              <span className="font-semibold text-slate-900">Step 1 starts immediately</span> at launch. Each
              next step waits a little while after the one before it (usually 30 minutes; later reminders wait
              longer).
            </p>
            <p>
              On a regular phase, email and SMS go out together, then a call about{" "}
              <span className="font-semibold">10 minutes</span> later. Extra steps are email and SMS only — no
              call.
            </p>
          </div>

          <div className="grid sm:grid-cols-2 gap-3">
            <div className="rounded-lg border border-slate-200 px-3 py-2.5 space-y-1">
              <p className="text-[11px] font-extrabold uppercase tracking-wider text-slate-400">
                Score 80% or higher
              </p>
              <p>
                They get the full 7-step plan: the four phases <span className="font-semibold">and</span> three
                extra nudges in between (Extra 1, Extra 2, Extra 3).
              </p>
            </div>
            <div className="rounded-lg border border-slate-200 px-3 py-2.5 space-y-1">
              <p className="text-[11px] font-extrabold uppercase tracking-wider text-slate-400">
                Score below 80%
              </p>
              <p>
                No extra outreach. They only get campaign steps{" "}
                <span className="font-semibold">1, 3, 5, and 7</span> — which this report shows as Phase 1, 2, 3,
                and 4.
              </p>
            </div>
          </div>

          <div className="overflow-x-auto rounded-lg border border-slate-200">
            <table className="w-full text-[12px] min-w-[520px]">
              <thead>
                <tr className="bg-slate-50 text-left text-[10px] font-extrabold uppercase tracking-wider text-slate-400">
                  <th className="px-3 py-2">Campaign step</th>
                  <th className="px-3 py-2">Wait after previous</th>
                  <th className="px-3 py-2">If score ≥ 80%</th>
                  <th className="px-3 py-2">If score &lt; 80%</th>
                  <th className="px-3 py-2">Call</th>
                </tr>
              </thead>
              <tbody>
                {STEPS.map((row) => (
                  <tr key={row.step} className="border-t border-slate-100">
                    <td className="px-3 py-2 font-semibold text-slate-900">{row.step}</td>
                    <td className="px-3 py-2 text-slate-600">{row.wait}</td>
                    <td className="px-3 py-2">{row.ge80}</td>
                    <td className={`px-3 py-2 ${row.lt80 === "Skipped" ? "text-slate-400" : ""}`}>{row.lt80}</td>
                    <td className="px-3 py-2 text-slate-600">{row.call}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="text-[12px] text-slate-500">
            Extra 1 / Extra 2 / Extra 3 and Total Extra on this report only count those in-between nudges for
            80%+ candidates who still have not started.
          </p>

          <div className="border border-slate-200 rounded-lg px-3 py-2.5 space-y-1.5">
            <p className="text-[11px] font-extrabold uppercase tracking-wider text-slate-400">Time zone</p>
            <p>
              Clocks use the <span className="font-semibold">candidate’s local time</span> (the campaign screen
              may label it, for example “India Time”). PAIR guesses the zone from their phone or ZIP when it
              can.
            </p>
            <p>
              We do not SMS or call outside <span className="font-semibold">8:00 AM–8:00 PM</span> in that
              timezone. If a step lands overnight, it waits until morning.
            </p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
