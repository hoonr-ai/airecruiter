#!/bin/bash

# Production Configuration Script
# ⚠️ NOTE: This script is legacy. Use setup-ssl.sh instead for SSL configuration.
# This script is kept for reference and custom production setups.

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m' 
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

# Function to detect environment domain (same logic as other scripts)
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

DOMAIN_NAME=$(detect_domain "$1")
EMAIL="${2:-Pragati.Raj@celsiortech.com}"

echo -e "${YELLOW}⚠️ This script is legacy. For SSL setup, use: ./setup-ssl.sh${NC}"
echo -e "${BLUE}🔐 Setting up production configuration for $DOMAIN_NAME${NC}"

# Install certbot for Let's Encrypt
echo -e "${BLUE}📜 Installing Certbot for SSL certificate...${NC}"
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# Setup Nginx configuration
echo -e "${BLUE}⚙️ Configuring Nginx...${NC}"

# Copy nginx config and replace template placeholder
cp nginx.conf /tmp/nginx-temp.conf
sed -i "s/{{DOMAIN_NAME}}/$DOMAIN_NAME/g" /tmp/nginx-temp.conf
sudo cp /tmp/nginx-temp.conf /etc/nginx/sites-available/airecruiter
rm /tmp/nginx-temp.conf

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

# Obtain SSL certificate
echo -e "${BLUE}🔒 Obtaining SSL certificate from Let's Encrypt...${NC}"
sudo certbot --nginx -d "$DOMAIN_NAME" --non-interactive --agree-tos --email "$EMAIL"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} SSL certificate obtained successfully"
else
    echo -e "${YELLOW}⚠${NC} SSL certificate setup failed, continuing with HTTP"
fi

# Update API environment with correct domain (if ALLOWED_ORIGINS exists in .env)
API_ENV_FILE="/home/ubuntu/codebase/airecruiter/apps/api/.env"
if [ -f "$API_ENV_FILE" ] && grep -q "ALLOWED_ORIGINS" "$API_ENV_FILE"; then
    sed -i "s|ALLOWED_ORIGINS=.*|ALLOWED_ORIGINS=https://$DOMAIN_NAME,http://$DOMAIN_NAME|" "$API_ENV_FILE"
    echo -e "${GREEN}✓${NC} Updated API CORS origins for $DOMAIN_NAME"
fi

# Restart services
sudo systemctl restart airecruiter-api
sudo systemctl restart airecruiter-web
sudo systemctl reload nginx

# Setup auto-renewal for SSL certificate
echo -e "${BLUE}🔄 Setting up automatic SSL certificate renewal...${NC}"
(crontab -l 2>/dev/null; echo "0 12 * * * /usr/bin/certbot renew --quiet") | crontab -

# Create monitoring script
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
echo -e "API Docs: https://$DOMAIN_NAME/api/docs"
echo ""
echo -e "${YELLOW}📋 Recommended workflow:${NC}"
echo -e "1. Use ./deploy-azure.sh for future deployments"
echo -e "2. Use ./setup-ssl.sh for SSL configuration"
echo -e "3. Use ./manage.sh status to check system health"
echo -e "4. Update API keys in /home/ubuntu/codebase/airecruiter/apps/api/.env"
echo -e "5. Monitor logs with: ./manage.sh logs"