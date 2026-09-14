with open("apps/web/app/jobs/[jobId]/rankings/page.tsx", "r") as f:
    text = f.read()

# Add border-b to TableRow
text = text.replace('<TableRow key={cand.id} className="group hover:bg-slate-50 transition-colors cursor-default">', '<TableRow key={cand.id} className="group hover:bg-slate-50 transition-colors cursor-default border-b border-slate-200">')
# Also for the unranked TableRow
text = text.replace('<TableRow key={cand.id} className="group hover:bg-slate-50/50 transition-colors cursor-default">', '<TableRow key={cand.id} className="group hover:bg-slate-50/50 transition-colors cursor-default border-b border-slate-200">')

# Add border-b to TableCell in mappings
# This might be tricky because there are multiple <TableCell classes. 
# We can do a blanket replace if we're careful.
text = text.replace('<TableCell className="', '<TableCell className="border-b border-slate-200 ')
text = text.replace('<TableHead className="border-b border-slate-200 ', '<TableHead className="') # Undo for headers

with open("apps/web/app/jobs/[jobId]/rankings/page.tsx", "w") as f:
    f.write(text)

