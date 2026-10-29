#!/bin/bash

# Azure-Specific Deployment Script for AI Recruiter
# This script assumes you have Azure Database for PostgreSQL setup
#
# 🔄 USAGE:
# Auto-detect (based on git branch):   ./deploy-azure.sh
# Explicit domain:                     ./deploy-azure.sh pair.pyramidci.com
# CI/CD (environment variable):        DOMAIN_NAME=domain.com ./deploy-azure.sh
# Force env update:                    FORCE_ENV_UPDATE=true ./deploy-azure.sh
# Legacy 301 redirect (transition):    LEGACY_DOMAIN=old.example.com LEGACY_REDIRECT_UNTIL=YYYY-MM-DD ./deploy-azure.sh
#
# 🌍 DOMAIN DETECTION PRIORITY:
# 1. Command line argument (highest)
# 2. DOMAIN_NAME environment variable (CI/CD)  
# 3. Git branch detection (main→PROD, develop→QA)
# 4. Default to QA domain

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
PROJECT_DIR="/home/ubuntu/codebase/airecruiter"
API_DIR="$PROJECT_DIR/apps/api"
WEB_DIR="$PROJECT_DIR/apps/web"
DEPLOY_USER="ubuntu"

# Function to detect environment domain
detect_domain() {
    # Priority order:
    # 1. Command line argument (highest priority)
    # 2. Environment variable (CI/CD sets this)
    # 3. Auto-detect from git branch (for manual runs)
    # 4. Default fallback
    
    if [ -n "$1" ]; then
        # Command line argument provided
        echo "$1"
        return
    fi
    
    if [ -n "$DOMAIN_NAME" ]; then
        # Environment variable set (CI/CD pipeline)
        echo "$DOMAIN_NAME"
        return
    fi
    
    # Auto-detect from git branch (for manual runs)
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    case "$current_branch" in
        "main"|"master")
            echo "pair.pyramidci.com"
            ;;
        "develop"|"development")
            echo "pairqa.pyramidci.com"
            ;;
        *)
            # Default to QA for feature branches or unknown
            echo "pairqa.pyramidci.com"
            ;;
    esac
}

# Store original env var for detection logging
DOMAIN_NAME_FROM_ENV="$DOMAIN_NAME"

# Detect and set domain
DOMAIN_NAME=$(detect_domain "$1")

echo -e "${BLUE}🚀 Starting AI Recruiter deployment for Azure...${NC}"
echo -e "${BLUE}🌍 Deployment Domain: ${YELLOW}$DOMAIN_NAME${NC}"

# Display domain detection method
if [ -n "$1" ]; then
    echo -e "${BLUE}🎯 Domain set via: ${GREEN}Command line argument${NC}"
elif [ -n "${DOMAIN_NAME_FROM_ENV:-$DOMAIN_NAME}" ] && [ -z "$1" ]; then
    echo -e "${BLUE}🎯 Domain set via: ${GREEN}Environment variable (CI/CD)${NC}"
else
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    echo -e "${BLUE}🎯 Domain set via: ${GREEN}Git branch auto-detection${NC} (branch: $current_branch)"
fi
echo ""

print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}❌${NC} $1"
}

# Deployed environment for telemetry names: NEW_RELIC_ENVIRONMENT from
# deploy.yml, else derived from the domain.
DEPLOY_ENV="${NEW_RELIC_ENVIRONMENT:-}"
if [ -z "$DEPLOY_ENV" ]; then
    case "$DOMAIN_NAME" in
        pair.pyramidci.com) DEPLOY_ENV="prod" ;;
        pairqa.pyramidci.com) DEPLOY_ENV="qa" ;;
    esac
fi

# ensure_trailing_newline FILE
# Appending to a file whose last line has no newline would glue the new line
# onto it (LAST=xNAME=...), corrupting both.
ensure_trailing_newline() {
    if [ -s "$1" ] && [ -n "$(tail -c1 "$1")" ]; then
        printf '\n' >> "$1"
    fi
}

# upsert_env FILE NAME VALUE [quote]
# Writes NAME=VALUE (NAME="VALUE" with quote) unless the file already holds
# that value. Compares after stripping only the outer quotes, so a value with
# spaces ("PAIR API (QA)") is not rewritten on every deploy. Never prints the
# value. Returns 0 when it wrote, 1 when unchanged.
upsert_env() {
    local file="$1" name="$2" value="$3" quote="${4:-}" current
    current=$(grep -E "^${name}=" "$file" | tail -n1 | cut -d= -f2-)
    current="${current%$'\r'}"
    case "$current" in
        \"*\") current="${current#\"}"; current="${current%\"}" ;;
        \'*\') current="${current#\'}"; current="${current%\'}" ;;
    esac
    if [ "$current" = "$value" ]; then
        return 1
    fi
    sed -i "/^${name}=/d" "$file"
    ensure_trailing_newline "$file"
    if [ -n "$quote" ]; then
        printf '%s="%s"\n' "$name" "$value" >> "$file"
    else
        printf '%s=%s\n' "$name" "$value" >> "$file"
    fi
    return 0
}

# Check if running as Ubuntu user
if [ "$USER" != "ubuntu" ]; then
    print_error "Please run this script as the ubuntu user"
    exit 1
fi

echo -e "${BLUE}Prerequisites Check:${NC}"
echo "Before running this script, ensure you have:"
echo "1. ✅ Azure Database for PostgreSQL created"
echo "2. ✅ Database connection string ready"
echo "3. ✅ All API keys available"
echo ""
echo "Proceeding with deployment..."

# Update system packages
echo -e "${BLUE}📦 Updating system packages...${NC}"
sudo apt update && sudo apt upgrade -y
print_status "System packages updated"

# Install essential packages (no PostgreSQL server needed for Azure Database)
echo -e "${BLUE}🛠 Installing essential packages...${NC}"
sudo apt install -y \
    curl \
    wget \
    git \
    nginx \
    ufw \
    htop \
    vim \
    unzip \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    build-essential \
    postgresql-client

print_status "Essential packages installed"

# Install Node.js 18.x (required for Next.js)
# No `sudo -E`: this pipes a remote script into a root shell, and -E would hand
# it the exported deploy secrets (KIPPLO_API_KEY, NEW_RELIC_LICENSE_KEY).
echo -e "${BLUE}🟢 Installing Node.js 18.x...${NC}"
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo bash -
sudo apt install -y nodejs
node --version
npm --version
print_status "Node.js installed"

# Install Python dependencies (using existing Python 3.12.3)
echo -e "${BLUE}🐍 Installing Python dependencies...${NC}"
sudo apt install -y python3-venv python3-dev python3-pip
python3 --version
print_status "Python dependencies installed"

# Create app directory and set permissions
echo -e "${BLUE}📁 Setting up application directories...${NC}"
sudo mkdir -p /var/log/airecruiter
sudo chown $DEPLOY_USER:$DEPLOY_USER /var/log/airecruiter
print_status "Application directories created"

# Install Python virtual environment for API
echo -e "${BLUE}🐍 Setting up Python virtual environment...${NC}"
cd "$API_DIR"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

# Install requirements with better error handling
echo -e "${BLUE}📦 Installing Python requirements...${NC}"
if ! pip install -r requirements.txt; then
    print_warning "Some packages failed to install, attempting individual installation..."
    # Install critical packages individually
    pip install fastapi uvicorn pydantic sqlalchemy psycopg2-binary python-dotenv
    pip install rapidfuzz || print_warning "rapidfuzz installation failed, continuing..."
    # Try requirements again
    pip install -r requirements.txt || print_warning "Some optional packages may be missing"
fi
print_status "Python dependencies installed"

# Check if .env exists
if [ -f "$API_DIR/.env" ]; then
    print_status "API environment file already exists and will be used"

    # APP_BASE_URL is embedded into candidate report / engagement emails at send
    # time (see core/email.py resolve_app_base_url). The code default only
    # applies when the var is unset, so on a domain rename a stale value in .env
    # keeps mailing links to the retired hostname long after cutover - and those
    # links outlive the legacy 301 window. The deployed domain is the authority
    # here, so reconcile rather than warn.
    EXPECTED_APP_BASE_URL="https://$DOMAIN_NAME"
    CURRENT_APP_BASE_URL=$(grep -E '^APP_BASE_URL=' "$API_DIR/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"'" ')

    if [ -z "$CURRENT_APP_BASE_URL" ]; then
        ensure_trailing_newline "$API_DIR/.env"
        echo "APP_BASE_URL=$EXPECTED_APP_BASE_URL" >> "$API_DIR/.env"
        print_status "APP_BASE_URL added to .env: $EXPECTED_APP_BASE_URL"
    elif [ "$CURRENT_APP_BASE_URL" != "$EXPECTED_APP_BASE_URL" ]; then
        cp "$API_DIR/.env" "$API_DIR/.env.bak"
        sed -i "s#^APP_BASE_URL=.*#APP_BASE_URL=$EXPECTED_APP_BASE_URL#" "$API_DIR/.env"
        print_warning "APP_BASE_URL was $CURRENT_APP_BASE_URL - updated to $EXPECTED_APP_BASE_URL (backup: .env.bak)"
    else
        print_status "APP_BASE_URL matches deployed domain"
    fi

    # KIPPLO_API_KEY (contact enrichment) comes from the GitHub secret of the
    # same name via deploy.yml. Written only when the secret is set and differs,
    # so an unset secret leaves a key added to .env by hand alone. The value is
    # never printed.
    if [ -n "${KIPPLO_API_KEY:-}" ]; then
        CURRENT_KIPPLO_API_KEY=$(grep -E '^KIPPLO_API_KEY=' "$API_DIR/.env" | tail -n1 | cut -d= -f2- | tr -d '"'"'"'" ')
        if [ "$CURRENT_KIPPLO_API_KEY" != "$KIPPLO_API_KEY" ]; then
            sed -i '/^KIPPLO_API_KEY=/d' "$API_DIR/.env"
            ensure_trailing_newline "$API_DIR/.env"
            echo "KIPPLO_API_KEY=$KIPPLO_API_KEY" >> "$API_DIR/.env"
            print_status "KIPPLO_API_KEY updated in .env from the deploy secret"
        else
            print_status "KIPPLO_API_KEY matches the deploy secret"
        fi
    fi

    # New Relic APM (docs/observability/new-relic.md). Each value is written
    # only when deploy.yml passed it non-empty: an unset secret leaves a key
    # added to .env by hand alone. NEW_RELIC_ENABLED (the kill switch) is never
    # written here, so `NEW_RELIC_ENABLED=false` in .env survives deploys.
    if [ -n "${NEW_RELIC_LICENSE_KEY:-}" ]; then
        if upsert_env "$API_DIR/.env" NEW_RELIC_LICENSE_KEY "$NEW_RELIC_LICENSE_KEY"; then
            print_status "NEW_RELIC_LICENSE_KEY updated in .env from the deploy secret"
        else
            print_status "NEW_RELIC_LICENSE_KEY matches the deploy secret"
        fi
    fi
    if [ -n "${NEW_RELIC_APP_NAME:-}" ]; then
        if upsert_env "$API_DIR/.env" NEW_RELIC_APP_NAME "$NEW_RELIC_APP_NAME" quote; then
            print_status "NEW_RELIC_APP_NAME set to $NEW_RELIC_APP_NAME"
        fi
    fi
    if [ -n "${NEW_RELIC_ENVIRONMENT:-}" ]; then
        if upsert_env "$API_DIR/.env" NEW_RELIC_ENVIRONMENT "$NEW_RELIC_ENVIRONMENT"; then
            print_status "NEW_RELIC_ENVIRONMENT set to $NEW_RELIC_ENVIRONMENT"
        fi
    fi

    # Server launch sessions (docs/launch-sessions.md "Rollout and deploys"):
    # the GitHub environment variables of the same name via deploy.yml. Written
    # only when set, so an unset variable leaves .env - and the code default
    # (on for QA, off for PROD) - alone. `if` keeps set -e from exiting when
    # upsert_env reports "unchanged" (status 1).
    if [ -n "${PAIR_SERVER_LAUNCH_ENABLED:-}" ]; then
        if upsert_env "$API_DIR/.env" PAIR_SERVER_LAUNCH_ENABLED "$PAIR_SERVER_LAUNCH_ENABLED"; then
            print_status "PAIR_SERVER_LAUNCH_ENABLED set to $PAIR_SERVER_LAUNCH_ENABLED"
        else
            print_status "PAIR_SERVER_LAUNCH_ENABLED already $PAIR_SERVER_LAUNCH_ENABLED"
        fi
    fi
    if [ -n "${PAIR_LAUNCH_RUNNER_ENABLED:-}" ]; then
        if upsert_env "$API_DIR/.env" PAIR_LAUNCH_RUNNER_ENABLED "$PAIR_LAUNCH_RUNNER_ENABLED"; then
            print_status "PAIR_LAUNCH_RUNNER_ENABLED set to $PAIR_LAUNCH_RUNNER_ENABLED"
        else
            print_status "PAIR_LAUNCH_RUNNER_ENABLED already $PAIR_LAUNCH_RUNNER_ENABLED"
        fi
    fi
else
    print_warning "API environment file not found. Please ensure .env is present in $API_DIR"
fi

# Install Next.js dependencies and create environment
echo -e "${BLUE}⚛️ Installing Next.js dependencies and configuring...${NC}"
cd "$WEB_DIR"
npm install --legacy-peer-deps

# Check if .env.local exists
if [ -f "$WEB_DIR/.env.local" ]; then
    print_status "Web environment file already exists and will be used"

    # Browser telemetry tags (lib/newrelic.ts): environment and release. Baked
    # in at build time, so written before `npm run build`.
    APP_VERSION=$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo "")
    if [ -n "$DEPLOY_ENV" ] && upsert_env "$WEB_DIR/.env.local" NEXT_PUBLIC_APP_ENV "$DEPLOY_ENV"; then
        print_status "NEXT_PUBLIC_APP_ENV set to $DEPLOY_ENV"
    fi
    if [ -n "$APP_VERSION" ] && upsert_env "$WEB_DIR/.env.local" NEXT_PUBLIC_APP_VERSION "$APP_VERSION"; then
        print_status "NEXT_PUBLIC_APP_VERSION set to $APP_VERSION"
    fi
else
    print_warning "Web environment file not found. Please ensure .env.local is present in $WEB_DIR"
fi

# Don't build yet - wait until environment is configured
print_status "Next.js dependencies installed and configured"

# Build Next.js application for production
echo -e "${BLUE}🏗️ Building Next.js application...${NC}"
cd "$WEB_DIR"
rm -rf .next  # Clean previous build artifacts
npm run build
print_status "Next.js application built"

# Configure Nginx reverse proxy
echo -e "${BLUE}🌐 Configuring Nginx reverse proxy...${NC}"

# Update nginx config with the correct domain (replace template placeholder)
sed -i "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" "$PROJECT_DIR/nginx.conf"

# Shared location/header config, included by both the HTTPS and bootstrap
# server blocks so the two can never drift apart.
sudo mkdir -p /etc/nginx/snippets
sudo cp "$PROJECT_DIR/nginx-app-locations.conf" /etc/nginx/snippets/airecruiter-app.conf
print_status "Nginx shared application snippet installed"

# Shared proxy/rate-limit directives for the three /api/v1/notifications*
# locations inside airecruiter-app.conf above (the exact list, the catch-all,
# and the SSE stream) so they can't drift apart on those. Installed here,
# before airecruiter-app.conf's own `include` of it is ever tested/loaded.
sudo cp "$PROJECT_DIR/nginx-notifications-common.conf" /etc/nginx/snippets/airecruiter-notifications-common.conf
print_status "Nginx shared notifications snippet installed"

# Shared upstream/limit_req_zone directives, included at the top level by both
# nginx.conf and nginx-bootstrap.conf. Must be installed before either config
# is tested/loaded, since the locations above proxy_pass/limit_req against
# these.
sudo cp "$PROJECT_DIR/nginx-upstreams.conf" /etc/nginx/snippets/airecruiter-upstreams.conf
print_status "Nginx shared upstreams snippet installed"

# nginx.conf hard-references /etc/letsencrypt/live/$DOMAIN_NAME/*.pem. On the
# first deploy after a domain rename that cert does not exist yet, so installing
# it fails `nginx -t` -> nginx never reloads -> certbot's nginx plugin also
# fails (it runs `nginx -t` too) -> the cert can never be issued. Break the
# deadlock by serving HTTP-only until setup-ssl.sh has obtained a certificate.
if sudo test -f "/etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem"; then
    sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
    print_status "Nginx configuration updated (HTTPS)"
else
    print_warning "No certificate for $DOMAIN_NAME yet - installing HTTP-only bootstrap config"
    sed "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" "$PROJECT_DIR/nginx-bootstrap.conf" \
        | sudo tee /etc/nginx/sites-available/airecruiter > /dev/null
    print_status "Nginx bootstrap configuration installed (setup-ssl.sh will switch to HTTPS)"
fi

# Enable airecruiter site and disable default
sudo ln -sf /etc/nginx/sites-available/airecruiter /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
print_status "Nginx airecruiter site enabled"

# ---------------------------------------------------------------------------
# Legacy domain 301 redirect (transition window)
#
# The rename is a hard switch: once nginx reloads with the new server_name, the
# old hostname stops answering. While LEGACY_DOMAIN is still in DNS and pointed
# at this VM, serve it from a redirect-only site so old links 301 to the new
# canonical host instead of erroring. Self-expiring: after
# LEGACY_REDIRECT_UNTIL the site is removed and the old name goes dark.
# ---------------------------------------------------------------------------
LEGACY_DOMAIN="${LEGACY_DOMAIN:-}"
LEGACY_REDIRECT_UNTIL="${LEGACY_REDIRECT_UNTIL:-}"
LEGACY_SITE="/etc/nginx/sites-available/airecruiter-legacy"

install_legacy_redirect=false
if [ -n "$LEGACY_DOMAIN" ] && [ "$LEGACY_DOMAIN" != "$DOMAIN_NAME" ]; then
    if [ -n "$LEGACY_REDIRECT_UNTIL" ] && [ "$(date -u +%Y-%m-%d)" \> "$LEGACY_REDIRECT_UNTIL" ]; then
        print_warning "Legacy redirect window for $LEGACY_DOMAIN ended $LEGACY_REDIRECT_UNTIL - removing redirect"
    elif ! sudo test -f "/etc/letsencrypt/live/$LEGACY_DOMAIN/fullchain.pem"; then
        # No cert for the old name means the HTTPS redirect block would fail
        # `nginx -t` and take the whole site down. Skip rather than break.
        print_warning "No SSL certificate for $LEGACY_DOMAIN - skipping legacy redirect"
    else
        install_legacy_redirect=true
    fi
fi

if [ "$install_legacy_redirect" = true ]; then
    sed -e "s/{{LEGACY_DOMAIN}}/$LEGACY_DOMAIN/g" \
        -e "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" \
        "$PROJECT_DIR/nginx-legacy-redirect.conf" | sudo tee "$LEGACY_SITE" > /dev/null
    sudo ln -sf "$LEGACY_SITE" /etc/nginx/sites-enabled/
    print_status "Legacy 301 redirect enabled: $LEGACY_DOMAIN -> $DOMAIN_NAME (until ${LEGACY_REDIRECT_UNTIL:-removed manually})"
else
    sudo rm -f /etc/nginx/sites-enabled/airecruiter-legacy "$LEGACY_SITE"
fi

# Test and reload nginx configuration
if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Nginx configuration reloaded"
else
    print_error "Nginx configuration test failed - aborting before reload"
    exit 1
fi

# Copy systemd service files
if [ -f "$PROJECT_DIR/systemd/airecruiter-api.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/airecruiter-api.service" /etc/systemd/system/
    print_status "API service file installed"
else
    print_warning "API service file not found in systemd/ directory"
fi

if [ -f "$PROJECT_DIR/systemd/airecruiter-web.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/airecruiter-web.service" /etc/systemd/system/
    print_status "Web service file installed"
else
    print_warning "Web service file not found in systemd/ directory"
fi

# airecruiter-worker (arq) runs the task_outbox relay — candidate_callback_reminder
# notifications and every other queued task included. This file/enable/restart
# block was missing entirely, so a deploy left the worker running whatever code
# it last booted with while the API moved on, and the two relays (API's own
# scheduler and the worker's arq cron, see services/task_outbox.py) raced each
# other on the same rows: the stale worker's claims failed as "unknown task"
# while the freshly-restarted API's claims still succeeded, intermittently
# dropping notifications. Restart it every deploy, same as the API.
#
# Only when this host's own .env actually turns arq on (ARQ_ENABLED=true) —
# core/queue.py's enqueue() and services/task_outbox.py's relay both already
# fall back to running in-process when it's off, so nothing needs the worker
# in that case. PROD's .env.example documents ARQ_ENABLED defaulting off
# there (core/config.py get_env_bool default); unconditionally enabling and
# starting `arq worker.WorkerSettings` with no REDIS_URL would crash-loop the
# unit forever under systemd's Restart=always (PR review).
WORKER_ARQ_ENABLED=false
if [ -f "$API_DIR/.env" ]; then
    _arq_env_val=$(grep -E '^ARQ_ENABLED=' "$API_DIR/.env" | tail -n1 | cut -d'=' -f2- | tr -d '"'"'"' \r')
    case "$(echo "$_arq_env_val" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) WORKER_ARQ_ENABLED=true ;;
    esac
fi

if [ "$WORKER_ARQ_ENABLED" = true ] && [ -f "$PROJECT_DIR/systemd/airecruiter-worker.service" ]; then
    sudo cp "$PROJECT_DIR/systemd/airecruiter-worker.service" /etc/systemd/system/
    print_status "Worker service file installed"
elif [ "$WORKER_ARQ_ENABLED" = true ]; then
    print_warning "Worker service file not found in systemd/ directory"
else
    print_status "ARQ_ENABLED is not set in $API_DIR/.env — skipping the arq worker (API/task_outbox already run in-process without it)"
fi

print_status "Systemd services configured"

# Configure firewall
echo -e "${BLUE}🔥 Configuring firewall...${NC}"
sudo ufw --force enable
sudo ufw allow ssh
sudo ufw allow 80
sudo ufw allow 443
sudo ufw allow 8000  # API port for external access
sudo ufw allow 3000  # Web port for external access
print_status "Firewall configured"

# Enable services and start them
echo -e "${BLUE}🔧 Enabling and starting services...${NC}"
sudo systemctl daemon-reload
if [ "$WORKER_ARQ_ENABLED" = true ]; then
    sudo systemctl enable airecruiter-api airecruiter-web airecruiter-worker
else
    sudo systemctl enable airecruiter-api airecruiter-web
    # Make sure a leftover worker from a previous deploy (or a manual start)
    # isn't left running stale code with nothing managing it, now that this
    # host's .env says arq is off.
    sudo systemctl disable --now airecruiter-worker 2>/dev/null || true
fi

# Restart services to pick up new changes
echo -e "${BLUE}🔄 Restarting services...${NC}"
if systemctl is-active --quiet airecruiter-api; then
    sudo systemctl restart airecruiter-api
    print_status "API service restarted"
else
    sudo systemctl start airecruiter-api
    print_status "API service started"
fi

sleep 3

if [ "$WORKER_ARQ_ENABLED" = true ]; then
    if systemctl is-active --quiet airecruiter-worker; then
        sudo systemctl restart airecruiter-worker
        print_status "Worker service restarted"
    else
        sudo systemctl start airecruiter-worker
        print_status "Worker service started"
    fi
fi

sleep 2

if systemctl is-active --quiet airecruiter-web; then
    sudo systemctl restart airecruiter-web
    print_status "Web service restarted"
else
    sudo systemctl start airecruiter-web
    print_status "Web service started"
fi

sleep 2

print_status "Services enabled and started"

# Verify services are running
echo -e "${BLUE}🔍 Verifying deployment...${NC}"
sleep 3

api_status=$(systemctl is-active airecruiter-api)
web_status=$(systemctl is-active airecruiter-web)

if [ "$api_status" = "active" ]; then
    print_status "API service is running"
else
    print_error "API service failed to start - check: journalctl -u airecruiter-api"
fi

# Unlike api_status/web_status above, this must not just print and move on
# (PR review: "that's the exact silent-stale-worker failure mode this PR is
# trying to eliminate") — a dead worker on a host that declared it wants arq
# (ARQ_ENABLED=true) silently drops every queued notification and arq-only
# task with no other symptom, which is the entire bug this deploy fix exists
# to close. `|| true` keeps this from tripping `set -e` on its own (a bare
# `x=$(systemctl is-active ...)` aborts the script right here with no
# message at all when the unit isn't active); DEPLOY_FAILED instead exits
# with a clear message once every other check below has also had a chance
# to report. Skipped entirely when this host's .env has arq off — nothing
# depends on the worker there (see WORKER_ARQ_ENABLED above).
DEPLOY_FAILED=false
if [ "$WORKER_ARQ_ENABLED" = true ]; then
    worker_status=$(systemctl is-active airecruiter-worker || true)
    if [ "$worker_status" = "active" ]; then
        print_status "Worker service is running"
    else
        print_error "Worker service failed to start (status: ${worker_status:-unknown}) - check: journalctl -u airecruiter-worker"
        DEPLOY_FAILED=true
    fi
fi

if [ "$web_status" = "active" ]; then
    print_status "Web service is running"
else
    print_error "Web service failed to start - check: journalctl -u airecruiter-web"
fi

if [ "$DEPLOY_FAILED" = true ]; then
    echo "::error::airecruiter-worker failed to start on a host with ARQ_ENABLED=true — queued notifications would silently stop working"
    print_error "Aborting deploy: the worker is required on this host and isn't running."
    exit 1
fi

# Test local connectivity. /api/health answers 503 when Postgres is down and
# -f turns any 4xx/5xx into a failure (the old `curl -s /docs` passed while
# every request 500'd, #388). Warn loudly but carry on: setup-ssl.sh still has
# to run, and deploy.yml re-checks after it and fails the job.
if curl -fsS --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors -o /dev/null http://localhost:8000/api/health; then
    print_status "API healthy on localhost:8000 (/api/health)"
else
    # Plain line first so GitHub Actions turns it into an annotation.
    echo "::error::API health check failed on localhost:8000/api/health"
    print_error "API health check failed - check: journalctl -u airecruiter-api -n 200"
fi

if curl -s --max-time 5 http://localhost:3000 > /dev/null 2>&1; then
    print_status "Web responding on localhost:3000"  
else
    print_warning "Web not responding on localhost:3000"
fi

# Update API environment with correct domain for CORS
# if [ -f "$API_DIR/.env" ]; then
#     echo -e "${BLUE}🔧 Updating API environment configuration...${NC}"
#     # Update ALLOWED_ORIGINS to include the new domain
#     if grep -q "ALLOWED_ORIGINS=" "$API_DIR/.env"; then
#         sed -i "s#ALLOWED_ORIGINS=.*#ALLOWED_ORIGINS=*,https://$DOMAIN_NAME,http://$DOMAIN_NAME#" "$API_DIR/.env"
#     else
#         echo "ALLOWED_ORIGINS=*,https://$DOMAIN_NAME,http://$DOMAIN_NAME" >> "$API_DIR/.env"
#     fi
#     print_status "API CORS configuration updated"
# else
#     print_warning "API .env file not found, CORS configuration skipped"
# fi

echo -e "${GREEN}🎉 Azure deployment completed successfully!${NC}"
echo -e "${YELLOW}📋 Deployment Summary:${NC}"
echo -e "✅ Database: Azure PostgreSQL (job-diva-db.postgres.database.azure.com)"
echo -e "✅ API Service: Running on port 8000 (systemd managed)"
echo -e "✅ Web Service: Running on port 3000 (systemd managed)"
echo -e "✅ Nginx: Reverse proxy configured"
echo -e "✅ Firewall: Configured with ports open"
echo -e "✅ Auto-start: Services enabled for automatic restart"
echo ""
echo -e "${BLUE}🌐 Access URLs:${NC}"
echo -e "- Web App: http://$DOMAIN_NAME"
echo -e "- API Docs: http://$DOMAIN_NAME/api/docs"
echo -e "- Direct API: http://$DOMAIN_NAME:8000/docs (HTTP only)"
echo -e "- Direct Web: http://$DOMAIN_NAME:3000 (HTTP only)"
echo ""
echo -e "${YELLOW}⚙️ Optional Configuration:${NC}"
echo -e "1. Update API keys in: $API_DIR/.env"
echo -e "   - OPENAI_API_KEY, UNIPILE_API_KEY, UNIPILE_ACCOUNT_ID"
echo -e "2. Setup custom domain: ./setup-production.sh yourdomain.com"
echo -e "3. Run ./setup-ssl.sh to enable HTTPS"
echo ""
echo -e "${BLUE}🛠️ Management Commands:${NC}"
echo -e "- Quick redeploy:   ./manage.sh deploy"
echo -e "- Setup SSL:       ./setup-ssl.sh"
echo -e "- Service status:   ./manage.sh status"
echo -e "- View logs:        ./manage.sh logs"
echo -e "- Restart services: ./manage.sh restart"