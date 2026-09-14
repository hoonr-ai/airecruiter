with open("apps/web/app/candidates/page.tsx", "r") as f:
    text = f.read()

text = text.replace('if (!s) return "—";', 'if (!s) return "Unknown";')

with open("apps/web/app/candidates/page.tsx", "w") as f:
    f.write(text)

with open("apps/web/app/jobs/[jobId]/rankings/page.tsx", "r") as f:
    text2 = f.read()

text2 = text2.replace('if (!s) return "—";', 'if (!s) return "Unknown";')

with open("apps/web/app/jobs/[jobId]/rankings/page.tsx", "w") as f:
    f.write(text2)

