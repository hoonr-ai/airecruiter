import re

with open("apps/api/routers/candidates.py", "r") as f:
    text = f.read()

# Add _to_iso_z helper at the top
helper = """
def _to_iso_z(dt_val) -> str:
    from datetime import datetime
    if isinstance(dt_val, datetime):
        return dt_val.isoformat() + "Z"
    elif isinstance(dt_val, str) and dt_val and not dt_val.endswith("Z"):
        return dt_val.replace(" ", "T") + "Z"
    return dt_val if isinstance(dt_val, str) else None
"""
if "def _to_iso_z" not in text:
    text = text.replace("router = APIRouter()", "from routers.launch_report import build_merged_outreach_payload\n" + helper + "\nrouter = APIRouter()")

# Remove inside-loop import
text = text.replace("            from routers.launch_report import build_merged_outreach_payload\n", "")

# 1. get_job_candidates
old_dt1 = """            import datetime
            dt_val = cand.get("engage_created_at")
            if isinstance(dt_val, datetime.datetime):
                cand["engage_created_at"] = dt_val.isoformat() + "Z"
            elif isinstance(dt_val, str) and dt_val and not dt_val.endswith("Z"):
                cand["engage_created_at"] = dt_val.replace(" ", "T") + "Z" """
new_dt1 = """            cand["engage_created_at"] = _to_iso_z(cand.get("engage_created_at"))"""
text = text.replace(old_dt1, new_dt1)

old_score1 = """                    f_score = float(raw_score)
                    f_total = float(raw_total)
                    if f_total > 0:
                        norm_engage_score = round((f_score / f_total) * 100, 1)
                        cand["engage_score"] = norm_engage_score"""
new_score1 = """                    try:
                        f_score = float(raw_score)
                        f_total = float(raw_total)
                        if f_total > 0:
                            norm_engage_score = round((f_score / f_total) * 100, 1)
                            cand["engage_score"] = norm_engage_score
                    except (TypeError, ValueError):
                        pass"""
text = text.replace(old_score1, new_score1)

old_avg1 = """            scores_to_avg = [float(r_score)]
            if is_engage_done and cand.get("engage_score") is not None:
                scores_to_avg.append(float(cand["engage_score"]))

            cand["total_fit_score"] = round(sum(scores_to_avg) / len(scores_to_avg), 1)"""
new_avg1 = """            try:
                scores_to_avg = [float(r_score)]
                if is_engage_done and cand.get("engage_score") is not None:
                    scores_to_avg.append(float(cand["engage_score"]))

                cand["total_fit_score"] = round(sum(scores_to_avg) / len(scores_to_avg), 1)
            except (TypeError, ValueError):
                pass"""
text = text.replace(old_avg1, new_avg1)

# 2. Master Pool CTE fix
old_cte = "SELECT DISTINCT ON (lookup_id) lookup_id, title, screening_level"
new_cte = "SELECT DISTINCT ON (lookup_id) lookup_id, title, screening_level, recruiter_emails"
text = text.replace(old_cte, new_cte)


# 3. get_candidate_evaluation_report
old_dt3 = """            "engage_completed_at":   engage_completed_at.isoformat() + "Z" if isinstance(engage_completed_at, datetime) else (str(engage_completed_at).replace(" ", "T") + "Z" if engage_completed_at else None),
            "engage_created_at":     engage_created_at.isoformat() + "Z" if isinstance(engage_created_at, datetime) else (str(engage_created_at).replace(" ", "T") + "Z" if engage_created_at else None),"""
new_dt3 = """            "engage_completed_at":   _to_iso_z(engage_completed_at),
            "engage_created_at":     _to_iso_z(engage_created_at),"""
text = text.replace(old_dt3, new_dt3)

with open("apps/api/routers/candidates.py", "w") as f:
    f.write(text)
