"""scripts/exa_runs_ledger.py: the weekly read-only reconcile of Exa Agent
runs (free GET /agent/runs) against what ExaCall events saw.

Pins: paging stops at the window; only id/status/createdAt/costDollars/usage/
metadata are kept (never the query or the output); a fractional ACU is fine;
grouping by metadata; orphans = billed, finished, unseen, older than the grace.
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "exa_runs_ledger.py"
_spec = importlib.util.spec_from_file_location("exa_runs_ledger", SCRIPT)
ledger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ledger)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def _ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _run(rid, hours_ago, cost, meta=None, status="completed"):
    return {
        "id": rid, "status": status, "createdAt": _ts(hours_ago),
        "costDollars": {"total": cost}, "usage": {"agentComputeUnits": 0.4},
        "metadata": meta or {},
        "request": {"query": "Find the work email for Jane Doe. LinkedIn: https://www.linkedin.com/in/jane"},
        "output": {"structured": {"contact": {"email": "jane@acme.com", "phone": "+14155550100"}}},
    }


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload


class _Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append(dict(params or {}))
        return _Resp(self.pages.pop(0))


CONTACT = {"env": "prod", "surface": "contact", "tier": "deep", "trigger": "launch", "call_id": "c-1"}


def _pages():
    return [
        {"data": [_run("r1", 1, 0.142, CONTACT), _run("r2", 2, 0.025, {**CONTACT, "tier": "normal", "call_id": "c-2"})],
         "hasMore": True, "nextCursor": "p2"},
        {"data": [_run("r3", 0.1, 0.1), _run("r4", 24 * 7.5, 0.025, {**CONTACT, "call_id": "c-4"}),
                  _run("r5", 24 * 9, 5.0)],  # past the 8-day window: not counted, paging stops
         "hasMore": True, "nextCursor": "p3"},
        {"data": [_run("never", 24 * 10, 9.9)], "hasMore": False},
    ]


def test_fetch_pages_to_the_window_and_keeps_no_pii():
    client = _Client(_pages())

    runs = ledger.fetch_runs(client, "k", days=8, now=NOW)

    assert [r["id"] for r in runs] == ["r1", "r2", "r3", "r4"]
    assert client.calls == [{"limit": 100}, {"limit": 100, "cursor": "p2"}]
    dumped = json.dumps(runs)
    assert "Jane" not in dumped and "linkedin" not in dumped and "acme" not in dumped and "4155550100" not in dumped
    assert set(runs[0]) == {"id", "status", "createdAt", "costDollars", "usage", "metadata"}


def test_summary_groups_by_metadata_and_flags_orphans():
    runs = ledger.fetch_runs(_Client(_pages()), "k", days=8, now=NOW)

    report = ledger.summarize(runs, {"c-1", "r2"}, grace_min=30, now=NOW)

    assert report["runs"] == 4 and report["usd"] == 0.292
    groups = {g["group"]: g for g in report["groups"]}
    assert groups["prod/contact/deep/launch/-"]["runs"] == 2
    assert groups["untagged"]["usd"] == 0.1
    # r1 seen by call_id, r2 by run id, r3 is younger than the grace: only r4.
    assert report["orphans"]["runs"] == 1 and report["orphans"]["usd"] == 0.025
    assert report["orphans"]["sample"][0]["id"] == "r4"


def test_a_seen_failed_or_cancelled_run_is_not_an_orphan():
    runs = [
        ledger.slim(_run("f1", 5, 0.03, {**CONTACT, "call_id": "c-f1"}, status="failed")),
        ledger.slim(_run("x1", 5, 0.02, status="cancelled")),
        ledger.slim(_run("f2", 5, 0.03, status="failed")),
    ]

    report = ledger.summarize(runs, {"c-f1", "x1"}, grace_min=30, now=NOW)

    assert [o["id"] for o in report["orphans"]["sample"]] == ["f2"]


def test_the_seen_export_covers_every_terminal_run_outcome():
    # ExaCall records a finished run as completed / run_failed / run_cancelled;
    # an export of 'completed' alone made every billed failed run an orphan.
    nrql = " ".join(ledger.__doc__.split("SELECT uniques(exa_run_id", 1)[1].split("SINCE", 1)[0].split())
    for status in ledger.TERMINAL:
        outcome = "completed" if status == "completed" else f"run_{status}"
        assert f"'{outcome}'" in nrql, outcome


def test_without_seen_ids_there_is_no_orphan_section():
    report = ledger.summarize([], None, now=NOW)
    assert report == {"runs": 0, "usd": 0.0, "group_keys": "env/surface/tier/trigger/arm", "groups": []}


def test_missing_key_exits_2(monkeypatch):
    # An empty value is never overridden by apps/api/.env (load_dotenv override=False).
    monkeypatch.setenv("EXA_API_KEY", "")
    assert ledger.main([]) == 2


# ---------------------------------------------------------------------------
# --seen: NRQL uniques() stops at 10,000, so a cut-off column must not
# become a page of false orphans.
# ---------------------------------------------------------------------------

def _export(run_ids, call_ids):
    # The shape of a New Relic JSON export of the module doc's query.
    return json.dumps([{"uniques.exa_run_id": run_ids, "uniques.call_id": call_ids}], indent=2)


def test_the_seen_export_is_one_day_per_query():
    doc = " ".join(ledger.__doc__.split())
    assert "SINCE 8 days ago UNTIL 7 days ago" in doc and "SINCE 1 day ago" in doc
    assert "10,000" in doc and str(ledger.SEEN_CAP) == "10000"


def test_appended_json_exports_are_read_column_by_column():
    text = _export(["r1", "r2"], ["c-1"]) + "\n" + _export(["r3"], ["c-3", "c-4"])

    columns = ledger.seen_columns(text)

    assert [len(ids) for _, ids in columns] == [2, 1, 1, 2]
    assert columns[0][0] == "export 1 uniques.exa_run_id" and columns[3][0] == "export 2 uniques.call_id"
    assert {i for _, ids in columns for i in ids} == {"r1", "r2", "r3", "c-1", "c-3", "c-4"}
    assert ledger.truncated_columns(columns) == []


def test_a_plain_list_is_split_into_columns_at_blank_lines():
    text = '"r1",\n"r2"\n\n# day 2\nc-1\n  \nr3\n'

    columns = ledger.seen_columns(text)

    assert [ids for _, ids in columns] == [["r1", "r2"], ["c-1"], ["r3"]]


def test_a_column_at_the_uniques_cap_is_truncated():
    full = [f"r{i}" for i in range(ledger.SEEN_CAP)]
    columns = ledger.seen_columns(_export(full, ["c-1"]))
    assert ledger.truncated_columns(columns) == [("export 1 uniques.exa_run_id", 10_000)]

    # Eight days of 2,000 runs each, appended: 16,000 ids, no column at the cap.
    days = "\n".join(_export([f"r{d}-{i}" for i in range(2000)], []) for d in range(8))
    columns = ledger.seen_columns(days)
    assert sum(len(ids) for _, ids in columns) == 16_000 and ledger.truncated_columns(columns) == []


def test_a_truncated_seen_file_exits_2_before_listing_runs(tmp_path, monkeypatch, capsys):
    seen = tmp_path / "seen.json"
    seen.write_text(_export([f"r{i}" for i in range(ledger.SEEN_CAP)], []), encoding="utf-8")
    monkeypatch.setenv("EXA_API_KEY", "k")

    def _no_network(*a, **k):
        raise AssertionError("must not list runs from a truncated export")

    monkeypatch.setattr(ledger, "fetch_runs", _no_network)

    assert ledger.main(["--seen", str(seen)]) == 2
    err = capsys.readouterr().err
    assert "TRUNCATED" in err and "uniques.exa_run_id" in err and "10,000" in err


def test_an_untruncated_seen_file_reports_orphans(tmp_path, monkeypatch, capsys):
    seen = tmp_path / "seen.txt"
    seen.write_text("r1\nr2\n\nc-4\n", encoding="utf-8")
    monkeypatch.setenv("EXA_API_KEY", "k")
    monkeypatch.setattr(
        ledger, "fetch_runs",
        lambda client, key, **kw: [ledger.slim(r) for r in (_run("r1", 48, 0.1), _run("r9", 48, 0.2, CONTACT))],
    )

    assert ledger.main(["--seen", str(seen), "--json"]) == 0
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert [o["id"] for o in report["orphans"]["sample"]] == ["r9"]
    assert "3 ids in 2 column(s)" in out.err
