with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

helper = """
const formatRecruiterEmails = (emails: string | string[] | null | undefined): string => {
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
};
"""

text = text.replace("export default function GlobalCandidatesPage() {", helper + "\nexport default function GlobalCandidatesPage() {")

# Fix CSV 1
text = text.replace('escapeCsvField(c.recruiter_emails || "N/A")', 'escapeCsvField(formatRecruiterEmails(c.recruiter_emails) || "N/A")')

# Fix UI Cell
old_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[180px] px-3">
                        {c.recruiter_emails ? (
                          <div className="flex items-center justify-center w-full">
                            <span className="inline-block break-words whitespace-normal leading-relaxed text-[11.5px] text-slate-500 w-full text-center">
                              {Array.isArray(c.recruiter_emails) ? c.recruiter_emails.join(", ") : c.recruiter_emails}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>"""

new_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[180px] px-3">
                        {c.recruiter_emails && formatRecruiterEmails(c.recruiter_emails) ? (
                          <div className="flex items-center justify-center w-full">
                            <span className="inline-block break-words whitespace-normal leading-relaxed text-[11.5px] text-slate-500 w-full text-center">
                              {formatRecruiterEmails(c.recruiter_emails)}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>"""

text = text.replace(old_cell, new_cell)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)
