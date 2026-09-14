with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# 1. Update widths of headers
headers_to_replace = {
    'className="w-[160px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"': 'className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"',
    'className="w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"': 'className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"',
    'className="w-[200px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"': 'className="w-[240px] min-w-[240px] max-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"',
    'className="w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"': 'className="w-[260px] min-w-[260px] max-w-[260px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200"',
}
for old_str, new_str in headers_to_replace.items():
    text = text.replace(old_str, new_str)

# 2. Update formatRecruiterEmails
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

helper_new = """const getRecruiterEmailsArray = (emails: string | string[] | null | undefined): string[] => {
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
text = text.replace('formatRecruiterEmails(c.recruiter_emails)', 'getRecruiterEmailsArray(c.recruiter_emails).join(", ")')

# 4. Fix UI Cell
cell_old = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[300px] px-3">
                        {c.recruiter_emails && formatRecruiterEmails(c.recruiter_emails) ? (
                          <div className="flex items-center justify-center w-full overflow-x-auto scrollbar-thin scrollbar-thumb-slate-200 pb-1">
                            <span className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center">
                              {formatRecruiterEmails(c.recruiter_emails)}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>"""

cell_new = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 min-w-[300px] max-w-[300px] px-3 align-top py-4">
                        {c.recruiter_emails && getRecruiterEmailsArray(c.recruiter_emails).length > 0 ? (
                          <div className="flex flex-col items-center justify-start w-full gap-1">
                            {getRecruiterEmailsArray(c.recruiter_emails).map((email, i) => (
                              <span key={i} className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center bg-slate-50 px-2 py-0.5 rounded-md border border-slate-100 max-w-full overflow-hidden text-ellipsis" title={email}>
                                {email}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <div className="flex items-center justify-center h-full">
                            <span className="text-slate-400 italic text-[11px]">N/A</span>
                          </div>
                        )}
                      </TableCell>"""

text = text.replace(cell_old, cell_new)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)

