with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# 1. Update headers widths
# SOURCE: w-[160px] -> w-[220px]
text = text.replace('className="w-[160px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"', 'className="w-[220px] min-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"')
# LAUNCHED DATE: w-[180px] -> w-[220px]
text = text.replace('className="w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"', 'className="w-[220px] min-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"')
# RESUME SCREENING SCORE: w-[200px] -> w-[240px]
text = text.replace('className="w-[200px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RESUME SCREENING SCORE', 'className="w-[240px] min-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RESUME SCREENING SCORE')
# ENGAGE STATUS: w-[200px] -> w-[240px]
text = text.replace('className="w-[200px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE STATUS', 'className="w-[240px] min-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE STATUS')
# ENGAGE SCORE: w-[200px] -> w-[240px]
text = text.replace('className="w-[200px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE SCORE', 'className="w-[240px] min-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE SCORE')
# TOTAL FIT SCORE: w-[220px] -> w-[260px]
text = text.replace('className="w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">TOTAL FIT SCORE', 'className="w-[260px] min-w-[260px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">TOTAL FIT SCORE')
# CANDIDATE FEEDBACK: w-[220px] -> w-[260px]
text = text.replace('className="w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">CANDIDATE FEEDBACK', 'className="w-[260px] min-w-[260px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">CANDIDATE FEEDBACK')

# 2. Update formatRecruiterEmails to return an array of strings
helper_old = """const formatRecruiterEmails = (emails: string | string[] | null | undefined): string => {
  if (!emails) return "";
  if (Array.isArray(emails)) return emails.join(", ");
  if (typeof emails === "string") {
    try {
      const parsed = JSON.parse(emails);
      if (Array.isArray(parsed)) return parsed.join(", ");
    } catch (e) {
      // ignore
    }
  }
  return String(emails);
};"""

helper_new = """const formatRecruiterEmailsArray = (emails: string | string[] | null | undefined): string[] => {
  if (!emails) return [];
  if (Array.isArray(emails)) return emails;
  if (typeof emails === "string") {
    try {
      const parsed = JSON.parse(emails);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch (e) {
      // ignore
    }
    return emails.split(",").map(s => s.trim()).filter(Boolean);
  }
  return [String(emails)];
};"""

text = text.replace(helper_old, helper_new)

# 3. Fix CSV Export
text = text.replace('formatRecruiterEmails(c.recruiter_emails)', 'formatRecruiterEmailsArray(c.recruiter_emails).join(", ")')

# 4. Fix UI Cell
cell_old = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[300px] px-3">
                        {c.recruiter_emails && formatRecruiterEmailsArray(c.recruiter_emails).join(", ") ? (
                          <div className="flex items-center justify-center w-full overflow-x-auto scrollbar-thin scrollbar-thumb-slate-200 pb-1">
                            <span className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center">
                              {formatRecruiterEmailsArray(c.recruiter_emails).join(", ")}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>"""

# Actually, the string replacement for `formatRecruiterEmails` just replaced the CSV *and* the cell logic above because I did a blind replace. Let me undo the global replace first.

