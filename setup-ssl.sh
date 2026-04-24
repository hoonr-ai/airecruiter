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

echo -e "${BLUE}🔒 Setting up SSL certificate for domain: ${YELLOW}$DOMAIN_NAME${NC}"

# Install Certbot if not already installed
echo -e "${BLUE}📦 Installing Certbot for SSL certificates...${NC}"
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
print_status "Certbot installed"

# Ensure webroot directory exists for certificate validation
sudo mkdir -p /var/www/html
print_status "Webroot directory prepared"

# Check if SSL certificate already exists
if sudo certbot certificates 2>/dev/null | grep -q "$DOMAIN_NAME"; then
    echo -e "${BLUE}🔐 SSL certificate already exists for $DOMAIN_NAME${NC}"
    print_status "Using existing SSL certificate"
    
    # Show certificate info
    echo -e "${BLUE}ℹ${NC} Certificate details:"
    sudo certbot certificates | grep -A 10 "$DOMAIN_NAME"
else
    # Obtain SSL certificate from Let's Encrypt using webroot validation
    echo -e "${BLUE}🔐 Obtaining SSL certificate for $DOMAIN_NAME...${NC}"
    echo -e "${BLUE}ℹ${NC} Using webroot validation method"
    
    if sudo certbot certonly \
        --webroot \
        -w /var/www/html \
        -d $DOMAIN_NAME \
        --non-interactive \
        --agree-tos \
        --email Pragati.Raj@celsiortech.com \
        --expand; then
        
        print_status "SSL certificate obtained successfully"
        echo -e "${BLUE}ℹ${NC} Certificate stored in: /etc/letsencrypt/live/$DOMAIN_NAME/"
    else
        print_error "Failed to obtain SSL certificate"
        echo -e "${YELLOW}ℹ${NC} Common issues:"
        echo -e "  • Domain not pointing to this server"
        echo -e "  • Port 80 not accessible from internet"  
        echo -e "  • nginx not serving /.well-known/acme-challenge/"
        exit 1
    fi
fi

# Set up automatic renewal if not already configured
echo -e "${BLUE}🔄 Setting up automatic SSL renewal...${NC}"
if ! crontab -l 2>/dev/null | grep -q certbot; then
    (crontab -l 2>/dev/null; echo "0 12 * * * /usr/bin/certbot renew --quiet && /bin/systemctl reload nginx") | crontab -
    print_status "Automatic renewal configured"
else
    print_status "Automatic renewal already configured"
fi

# Reload nginx to apply SSL configuration (assuming deploy-azure.sh already set up nginx config)
echo -e "${BLUE}🔄 Switching to HTTPS nginx configuration...${NC}"

# Now that we have SSL certificates, apply the full HTTPS configuration
PROJECT_DIR="/home/ubuntu/codebase/airecruiter"
if [ -f "$PROJECT_DIR/nginx.conf" ]; then
    # Update nginx config with the correct domain (replace template placeholder)
    sed -i "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" "$PROJECT_DIR/nginx.conf"
    sudo cp "$PROJECT_DIR/nginx.conf" /etc/nginx/sites-available/airecruiter
    # Restore template for next deployment
    sed -i "s/$DOMAIN_NAME/{{DOMAIN_NAME}}/g" "$PROJECT_DIR/nginx.conf"
    print_status "HTTPS configuration applied"
else
    print_error "nginx.conf template not found at $PROJECT_DIR/nginx.conf"
    exit 1
fi

if sudo nginx -t; then
    sudo systemctl reload nginx
    print_status "Nginx reloaded with HTTPS configuration"
else
    print_warning "Nginx configuration test failed - please check nginx config manually"
    echo -e "${BLUE}ℹ${NC} You may need to update nginx configuration to use the SSL certificate"
fi

# Verify SSL certificate installation
echo -e "${BLUE}🔍 Verifying SSL certificate...${NC}"
sleep 2  # Give nginx a moment to reload

if timeout 10 openssl s_client -connect $DOMAIN_NAME:443 -servername $DOMAIN_NAME < /dev/null 2>/dev/null | grep -q 'Certificate chain'; then
    print_status "SSL certificate is working correctly"
    
    # Show certificate expiry
    expiry=$(echo | openssl s_client -servername $DOMAIN_NAME -connect $DOMAIN_NAME:443 2>/dev/null | openssl x509 -noout -dates | grep notAfter | cut -d= -f2)
    echo -e "${BLUE}ℹ${NC} Certificate expires: $expiry"
else
    print_warning "SSL certificate verification failed"
    echo -e "${YELLOW}ℹ${NC} This could be due to:"
    echo -e "  • DNS propagation delay"
    echo -e "  • Firewall blocking port 443"
    echo -e "  • nginx configuration issues"
fi

echo ""
echo -e "${GREEN}🎉 SSL setup completed for $DOMAIN_NAME!${NC}"
echo -e "${BLUE}🌐 Your application should now be available at:${NC}"
echo -e "  • Web App: https://$DOMAIN_NAME"  
echo -e "  • API Docs: https://$DOMAIN_NAME/api/docs"
echo ""
echo -e "${YELLOW}📋 Useful commands:${NC}"
echo -e "  • Test HTTPS: curl -I https://$DOMAIN_NAME"
echo -e "  • Check certificates: sudo certbot certificates"
echo -e "  • Renew manually: sudo certbot renew"
echo -e "  • View nginx logs: sudo journalctl -u nginx -f"