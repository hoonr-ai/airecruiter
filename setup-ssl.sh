#!/bin/bash

# SSL Setup Script for AI Recruiter
# Run this after deploy-azure.sh if SSL is needed
# 
# 🔄 USAGE:
# Auto-detect (based on git branch):   ./setup-ssl.sh
# Explicit domain:                     ./setup-ssl.sh curate.hoonr.ai
# Environment variable:                DOMAIN_NAME=domain.com ./setup-ssl.sh

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_DIR="/home/ubuntu/codebase/airecruiter"

print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}❌${NC} $1"
}

# Function to detect environment domain (same logic as deploy-azure.sh)
detect_domain() {
    if [ -n "$1" ]; then
        echo "$1"
        return
    fi
    
    if [ -n "$DOMAIN_NAME" ]; then
        echo "$DOMAIN_NAME"
        return
    fi
    
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    case "$current_branch" in
        "main"|"master")
            echo "curate.hoonr.ai"
            ;;
        "develop"|"development")
            echo "pairqa.pyramidci.com"
            ;;
        *)
            echo "pairqa.pyramidci.com"
            ;;
    esac
}

# Detect and set domain
DOMAIN_NAME=$(detect_domain "$1")

if [ -z "$DOMAIN_NAME" ]; then
    print_error "DOMAIN_NAME could not be determined"
    exit 1
fi

echo -e "${BLUE}🔒 Setting up SSL certificate for domain: ${YELLOW}$DOMAIN_NAME${NC}"

# --- DNS preflight -----------------------------------------------------------
# Let's Encrypt validates over HTTP against whatever $DOMAIN_NAME resolves to.
# If DNS has not been cut over to this VM yet, certbot cannot issue a cert,
# nginx.conf (which references /etc/letsencrypt/live/$DOMAIN_NAME/...) fails
# `nginx -t`, and nginx is never reloaded. Catch that here with an actionable
# message instead of leaving the box on a stale config.
echo -e "${BLUE}🌐 Checking DNS for $DOMAIN_NAME...${NC}"
RESOLVED_IP=$(getent ahostsv4 "$DOMAIN_NAME" 2>/dev/null | awk 'NR==1 {print $1}')
PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || echo "")

if [ -z "$RESOLVED_IP" ]; then
    print_error "$DOMAIN_NAME does not resolve. Point an A record at this VM ($PUBLIC_IP) before deploying."
    exit 1
fi

if [ -n "$PUBLIC_IP" ] && [ "$RESOLVED_IP" != "$PUBLIC_IP" ]; then
    print_error "$DOMAIN_NAME resolves to $RESOLVED_IP but this VM is $PUBLIC_IP."
    print_error "Update DNS (and any load balancer / firewall rule) for the new hostname, then re-run the deploy."
    exit 1
fi

print_status "DNS OK: $DOMAIN_NAME -> $RESOLVED_IP"

# Install Certbot if not already installed
echo -e "${BLUE}📦 Installing Certbot for SSL certificates...${NC}"
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
print_status "Certbot installed"

# Ensure webroot directory exists for certificate validation
sudo mkdir -p /var/www/html
print_status "Webroot directory prepared"

# Check if SSL certificate already exists using Certbot's records (more reliable)
if sudo certbot certificates | grep -q "$DOMAIN_NAME"; then
    echo -e "${BLUE}🔐 SSL certificate already exists for $DOMAIN_NAME${NC}"
    print_status "Using existing SSL certificate"
    # Update domain in nginx config  
    sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
    print_status "Nginx SSL configuration updated"
else
    # Obtain SSL certificate from Let's Encrypt
    echo -e "${BLUE}🔐 Obtaining SSL certificate for $DOMAIN_NAME...${NC}"
    if sudo certbot --nginx -d $DOMAIN_NAME --non-interactive --agree-tos --email Pragati.Raj@celsiortech.com; then
        print_status "SSL certificate obtained and configured successfully"
    else
        print_warning "Certbot failed to configure SSL automatically, applying manual configuration..."
        # Force update nginx config with SSL settings
        sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
        print_status "Nginx SSL configuration updated manually"
    fi
fi

# Test and reload nginx configuration after SSL setup
if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Nginx configuration reloaded with SSL"
else
    print_error "Nginx configuration test failed after SSL setup"
    print_error "Nginx was NOT reloaded and is still serving the previous configuration."
    exit 1
fi

echo -e "${GREEN}🎉 SSL setup completed for $DOMAIN_NAME!${NC}"