import re

with open("apps/web/lib/campaigns.ts", "r") as f:
    content = f.read()

helper = """
export function resolveLockedFlag(q: any): boolean {
  return !!q.is_locked || isLockedDefaultQuestion(q.question_text);
}
"""
content = content.replace("export function isLockedDefaultQuestion", helper + "\nexport function isLockedDefaultQuestion")

with open("apps/web/lib/campaigns.ts", "w") as f:
    f.write(content)
