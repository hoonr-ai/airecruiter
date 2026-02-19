# JobDiva Search Query Construction Analysis

## Current Logic (from services/jobdiva.py)

### Skill Processing:
1. **Must Have Skills** → Added with AND logic: `("SQL" AND "Java")`
2. **Nice to Have Skills** → Added with OR logic: `("Python" OR "React")`

### Seniority → Years Mapping:
- **Senior**: 5 years → `("SQL" RECENT OVER 5 YRS)`
- **Mid**: 3 years → `("SQL" RECENT OVER 3 YRS)`
- **Junior**: 0 years → `"SQL"` (no years filter)

### Final Query Construction:
```
IF must_haves:
    criteria = (must_have_1 AND must_have_2 AND ...) 
    # Note: Flexible skills are NOT added to avoid excluding valid candidates
ELIF flexible_skills:
    criteria = (flexible_1 OR flexible_2 OR ...)
    
IF location:
    criteria = criteria AND "Location"

Final: keywords = criteria
```

## Example Query:

**Input:**
- Skills: SQL (Must Have, Mid), Java (Must Have, Senior), Python (Nice to Have)
- Location: Atlanta

**Generated Query:**
```
(("SQL" RECENT OVER 3 YRS) AND ("Java" RECENT OVER 5 YRS)) AND "Atlanta"
```

**Note:** Python is ignored because it's Nice to Have and there are Must Haves present.

## Current Issues:

1. **Location Filter**: Currently adds location with AND, which might be too restrictive if Remote is desired
2. **Flexible Skills Ignored**: When Must Haves exist, flexible skills are completely ignored (by design to avoid over-filtering)
3. **Years Experience**: Always applied based on seniority, even if user wants broader results

## What specific mismatch are you seeing?
- Too many results?
- Too few results?
- Wrong skills?
- Wrong experience levels?
