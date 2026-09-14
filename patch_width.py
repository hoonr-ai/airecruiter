with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# Fix header
old_head = '                <TableHead className="w-[180px] min-w-[180px] max-w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RECRUITER EMAIL</TableHead>'
new_head = '                <TableHead className="w-[300px] min-w-[300px] max-w-[300px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RECRUITER EMAIL</TableHead>'
text = text.replace(old_head, new_head)

# Fix cell
old_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[180px] px-3">
                        {c.recruiter_emails && formatRecruiterEmails(c.recruiter_emails) ? (
                          <div className="flex items-center justify-center w-full">
                            <span className="inline-block break-words whitespace-normal leading-relaxed text-[11.5px] text-slate-500 w-full text-center">"""

new_cell = """                      <TableCell className="text-center font-medium text-slate-600 text-[12px] border-l border-slate-200 max-w-[300px] px-3">
                        {c.recruiter_emails && formatRecruiterEmails(c.recruiter_emails) ? (
                          <div className="flex items-center justify-center w-full overflow-x-auto scrollbar-thin scrollbar-thumb-slate-200 pb-1">
                            <span className="inline-block whitespace-nowrap leading-relaxed text-[11.5px] text-slate-500 text-center">"""
text = text.replace(old_cell, new_cell)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)
