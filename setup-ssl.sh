#!/bin/bash

# SSL Setup Script for AI Recruiter
# Run this after deploy-azure.sh if SSL is needed
# Ensure DOMAIN_NAME is set, e.g., export DOMAIN_NAME=qacurate.hoonr.ai

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

if [ -z "$DOMAIN_NAME" ]; then
    print_error "DOMAIN_NAME is not set. Please export DOMAIN_NAME=yourdomain.com"
    exit 1
fi

echo -e "${BLUE}🔒 Installing Certbot for SSL certificates...${NC}"
sudo apt install -y certbot python3-certbot-nginx
print_status "Certbot installed"

# Check if SSL certificate already exists
if [ -f /etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem ]; then
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
fi

echo -e "${GREEN}🎉 SSL setup completed for $DOMAIN_NAME!${NC}"