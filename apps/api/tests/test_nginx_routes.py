import re
from fastapi import APIRouter
from routers.jobs import router as jobs_router

def test_nginx_jobs_regex_is_comprehensive():
    """
    Ensure all /jobs/{job_id}/<subpath> endpoints in the jobs_router
    are accounted for in the nginx-app-locations.conf regex.
    """
    # 1. Parse the regex from the NGINX config file
    with open("../../nginx-app-locations.conf", "r") as f:
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
