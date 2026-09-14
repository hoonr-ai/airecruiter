import os

filepath = "apps/web/app/candidates/page.tsx"
with open(filepath, "r") as f:
    content = f.read()

# Fix 1: capitalize when setting pageFeedbacks
old_code = """          if (c.data?.feedback_type) pageFeedbacks[c.id] = c.data.feedback_type;"""
new_code = """          if (c.data?.feedback_type) {
            const raw = c.data.feedback_type.trim();
            const lower = raw.toLowerCase();
            if (lower.startsWith("reject")) pageFeedbacks[c.id] = "Reject";
            else if (lower === "submit" || lower === "submitted") pageFeedbacks[c.id] = "Submit";
            else if (lower === "unreachable") pageFeedbacks[c.id] = "Unreachable";
            else pageFeedbacks[c.id] = raw;
          }"""

if old_code in content:
    content = content.replace(old_code, new_code)
else:
    print("Could not find old_code")

with open(filepath, "w") as f:
    f.write(content)
print("Done patching.")
