import re
from pathlib import Path

import pytest
from fastapi import APIRouter
from routers.jobs import router as jobs_router

def test_nginx_jobs_regex_is_comprehensive():
    """
    Ensure all /jobs/{job_id}/<subpath> endpoints in the jobs_router
    are accounted for in the nginx-app-locations.conf regex.
    """
    from pathlib import Path

    # Find nginx-app-locations.conf relative to this test file or repository root
    test_dir = Path(__file__).resolve().parent
    repo_root = test_dir.parents[2]
    conf_path = repo_root / "nginx-app-locations.conf"
    if not conf_path.exists():
        conf_path = Path("nginx-app-locations.conf").resolve()

    with open(conf_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Look for the location block for job ID-specific endpoints
    # e.g. location ~ ^/jobs/([^/]+)/(monitored-data|save|...)(/.*)?$
    match = re.search(r'location ~ \^/jobs/\(\[\^/\]\+\)/\((.*?)\)\(\/\.\*\)\?\$', content)
    assert match is not None, "Could not find the /jobs/ regex in nginx-app-locations.conf"
    
    allowed_subpaths = match.group(1).split('|')
    
    # 2. Iterate through FastAPI jobs_router routes
    for route in jobs_router.routes:
        path = route.path
        
        # We are only interested in /jobs/{job_id}/... routes
        # Path looks like /jobs/{job_id}/draft or /jobs/{job_id_or_ref}/outreach-stats
        path_match = re.match(r'^/jobs/\{[^/]+\}/([^/]+)', path)
        if path_match:
            subpath = path_match.group(1)
            
            assert subpath in allowed_subpaths, (
                f"FastAPI route {path} has subpath '{subpath}', which is missing "
                f"from the NGINX allowlist in nginx-app-locations.conf. "
                f"Please add it to prevent 404 routing errors on QA/Production."
            )


def test_voice_webhook_has_its_own_rate_limit_zone():
    """Pairbot sends ~20-30 webhooks/min; under /api/ they'd share the 30r/m
    write zone with pairbot's DNC and creation POSTs."""
    repo = Path(__file__).resolve().parents[3]
    upstreams = (repo / "nginx-upstreams.conf").read_text(encoding="utf-8")
    assert re.search(r"zone=voice_webhook_limit:\S+ rate=60r/m;", upstreams)
    conf = (repo / "nginx-app-locations.conf").read_text(encoding="utf-8")
    m = re.search(r"location = /api/v1/voice/interviews/webhook \{(.*?)\n    \}", conf, re.S)
    assert m, "exact nginx location for the voice webhook is missing"
    assert "zone=voice_webhook_limit" in m.group(1)
    assert "api_write_limit" not in m.group(1) and "api_limit" not in m.group(1)
    assert "proxy_pass http://airecruiter_api$uri$is_args$args;" in m.group(1)


def test_launch_session_upload_has_its_own_batch_location():
    """POST /api/v1/launch-sessions/<uuid>/candidates (provided-mode uploads,
    chunks of 50 with résumé text) rides pair_batch_limit with the
    /candidates/save body ceiling, not the /api/ write limit (30 r/m)."""
    from pathlib import Path

    conf = (Path(__file__).resolve().parents[3] / "nginx-app-locations.conf").read_text(encoding="utf-8")
    m = re.search(r"location ~ (\^/api/v1/launch-sessions/\S+) \{(.*?)\n    \}", conf, re.S)
    assert m, "the launch-session upload location is missing"
    pattern, block = m.group(1), m.group(2)
    rx = re.compile(pattern)
    assert rx.match("/api/v1/launch-sessions/0f8fad5b-d9cb-469f-a165-70867728950e/candidates")
    assert not rx.match("/api/v1/launch-sessions/0f8fad5b-d9cb-469f-a165-70867728950e/start")
    assert not rx.match("/api/v1/launch-sessions/config")
    assert not rx.match("/api/v1/launch-sessions/0f8fad5b-d9cb-469f-a165-70867728950e/candidates/x")
    assert "limit_req zone=pair_batch_limit" in block
    assert "client_max_body_size 25m;" in block
    assert "proxy_buffering off;" in block
    assert "proxy_pass http://airecruiter_api$uri$is_args$args;" in block


# ── nginx's own tokenizer: regex locations ───────────────────────────────────
# Python's `re` accepts `[0-9a-f-]{36}`, but nginx's config tokenizer ends an
# UNQUOTED word at `{` (unless it follows `$`), so that regex splits in two and
# `nginx -t` fails with `unknown directive "36}/candidates$"` — which aborts
# deploy-azure.sh after the code is already in place. These tests run a port of
# ngx_conf_read_token (src/core/ngx_conf_file.c) over every nginx*.conf.

_LOCATION_LINE = re.compile(r"^\s*location\s+(~\*?)\s+(.+?)\s*\{\s*(?:#.*)?$")


def _ngx_statements(text: str):
    """[(line, [(word, quoted)], terminator)] — terminator ';', '{' or '}'.

    Mirrors ngx_conf_read_token's word rules: a word ends at space, tab, CR,
    LF, ';' or '{' ('}' does not end a word; '{' right after '$' is part of a
    ${var}); a backslash escapes the next character; "…" / '…' are one word;
    '#' starts a comment at a word boundary. Raises ValueError where nginx
    would report "unexpected …"."""
    out = []
    args = []
    buf = []
    line = 1
    word_line = 1
    last_space, need_space, comment, quoted = True, False, False, False
    d_quoted = s_quoted = variable = qword = False

    def end_statement(term):
        nonlocal args
        out.append((args[0][2] if args else line, [(w, q) for w, q, _ in args], term))
        args = []

    for ch in text:
        if ch == "\n":
            line += 1
            comment = False
        if comment:
            continue
        if quoted:
            quoted = False
            buf.append(ch)
            continue
        if need_space:
            if ch in " \t\r\n":
                last_space, need_space = True, False
                continue
            if ch in ";{":
                last_space, need_space = True, False
                end_statement(ch)
                continue
            raise ValueError(f"line {line}: unexpected {ch!r}")
        if last_space:
            if ch in " \t\r\n":
                continue
            if ch in ";{":
                if not args:
                    raise ValueError(f"line {line}: unexpected {ch!r}")
                end_statement(ch)
                continue
            if ch == "}":
                if args:
                    raise ValueError(f"line {line}: unexpected '}}'")
                out.append((line, [], "}"))
                continue
            if ch == "#":
                comment = True
                continue
            last_space, buf, word_line, qword, variable = False, [], line, False, False
            if ch == '"':
                d_quoted = qword = True
            elif ch == "'":
                s_quoted = qword = True
            else:
                buf.append(ch)
                quoted = ch == "\\"
                variable = ch == "$"
            continue
        if ch == "{" and variable:
            buf.append(ch)
            continue
        variable = False
        if ch == "\\":
            quoted = True
            buf.append(ch)
            continue
        if ch == "$":
            variable = True
            buf.append(ch)
            continue
        found = False
        if d_quoted:
            if ch == '"':
                d_quoted, need_space, found = False, True, True
        elif s_quoted:
            if ch == "'":
                s_quoted, need_space, found = False, True, True
        elif ch in " \t\r\n;{":
            last_space, found = True, True
        if found:
            args.append(("".join(buf), qword, word_line))
            if ch in ";{":
                end_statement(ch)
            continue
        buf.append(ch)
    if args or not last_space or d_quoted or s_quoted:
        raise ValueError("unexpected end of file")
    return out


def _location_regex_problems(text: str, name: str = "conf"):
    """Every `location ~` / `~*` must parse, under nginx's tokenizer, as
    exactly [location, ~, <the regex as written>] `{` — and an UNQUOTED regex
    may not contain '{', '}' or ';' (quote the whole regex if it needs them)."""
    problems = []
    lines = text.split("\n")
    try:
        statements = _ngx_statements(text)
    except ValueError as exc:
        return [f"{name}: nginx tokenizer error: {exc}"]
    for line_no, words, term in statements:
        if len(words) < 2 or words[0] != ("location", False) or words[1][0] not in ("~", "~*"):
            continue
        where = f"{name}:{line_no}"
        m = _LOCATION_LINE.match(lines[line_no - 1])
        if not m:
            problems.append(f"{where}: a regex location must be one line ending in '{{'")
            continue
        raw = m.group(2)
        is_quoted = len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]
        if not is_quoted and any(c in raw for c in "{};"):
            problems.append(f"{where}: unquoted location regex {raw!r} contains '{{', '}}' or ';' "
                            "(nginx splits the word there) — quote the regex or drop the {n} quantifier")
        expected = raw[1:-1] if is_quoted else raw
        if term != "{" or len(words) != 3 or words[2][0] != expected:
            problems.append(f"{where}: nginx reads this location as {[w for w, _ in words]} {term!r}, "
                            f"not as the regex {raw!r}")
    return problems


def _nginx_confs():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    confs = sorted(root.glob("nginx*.conf"))
    assert any(p.name == "nginx-app-locations.conf" for p in confs), f"no nginx confs under {root}"
    return confs


def test_every_nginx_location_regex_is_one_nginx_token():
    """The QA-deploy blocker: `[0-9a-fA-F-]{36}` passed Python's `re` but not
    `nginx -t`."""
    problems = []
    checked = 0
    for path in _nginx_confs():
        # deploy-azure.sh seds {{DOMAIN_NAME}} / {{LEGACY_DOMAIN}} in before install.
        text = re.sub(r"\{\{[A-Z_]+\}\}", "pair.example.test", path.read_text(encoding="utf-8"))
        problems += _location_regex_problems(text, path.name)
        checked += sum(1 for ln in text.split("\n") if _LOCATION_LINE.match(ln))
    assert checked >= 3, "expected the /api/v1/launch-sessions, /jobs and /candidates regex locations"
    assert problems == []


@pytest.mark.parametrize("line, ok", [
    ("location ~ ^/api/v1/launch-sessions/[0-9a-fA-F-]{36}/candidates$ {", False),
    ("location ~* ^/a/(x|y){2}$ {", False),
    ("location ~ ^/a;b$ {", False),
    ("location ~ ^/a/b}$ {", False),
    ('location ~ "^/api/v1/launch-sessions/[0-9a-fA-F-]{36}/candidates$" {', True),
    ("location ~ '^/a/[0-9]{2}$' {", True),
    ("location ~ ^/api/v1/launch-sessions/[0-9a-fA-F-]+/candidates$ {", True),
    ("location ~ ^/jobs/([^/]+)/(save|draft)(/.*)?$ {", True),
])
def test_the_regex_check_catches_what_nginx_t_rejects(line, ok):
    text = f"server {{\n    {line}\n        proxy_pass http://u$uri;\n    }}\n}}\n"
    assert (_location_regex_problems(text) == []) is ok


def test_the_tokenizer_port_matches_nginx_on_the_blocker():
    """What nginx itself does with the broken line (the reviewer's repro)."""
    broken = "location ~ ^/api/v1/launch-sessions/[0-9a-fA-F-]{36}/candidates$ {\n proxy_pass http://u;\n}\n"
    statements = _ngx_statements(broken)
    assert statements[0][1] == [("location", False), ("~", False), ("^/api/v1/launch-sessions/[0-9a-fA-F-]", False)]
    assert statements[0][2] == "{"
    assert statements[1][1] == [("36}/candidates$", False)] and statements[1][2] == "{"
    assert _ngx_statements('add_header X "${a}{b}";')[0][1][2] == ("${a}{b}", True)
    assert _ngx_statements("set $x ${y}z;")[0][1] == [("set", False), ("$x", False), ("${y}z", False)]
