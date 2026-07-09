# Additive Gender Logic (No Existing Code Changes)

This repository now includes an isolated helper module for gender label normalization
and PAIR payload enrichment using canonical labels:

- male
- female
- default

Back-compat inputs accepted and normalized:

- else -> default
- unknown/other/unspecified -> default

## New Module

- `apps/api/services/gender_logic.py`

## What It Provides

1. `normalize_gender_prediction(...)`
- Normalizes arbitrary model output to `male|female|default`
- Applies confidence threshold
- Forces `default` for unsupported labels or low confidence

2. `resolve_gender_with_priority(...)`
- Self-declared value wins (if valid)
- Falls back to inferred prediction

3. `enrich_pair_resumes_payload(...)`
- Adds these fields to each resume object in PAIR payload:
  - `gender_label`
  - `gender_confidence`
  - `gender_source`
  - `gender_updated_at`

## Example Usage (when wiring later)

```python
from services.gender_logic import enrich_pair_resumes_payload

payload["resumes"] = enrich_pair_resumes_payload(
    payload.get("resumes", []),
    gender_by_candidate_id={
        "123": {
            "gender_label": "else",  # normalized to default
            "gender_confidence": 0.42,
            "gender_source": "inferred",
        }
    },
)
```

## Important

This is intentionally additive only:

- No existing files were edited
- No existing runtime logic was changed
- Module is ready for later wiring into `generate-payload`, search stream, and report paths
