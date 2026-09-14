with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# 1. Fix getRecruiterEmailsArray
old_helper = """const getRecruiterEmailsArray = (emails: string | string[] | null | undefined): string[] => {
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

new_helper = """const getRecruiterEmailsArray = (emails: string | string[] | null | undefined): string[] => {
  if (!emails) return [];
  if (Array.isArray(emails)) return emails;
  if (typeof emails === "string") {
    try {
      const parsed = JSON.parse(emails);
      if (Array.isArray(parsed)) return parsed.map(String);
      if (typeof parsed === "string") return [parsed]; // Fix: handle JSON-quoted single strings without falling through
    } catch (e) {
      // ignore
    }
    return emails.split(",").map(s => s.trim()).filter(Boolean);
  }
  return [String(emails)];
};"""
text = text.replace(old_helper, new_helper)

# 2. Add parsedRecruiterEmails variable
old_map = """                candidates.map((c, i) => {
                  const statusInfo = normalizeInterviewStatus(c.engage_status);
                  const resumeScore = Math.round(c.match_score || 0);

                  return ("""

new_map = """                candidates.map((c, i) => {
                  const statusInfo = normalizeInterviewStatus(c.engage_status);
                  const resumeScore = Math.round(c.match_score || 0);
                  const parsedRecruiterEmails = getRecruiterEmailsArray(c.recruiter_emails); // Cache parsed emails once per row

                  return ("""
text = text.replace(old_map, new_map)

# 3. Use parsedRecruiterEmails, avoid `i` shadowing, use `email` as key
old_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 min-w-[300px] max-w-[300px] px-3 align-top py-4">
                        {c.recruiter_emails && getRecruiterEmailsArray(c.recruiter_emails).length > 0 ? (
                          <div className="flex flex-col items-center justify-center w-full h-full gap-2 py-1">
                            {getRecruiterEmailsArray(c.recruiter_emails).map((email, i) => (
                              <span key={i} className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center bg-slate-50 px-2.5 py-1 rounded-md border border-slate-100 max-w-full overflow-hidden text-ellipsis shadow-sm" title={email}>
                                {email}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <div className="flex items-center justify-center h-full w-full">
                            <span className="text-slate-400 italic text-[11px]">N/A</span>
                          </div>
                        )}
                      </TableCell>"""

new_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 min-w-[300px] max-w-[300px] px-3 align-top py-4">
                        {parsedRecruiterEmails.length > 0 ? (
                          <div className="flex flex-col items-center justify-center w-full h-full gap-2 py-1">
                            {parsedRecruiterEmails.map((email) => (
                              <span key={email} className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center bg-slate-50 px-2.5 py-1 rounded-md border border-slate-100 max-w-full overflow-hidden text-ellipsis shadow-sm" title={email}>
                                {email}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <div className="flex items-center justify-center h-full w-full">
                            <span className="text-slate-400 italic text-[11px]">N/A</span>
                          </div>
                        )}
                      </TableCell>"""
text = text.replace(old_cell, new_cell)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)

