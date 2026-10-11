import re
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
