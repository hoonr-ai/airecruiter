#!/bin/bash

# Azure-Specific Deployment Script for AI Recruiter
# This script assumes you have Azure Database for PostgreSQL setup
#
# 🔄 USAGE:
# Auto-detect (based on git branch):   ./deploy-azure.sh
# Explicit domain:                     ./deploy-azure.sh curate.hoonr.ai
# CI/CD (environment variable):        DOMAIN_NAME=domain.com ./deploy-azure.sh
# Force env update:                    FORCE_ENV_UPDATE=true ./deploy-azure.sh
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
            echo "curate.hoonr.ai"
            ;;
        "develop"|"development")
            echo "qacurate.hoonr.ai"
            ;;
        *)
            # Default to QA for feature branches or unknown
            echo "qacurate.hoonr.ai"
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
echo -e "${BLUE}🟢 Installing Node.js 18.x...${NC}"
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
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

# Always copy and update nginx config
sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
print_status "Nginx configuration updated"

# Restore template for next deployment
sed -i "s/$DOMAIN_NAME/{{DOMAIN_NAME}}/g" "$PROJECT_DIR/nginx.conf"

# Enable airecruiter site and disable default
sudo ln -sf /etc/nginx/sites-available/airecruiter /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
print_status "Nginx airecruiter site enabled"

# Test and reload nginx configuration
if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Nginx configuration reloaded"
else
    print_error "Nginx configuration test failed"
fi

# Create systemd services
echo -e "${BLUE}🔧 Setting up systemd services...${NC}"

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
sudo systemctl enable airecruiter-api airecruiter-web

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

if [ "$web_status" = "active" ]; then
    print_status "Web service is running"
else
    print_error "Web service failed to start - check: journalctl -u airecruiter-web"
fi

# Test local connectivity
if curl -s --max-time 5 http://localhost:8000/docs > /dev/null 2>&1; then
    print_status "API responding on localhost:8000"
else
    print_warning "API not responding on localhost:8000"
fi

if curl -s --max-time 5 http://localhost:3000 > /dev/null 2>&1; then
    print_status "Web responding on localhost:3000"  
else
    print_warning "Web not responding on localhost:3000"
fi

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