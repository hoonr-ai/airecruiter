import re

with open("apps/web/lib/campaigns.ts", "r") as f:
    content = f.read()

# Fix first conflict block
content = re.sub(
    r"<<<<<<< Updated upstream\n    const isLocked = q.text.includes\(\"authorized to work indefinitely\"\) \|\|\n                     q.text.includes\(\"require visa sponsorship to continue working\"\) \|\|\n                     q.text.includes\(\"types of working arrangements are you open to and eligible for\"\);\n=======\n>>>>>>> Stashed changes",
    "",
    content
)
content = re.sub(
    r"<<<<<<< Updated upstream\n      is_locked: isLocked,\n=======\n      is_locked: isLockedDefaultQuestion\(q.text\),\n>>>>>>> Stashed changes",
    "      is_locked: isLockedDefaultQuestion(q.text),",
    content
)

with open("apps/web/lib/campaigns.ts", "w") as f:
    f.write(content)
