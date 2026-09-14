with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# 1. Remove old header
old_h = '                <TableHead className="w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RECRUITER EMAIL</TableHead>\n'
text = text.replace(old_h, "")

# 2. Insert new header after CANDIDATE NAME
cand_name_h = '                <TableHead className="w-[300px] min-w-[300px] max-w-[300px] sticky left-[370px] z-30 bg-slate-50 text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200 shadow-[2px_0_5px_-2px_rgba(0,0,0,0.05)]">CANDIDATE NAME</TableHead>\n'
new_rec_h = '                <TableHead className="w-[180px] min-w-[180px] max-w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RECRUITER EMAIL</TableHead>\n'
text = text.replace(cand_name_h, cand_name_h + new_rec_h)

# 3. Remove old cell
old_c = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200">
                        {c.recruiter_emails ? (
                          <span className="block truncate max-w-[200px]" title={Array.isArray(c.recruiter_emails) ? c.recruiter_emails.join(", ") : c.recruiter_emails}>
                            {Array.isArray(c.recruiter_emails) ? c.recruiter_emails.join(", ") : c.recruiter_emails}
                          </span>
                        ) : (
                          <span className="text-slate-400 italic">N/A</span>
                        )}
                      </TableCell>
"""
text = text.replace(old_c, "")

# 4. Insert new cell after CANDIDATE NAME
cand_name_c = """                            </button>
                          </div>
                        </div>
                      </TableCell>
"""

new_rec_c = """
                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[180px] px-3">
                        {c.recruiter_emails ? (
                          <div className="flex items-center justify-center w-full">
                            <span className="inline-block break-words whitespace-normal leading-relaxed text-[11.5px] text-slate-500 w-full text-center">
                              {Array.isArray(c.recruiter_emails) ? c.recruiter_emails.join(", ") : c.recruiter_emails}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>
"""
text = text.replace(cand_name_c, cand_name_c + new_rec_c)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)
