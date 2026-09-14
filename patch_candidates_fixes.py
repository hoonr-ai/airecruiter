with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

# 1. JOB DIVA ID -> JOBDIVA ID
text = text.replace("JOB DIVA ID", "JOBDIVA ID")

# 2. Search bar width
text = text.replace('className="relative w-full sm:max-w-md"', 'className="relative w-full sm:max-w-xl"')

# 3. Date filter text
old_date_filter = """<span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider shrink-0 w-[80px]">Launched</span>"""
new_date_filter = """<span className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider shrink-0 w-[95px]">Launched From</span>"""
text = text.replace(old_date_filter, new_date_filter)

# For the TO part
old_date_to = """<span className="text-[11px] font-bold text-slate-300 uppercase tracking-wider mx-2">to</span>"""
new_date_to = """<span className="text-[11px] font-bold text-slate-300 uppercase tracking-wider mx-1">to</span>"""
text = text.replace(old_date_to, new_date_to)

# 4. Add SCREENING_LEVEL_STYLES definition
styles = """
const SCREENING_LEVEL_STYLES: Record<string, string> = {
  "l0.5": "bg-gray-100 text-gray-600 border-gray-300",
  "l1":   "bg-blue-50 text-blue-700 border-blue-200",
  "l1.5": "bg-teal-50 text-teal-700 border-teal-200",
  "l2":   "bg-purple-50 text-purple-700 border-purple-200",
};
"""
# insert near top after imports
text = text.replace("const getRecruiterEmailsArray", styles + "\nconst getRecruiterEmailsArray")

# 5. Add SCREENING LEVEL column
header_old = '<TableHead className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">LAUNCHED DATE</TableHead>'
header_new = '<TableHead className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">LAUNCHED DATE</TableHead>\n                <TableHead className="w-[180px] min-w-[180px] max-w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">SCREENING LEVEL</TableHead>'
text = text.replace(header_old, header_new)

# Table body cell
cell_old = """                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-600 text-[12px]">
                        {c.engage_created_at ? formatDate(c.engage_created_at) : <span className="text-slate-400 italic">N/A</span>}
                      </TableCell>"""
cell_new = """                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-600 text-[12px]">
                        {c.engage_created_at ? formatDate(c.engage_created_at) : <span className="text-slate-400 italic">N/A</span>}
                      </TableCell>
                      
                      <TableCell className="border-b border-slate-200 text-center font-medium border-l border-slate-200">
                        {c.screening_level ? (
                          <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-bold border uppercase ${SCREENING_LEVEL_STYLES[c.screening_level.toLowerCase()] ?? "bg-gray-100 text-gray-600 border-gray-300"}`}>
                            {c.screening_level}
                          </span>
                        ) : (
                          <span className="text-slate-400 text-xs italic">N/A</span>
                        )}
                      </TableCell>"""
text = text.replace(cell_old, cell_new)

# CSV export (add screening level)
csv_header_old = '"Source","Launched Date","Resume Screening Score","Engage Status","Engage Score","Total Fit Score","Recruiter Email"'
csv_header_new = '"Source","Launched Date","Screening Level","Resume Screening Score","Engage Status","Engage Score","Total Fit Score","Recruiter Email"'
text = text.replace(csv_header_old, csv_header_new)

csv_row_old = """        escapeCsvField(c.source),
        escapeCsvField(c.engage_created_at ? new Date(c.engage_created_at).toLocaleString() : "N/A"),
        escapeCsvField(resumeScoreStr),"""
csv_row_new = """        escapeCsvField(c.source),
        escapeCsvField(c.engage_created_at ? new Date(c.engage_created_at).toLocaleString() : "N/A"),
        escapeCsvField(c.screening_level || "N/A"),
        escapeCsvField(resumeScoreStr),"""
text = text.replace(csv_row_old, csv_row_new)

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)

