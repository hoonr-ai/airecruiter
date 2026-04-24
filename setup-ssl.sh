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
            echo "qacurate.hoonr.ai"
            ;;
        *)
            echo "qacurate.hoonr.ai"
            ;;
    esac
}

# Detect and set domain
DOMAIN_NAME=$(detect_domain "$1")

if [ -z "$DOMAIN_NAME" ]; then
    print_error "DOMAIN_NAME could not be determined"
    exit 1
fi

echo -e "${BLUE}🔒 Setting up SSL for domain: ${YELLOW}$DOMAIN_NAME${NC}"

echo -e "${BLUE}🔒 Installing Certbot for SSL certificates...${NC}"
sudo apt install -y certbot python3-certbot-nginx
print_status "Certbot installed"

# Create temporary HTTP-only nginx configuration for certificate validation
echo -e "${BLUE}📋 Creating temporary HTTP-only nginx configuration...${NC}"
cat > /tmp/nginx-temp-http.conf << EOF
# Temporary HTTP-only configuration for SSL certificate validation
upstream airecruiter_api {
    server 127.0.0.1:8000;
    keepalive 32;
}

upstream airecruiter_web {
    server 127.0.0.1:3000;
    keepalive 32;
}

limit_req_zone \$binary_remote_addr zone=api_limit:10m rate=30r/m;
limit_req_zone \$binary_remote_addr zone=web_limit:10m rate=600r/m;

# HTTP server for certificate validation and serving content
server {
    listen 80;
    server_name $DOMAIN_NAME;
    
    # Allow Let's Encrypt validation
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    
    # API routes
    location /api/ {
        limit_req zone=api_limit burst=10 nodelay;
        proxy_pass http://airecruiter_api;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 86400;
    }
    
    # Backend API endpoints (non-/api/ prefix)
    location ~ ^/(jobs|chat|candidates)/ {
        limit_req zone=api_limit burst=10 nodelay;
        proxy_pass http://airecruiter_api\$uri\$is_args\$args;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 86400;
    }
    
    # All other requests go to Next.js frontend
    location / {
        limit_req zone=web_limit burst=100 nodelay;
        proxy_pass http://airecruiter_web;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_cache_bypass \$http_upgrade;
        proxy_read_timeout 86400;
    }
}
EOF

# Apply temporary HTTP configuration
sudo cp /tmp/nginx-temp-http.conf /etc/nginx/sites-available/airecruiter
sudo mkdir -p /var/www/html

# Test and reload nginx with HTTP-only config
if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Temporary HTTP-only configuration applied"
else
    print_error "Failed to apply temporary HTTP configuration"
    exit 1
fi

# Check if SSL certificate already exists using Certbot's records (more reliable)
if sudo certbot certificates | grep -q "$DOMAIN_NAME"; then
    echo -e "${BLUE}🔐 SSL certificate already exists for $DOMAIN_NAME${NC}"
    print_status "Using existing SSL certificate"
else
    # Obtain SSL certificate from Let's Encrypt (HTTP validation)
    echo -e "${BLUE}🔐 Obtaining SSL certificate for $DOMAIN_NAME...${NC}"
    if sudo certbot certonly --webroot -w /var/www/html -d $DOMAIN_NAME --non-interactive --agree-tos --email Pragati.Raj@celsiortech.com; then
        print_status "SSL certificate obtained successfully"
    else
        print_error "Failed to obtain SSL certificate"
        exit 1
    fi
fi

# Now apply the full HTTPS nginx configuration
echo -e "${BLUE}🔒 Applying HTTPS nginx configuration...${NC}"
sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
# Replace template placeholder with actual domain
sudo sed -i "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" /etc/nginx/sites-available/airecruiter
print_status "HTTPS configuration applied"

# Test and reload nginx configuration after SSL setup
if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Nginx configuration reloaded with SSL"
else
    print_error "Nginx configuration test failed after SSL setup"
    echo -e "${BLUE}ℹ${NC} Checking nginx error details..."
    sudo nginx -t
    exit 1
fi

# Clean up temporary files
rm -f /tmp/nginx-temp-http.conf

# Verify SSL certificate installation
echo -e "${BLUE}🔍 Verifying SSL certificate...${NC}"
if openssl s_client -connect $DOMAIN_NAME:443 -servername $DOMAIN_NAME < /dev/null 2>/dev/null | grep -q 'Certificate chain'; then
    print_status "SSL certificate is working"
else
    print_warning "SSL certificate verification failed - check DNS and firewall settings"
fi

echo -e "${GREEN}🎉 SSL setup completed for $DOMAIN_NAME!${NC}"
echo -e "${BLUE}🌐 Your application is now available at:${NC}"
echo -e "- Web App: https://$DOMAIN_NAME"  
echo -e "- API Docs: https://$DOMAIN_NAME/api/docs"
echo ""
echo -e "${YELLOW}📋 Verification Commands:${NC}"
echo -e "- Test SSL: curl -I https://$DOMAIN_NAME"
echo -e "- Check cert: sudo certbot certificates"
echo -e "- View logs: sudo journalctl -u nginx -f"