# Org hierarchy — who sees whose jobs and admin analytics

```
Recruiter → Resource Manager → Delivery Manager → Delivery Director → AVP
```

Everyone sees their own jobs. From **Resource Manager up**, a person also sees the
jobs of everyone beneath them (any depth) and gets the **Dashboard, Team/Admin
Analytics, Recruiter Analytics and Launch Report** scoped to that same population.
Nobody sees sideways or upwards. Admins still see everything.

Code: `services/org_hierarchy.py` (tree, importer), `routers/org_hierarchy.py`
(admin API), `core/auth.py` (scope), `routers/_helpers.py::_load_team_scope`
(reports). Admin UI: **Org Hierarchy** in the sidebar (`/admin/hierarchy`).

## How access is decided

`core.auth.get_user_scope_emails(user)` is the one definition of "whose jobs can
this person see": themself, plus the team they *lead* (Teams page), plus everyone
beneath them in the hierarchy. The jobs list and `verify_job_access` use it
directly; the reports use `report_scope_parts` (the same two sources) as a scope
key, so a manager's jobs and analytics always describe the same people.

- A manager resolves to the existing `team_lead` role, so every page already gated
  on it admits them. `/api/v1/auth/me` adds `org_role`, `org_role_label` and
  `manages_people` for labels.
- Scope keys: `org:<member id>` = that person and everyone beneath them; a bare id
  = a Teams-page team; several joined with `+` are a union (a manager who also leads
  a team). Non-admins are always pinned to their own key; a `team_id` they send is
  ignored. Admins may pass any key (e.g. `?team_id=org:12` to see what someone sees).
- The hierarchy and Teams **coexist**. Scope is the union, so introducing the
  hierarchy never removes access anyone had. Belonging to a team is not leading it
  (`UserIdentity.leads_team`): a manager who is only a team *member* is not handed
  that team's roster.
- **Seeing a job means being able to act on it** (launch, edit, …) — the same as
  team leads today. `verify_job_access` guards both. A read-only manager role would
  need that check split across the job-scoped endpoints.
- Anything else gated on `is_team_lead` now also admits managers: the opt-out status
  lookup without a job id (`outreach_optout`) and the live-report health strip.

## Loading the tree: the mapping sheet

Admin → Org Hierarchy → upload a CSV. **Every upload replaces the whole tree**
(one transaction; a failed import changes nothing). The upload previews first — people
per level, who still needs an email, what would be removed, and how many of the
sheet's emails are on a job today (a low number means the emails are not the ones
JobDiva uses).

Columns (spelling/punctuation ignored): `Email. ID, Name, Positions,
Reporting Manager, Regional/Vertical Structure, Regional/Vertical Head`.

| Cell | Meaning |
|---|---|
| Positions | `Recruiter`, `Resource Manager`, `Delivery Manager`, `Delivery Director`, `AVP` (a few synonyms). **Anything else is a Recruiter** — least privilege; a typo can hide a team, never widen access. The preview lists such titles. |
| Reporting Manager | The person's manager, by **email** if the cell holds one (unambiguous) or by **name** (case/spacing ignored). A manager equal to the person themself means "none stated". A manager must be a level *above* the report or the link is ignored with a warning. |
| Regional/Vertical Head | Fallback manager when none is stated, and the manager of any Reporting Manager the sheet has no row for. |
| Email | Optional. A person without one stays in the tree (it keeps their reports grouped) but sees nothing until one is added. Invalid emails are treated as blank, never guessed. |

A manager or head the sheet names but gives no row for is created without an email:
a Reporting Manager as a Resource Manager; a Regional/Vertical Head as a **Delivery
Director by default** — the sheet does not say, so the upload has a selector for it.
Above Resource Manager, add a row for each Delivery Manager / Director / AVP with that
Positions value, and point their reports' Reporting Manager at them.

**Filling gaps:** Download CSV exports the current tree in this same format (blank
Email cells for the people still needing one). Fill them in — or add rows for the
higher levels — and upload it again; it round-trips to the identical tree.

## Failure behaviour

- Inert until the first import: with no rows, nobody's access changes.
- Identity lookups fail **closed**: if `org_members` can't be read (e.g. startup schema
  init timed out) a person simply gets no extra visibility, never more.
- Ranks must strictly increase up the tree, so cycles cannot exist.

## Tests

`tests/test_org_hierarchy.py` (importer + scope maths, pure),
`tests/test_org_hierarchy_access.py` (identity, scope, report keys, routes),
`tests/test_org_hierarchy_postgres.py` (the SQL on a real Postgres; needs
`pip install pgserver`, skipped otherwise).
