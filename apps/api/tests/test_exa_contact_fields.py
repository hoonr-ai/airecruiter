"""Tests for the Exa Agent contact-enrichment parser + helper gating.

Covers extract_exa_contact_fields (work/personal classification, phone
normalisation, shape variants, None/empty) and the flag-gating of
exa_enrich_by_linkedin (no-op when disabled / no key — never hits the network).

Standalone script — same stubbing strategy as test_exa_query_builder.
Run with:
    cd apps/api && python -m tests.test_exa_contact_fields
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stub_module(name: str, **attrs) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod


# Stub the heavy deps so importing services.contact_enrichment needs no env/.env.
_stub_module("httpx")
_stub_module("core", __path__=[])
_stub_module(
    "core.config",
    APOLLO_API_KEY="",
    EXA_API_KEY="",
    EXA_CONTACT_ENRICH_EFFORT="low",
    EXA_CONTACT_ENRICH_ENABLED=False,
    EXA_CONTACT_ENRICH_TIMEOUT_S=60,
    ZOOMINFO_BEARER_TOKEN="",
    ZOOMINFO_CLIENT_ID="",
    ZOOMINFO_ENRICH_URL="",
    ZOOMINFO_CLIENT_SECRET="",
    ZOOMINFO_OAUTH_TOKEN_URL="",
    ZOOMINFO_SCOPES="api:data:contact",
)

from services import contact_enrichment as ce  # noqa: E402

extract_exa_contact_fields = ce.extract_exa_contact_fields

EMPTY = {
    "mobilePhone": "",
    "workPhone": "",
    "workEmail": "",
    "personalEmail": "",
    "phoneCandidates": [],
}


def _check(name: str, cond: bool, detail: str = "") -> int:
    if cond:
        print(f"  PASS: {name}")
        return 0
    print(f"  FAIL: {name} {('-> ' + detail) if detail else ''}")
    return 1


def test_parser() -> int:
    f = 0

    # work email (corporate domain) + messy phone normalises to +digits
    out = extract_exa_contact_fields({"contact": {"email": "jane@acme.com", "phone": "+1 (415) 555-0100"}})
    f += _check("work email classified to workEmail", out["workEmail"] == "jane@acme.com" and out["personalEmail"] == "", str(out))
    f += _check("phone normalised to mobilePhone", out["mobilePhone"] == "+14155550100", str(out))
    f += _check("phone added to phoneCandidates", out["phoneCandidates"] == ["+14155550100"], str(out))
    f += _check("workPhone stays empty (agent has one phone)", out["workPhone"] == "", str(out))

    # consumer-domain email → personalEmail; no phone
    out = extract_exa_contact_fields({"contact": {"email": "jane.doe@gmail.com", "phone": ""}})
    f += _check("gmail classified to personalEmail", out["personalEmail"] == "jane.doe@gmail.com" and out["workEmail"] == "", str(out))
    f += _check("no phone => empty candidates", out["phoneCandidates"] == [], str(out))

    # phone-only, no leading + → kept without plus
    out = extract_exa_contact_fields({"contact": {"phone": "415-555-0100"}})
    f += _check("plain phone kept", out["mobilePhone"] == "4155550100" and out["phoneCandidates"] == ["4155550100"], str(out))

    # too-short phone (<7 digits) dropped
    out = extract_exa_contact_fields({"contact": {"phone": "12345"}})
    f += _check("short phone dropped", out["mobilePhone"] == "" and out["phoneCandidates"] == [], str(out))

    # flat shape (no "contact" wrapper)
    out = extract_exa_contact_fields({"email": "x@corp.io", "phone": "+12025550173"})
    f += _check("flat shape parsed", out["workEmail"] == "x@corp.io" and out["mobilePhone"] == "+12025550173", str(out))

    # None / empty / contact=None
    f += _check("None -> all empty", extract_exa_contact_fields(None) == EMPTY, str(extract_exa_contact_fields(None)))
    f += _check("{} -> all empty", extract_exa_contact_fields({}) == EMPTY, str(extract_exa_contact_fields({})))
    f += _check("contact=None -> all empty", extract_exa_contact_fields({"contact": None}) == EMPTY, str(extract_exa_contact_fields({"contact": None})))
    return f


def test_helper_gating() -> int:
    f = 0

    # Disabled (default) → immediate no-op, no network.
    ce.EXA_CONTACT_ENRICH_ENABLED = False
    res = asyncio.run(ce.exa_enrich_by_linkedin("cand1", "https://linkedin.com/in/x", "Jane", "Acme"))
    f += _check("disabled -> ok=False", res.get("ok") is False, str(res))

    # Enabled but no API key → no-op, no network.
    ce.EXA_CONTACT_ENRICH_ENABLED = True
    ce.EXA_API_KEY = ""
    res = asyncio.run(ce.exa_enrich_by_linkedin("cand1", "https://linkedin.com/in/x", "Jane", "Acme"))
    f += _check("enabled+no key -> ok=False", res.get("ok") is False, str(res))

    # restore defaults
    ce.EXA_CONTACT_ENRICH_ENABLED = False
    return f


def test_zoominfo_email_gating() -> int:
    f = 0
    # No email → immediate no-op, no network.
    res = asyncio.run(ce.zoominfo_enrich_by_email("c1", ""))
    f += _check("empty email -> ok=False", res.get("ok") is False, str(res))
    # Email present but OAuth not configured (stub has empty secret/url) → no-op.
    res = asyncio.run(ce.zoominfo_enrich_by_email("c1", "x@corp.io"))
    f += _check("no OAuth creds -> ok=False", res.get("ok") is False, str(res))
    return f


def test_query_builder() -> int:
    f = 0
    q = ce._build_exa_contact_query("Jane Smith", "Acme", "https://www.linkedin.com/in/jane")
    f += _check("query contains name/company/url",
                "Jane Smith" in q and "Acme" in q and "linkedin.com/in/jane" in q, q)
    q2 = ce._build_exa_contact_query("", "", "")
    f += _check("query degrades gracefully", "this person" in q2, q2)
    return f


def run() -> int:
    failures = 0
    print("[test] extract_exa_contact_fields")
    failures += test_parser()
    print("[test] exa_enrich_by_linkedin gating")
    failures += test_helper_gating()
    print("[test] zoominfo_enrich_by_email gating")
    failures += test_zoominfo_email_gating()
    print("[test] _build_exa_contact_query")
    failures += test_query_builder()
    if failures:
        print(f"FAIL: {failures} check(s) failed")
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
