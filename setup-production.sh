#!/bin/bash

# SSL Setup and Production Configuration Script
# Updated to work with template-based nginx configuration
# Run this after deploy-azure.sh and updating your API keys

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m' 
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

DOMAIN_NAME="${1:-}"
EMAIL="${2:-Pragati.Raj@celsiortech.com}"

if [ -z "$DOMAIN_NAME" ]; then
    echo -e "${RED}❌ Please provide your domain name:${NC}"
    echo "Usage: ./setup-production.sh yourdomain.com [admin@yourdomain.com]"
    echo "Example: ./setup-production.sh curate.hoonr.ai"
    echo ""
    echo -e "${BLUE}📝 NOTE:${NC} For automatic domain detection, use ./deploy-azure.sh instead"
    exit 1
fi

echo -e "${BLUE}🔐 Setting up production configuration for $DOMAIN_NAME${NC}"

# Install certbot for Let's Encrypt
echo -e "${BLUE}📜 Installing Certbot for SSL certificate...${NC}"
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# Setup Nginx configuration using template system
echo -e "${BLUE}⚙️ Configuring Nginx with template system...${NC}"
sudo cp nginx.conf /etc/nginx/sites-available/airecruiter

# Replace template placeholder with actual domain
sudo sed -i "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" /etc/nginx/sites-available/airecruiter
echo -e "${GREEN}✓${NC} Template processed with domain: $DOMAIN_NAME"

# Enable site
sudo ln -sf /etc/nginx/sites-available/airecruiter /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test nginx configuration
if sudo nginx -t; then
    echo -e "${GREEN}✓${NC} Nginx configuration is valid"
    sudo systemctl reload nginx
else
    echo -e "${RED}❌ Nginx configuration error${NC}"
    exit 1
fi

# Obtain SSL certificate (using webroot method for better compatibility)
echo -e "${BLUE}🔒 Obtaining SSL certificate from Let's Encrypt...${NC}"

# Create webroot directory if it doesn't exist
sudo mkdir -p /var/www/html

# First, try to obtain certificate with HTTP validation
if sudo certbot certonly --webroot -w /var/www/html -d "$DOMAIN_NAME" --non-interactive --agree-tos --email "$EMAIL"; then
    echo -e "${GREEN}✓${NC} SSL certificate obtained successfully"
    # Reload nginx to apply SSL configuration
    sudo systemctl reload nginx
else
    echo -e "${YELLOW}⚠${NC} SSL certificate setup failed, continuing with HTTP. You can run ./setup-ssl.sh later."
fi

# Update API environment with correct domain
if [ -f "/home/ubuntu/codebase/airecruiter/apps/api/.env" ]; then
    echo -e "${BLUE}🔧 Updating API environment configuration...${NC}"
    # Update ALLOWED_ORIGINS to include the new domain
    if grep -q "ALLOWED_ORIGINS=" /home/ubuntu/codebase/airecruiter/apps/api/.env; then
        sed -i "s#ALLOWED_ORIGINS=.*#ALLOWED_ORIGINS=*,https://$DOMAIN_NAME,http://$DOMAIN_NAME#" /home/ubuntu/codebase/airecruiter/apps/api/.env
    else
        echo "ALLOWED_ORIGINS=*,https://$DOMAIN_NAME,http://$DOMAIN_NAME" >> /home/ubuntu/codebase/airecruiter/apps/api/.env
    fi
    echo -e "${GREEN}✓${NC} API environment updated"
else
    echo -e "${YELLOW}⚠${NC} API .env file not found, skipping environment update"
fi

# Restart services
sudo systemctl restart airecruiter-api
sudo systemctl restart airecruiter-web
sudo systemctl reload nginx

# Setup auto-renewal for SSL certificate
echo -e "${BLUE}🔄 Setting up automatic SSL certificate renewal...${NC}"
(crontab -l 2>/dev/null; echo "0 12 * * * /usr/bin/certbot renew --quiet") | crontab -

# Create monitoring script with Azure Database support
sudo mkdir -p /var/log/airecruiter
sudo tee /usr/local/bin/airecruiter-monitor.sh > /dev/null << 'EOF'
#!/bin/bash
LOG_FILE="/var/log/airecruiter/monitor.log"

check_service() {
    if systemctl is-active --quiet $1; then
        echo "$(date): $1 is running" >> $LOG_FILE
    else
        echo "$(date): $1 is down, restarting..." >> $LOG_FILE
        systemctl restart $1
        sleep 10
        if systemctl is-active --quiet $1; then
            echo "$(date): $1 restarted successfully" >> $LOG_FILE
        else
            echo "$(date): Failed to restart $1" >> $LOG_FILE
        fi
    fi
}

check_service "airecruiter-api"
check_service "airecruiter-web" 
check_service "nginx"
# Note: Using Azure Database for PostgreSQL (managed service)
EOF

sudo chmod +x /usr/local/bin/airecruiter-monitor.sh

# Add monitoring to cron (every 5 minutes)
(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/airecruiter-monitor.sh") | crontab -

echo -e "${GREEN}🎉 Production setup completed!${NC}"
echo -e "${BLUE}Your application is now available at:${NC}"
echo -e "Web: https://$DOMAIN_NAME"
echo -e "API: https://$DOMAIN_NAME/api"
echo -e "API Docs: https://$DOMAIN_NAME/api/docs"
echo ""
echo -e "${YELLOW}📋 Final steps:${NC}"
echo -e "1. Update API keys in /home/ubuntu/codebase/airecruiter/apps/api/.env"
echo -e "2. Test all endpoints at https://$DOMAIN_NAME/api/docs" 
echo -e "3. Monitor logs with: sudo journalctl -u airecruiter-api -f"
echo -e "4. Check monitoring logs: tail -f /var/log/airecruiter/monitor.log"
echo ""
echo -e "${BLUE}🚀 Management Commands:${NC}"
echo -e "- Show status: ./manage.sh status"
echo -e "- Show environment: ./manage.sh env"
echo -e "- Deploy updates: ./deploy-azure.sh"
echo -e "- Setup SSL later: ./setup-ssl.sh"