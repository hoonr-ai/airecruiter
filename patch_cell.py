with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

cell_old = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[300px] px-3">
                        {c.recruiter_emails && getRecruiterEmailsArray(c.recruiter_emails).join(", ") ? (
                          <div className="flex items-center justify-center w-full overflow-x-auto scrollbar-thin scrollbar-thumb-slate-200 pb-1">
                            <span className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center">
                              {getRecruiterEmailsArray(c.recruiter_emails).join(", ")}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400 italic text-[11px]">N/A</span>
                        )}
                      </TableCell>"""

cell_new = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 min-w-[300px] max-w-[300px] px-3 align-top py-4">
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

text = text.replace(cell_old, cell_new)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)
