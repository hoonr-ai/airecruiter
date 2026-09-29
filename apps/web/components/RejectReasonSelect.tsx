import React from 'react';

export const REJECTION_REASONS = [
  "Skills do not meet requirements",
  "Communication skills",
  "Domain experience mismatch",
  "More qualified candidates identified",
  "Overqualified for the role",
  "Compensation expectations exceed budget",
  "Not aligned with employment type (W2 / C2C / 1099)",
  "Work authorization / visa constraints",
  "Not comfortable with background check / drug test",
  "Not local and not open to relocation",
  "Open to remote only",
  "Not available within required timeline",
  "Accepted another offer",
  "Candidate withdrew interest",
  "Career gap concern",
  "Job Hopping (short-term engagements throughout or in the last 5-7 years)",
  "Fake candidate — Multiple profiles/resumes; misrepresentation of past experience",
  "Already submitted to same client / hiring manager by another vendor",
  "Previously rejected by client",
  "Not eligible for rehire",
  "Past performance concern (Internal note as per past Pyramid client feedback)",
  "Candidate does not want to work with the same client",
];

interface RejectReasonSelectProps {
  rejectReason: string;
  setRejectReason: (val: string) => void;
  otherRejectText: string;
  setOtherRejectText: (val: string) => void;
}

export function RejectReasonSelect({
  rejectReason,
  setRejectReason,
  otherRejectText,
  setOtherRejectText,
}: RejectReasonSelectProps) {
  return (
    <>
      <div className="space-y-2">
        <label htmlFor="rejectReasonSelect" className="text-xs font-bold text-slate-500 uppercase tracking-widest">
          Rejection Reason
        </label>
        <select
          id="rejectReasonSelect"
          className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50"
          value={rejectReason}
          onChange={e => { setRejectReason(e.target.value); setOtherRejectText(''); }}
          aria-label="Rejection Reason"
        >
          <option value="" disabled>Select a reason...</option>
          {REJECTION_REASONS.map((reason) => (
            <option key={reason} value={reason}>{reason}</option>
          ))}
          <option value="__other__">Other (specify below)</option>
        </select>
      </div>
      {rejectReason === "__other__" && (
        <div className="space-y-2">
          <label htmlFor="otherRejectText" className="text-xs font-bold text-slate-500 uppercase tracking-widest">
            Please specify
          </label>
          <textarea
            id="otherRejectText"
            className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50 resize-none"
            rows={3}
            placeholder="Enter your rejection reason..."
            value={otherRejectText}
            onChange={e => setOtherRejectText(e.target.value)}
            maxLength={500}
            autoFocus
            aria-label="Please specify rejection reason"
          />
          <div className="flex justify-between items-center text-xs text-slate-400">
            <span>Visible in JobDiva notes – avoid sensitive personal data.</span>
            <span>{otherRejectText.length}/500</span>
          </div>
        </div>
      )}
    </>
  );
}
