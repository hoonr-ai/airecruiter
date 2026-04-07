#!/bin/bash

# SSL Setup and Production Configuration Script
# Run this after deploy.sh and updating your API keys

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m' 
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

DOMAIN_NAME="${1:-}"
EMAIL="${2:-admin@example.com}"

if [ -z "$DOMAIN_NAME" ]; then
    echo -e "${RED}❌ Please provide your domain name:${NC}"
    echo "Usage: ./setup-production.sh yourdomain.com [admin@yourdomain.com]"
    echo "Example: ./setup-production.sh myapp.example.com admin@example.com"
    exit 1
fi

echo -e "${BLUE}🔐 Setting up production configuration for $DOMAIN_NAME${NC}"

# Install certbot for Let's Encrypt
echo -e "${BLUE}📜 Installing Certbot for SSL certificate...${NC}"
sudo apt update
sudo apt install -y certbot python3-certbot-nginx

# Setup Nginx configuration
echo -e "${BLUE}⚙️ Configuring Nginx...${NC}"
sudo cp nginx.conf /etc/nginx/sites-available/airecruiter

# Update domain name in nginx config
sudo sed -i "s/your-domain.com/$DOMAIN_NAME/g" /etc/nginx/sites-available/airecruiter
sudo sed -i "s/www.your-domain.com/www.$DOMAIN_NAME/g" /etc/nginx/sites-available/airecruiter

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
sudo certbot --nginx -d "$DOMAIN_NAME" -d "www.$DOMAIN_NAME" --non-interactive --agree-tos --email "$EMAIL"

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} SSL certificate obtained successfully"
else
    echo -e "${YELLOW}⚠${NC} SSL certificate setup failed, continuing with HTTP"
fi

# Update API environment with correct domain
sed -i "s/ALLOWED_ORIGINS=.*/ALLOWED_ORIGINS=*,https:\/\/$DOMAIN_NAME,https:\/\/www.$DOMAIN_NAME,http:\/\/20.57.137.251,http:\/\/20.57.137.251:3000/" /home/ubuntu/codebase/airecruiter/apps/api/.env

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
check_service "postgresql"
# Redis not used in current application
EOF

sudo chmod +x /usr/local/bin/airecruiter-monitor.sh

# Add monitoring to cron (every 5 minutes)
(crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/airecruiter-monitor.sh") | crontab -

echo -e "${GREEN}🎉 Production setup completed!${NC}"
echo -e "${BLUE}Your application is now available at:${NC}"
echo -e "Web: https://$DOMAIN_NAME"
echo -e "API: https://$DOMAIN_NAME/api"
echo -e "API Docs: https://$DOMAIN_NAME/docs"
echo ""
echo -e "${YELLOW}📋 Final steps:${NC}"
echo -e "1. Update API keys in /home/ubuntu/codebase/airecruiter/apps/api/.env"
echo -e "2. Test all endpoints at https://$DOMAIN_NAME/docs" 
echo -e "3. Monitor logs with: sudo journalctl -u airecruiter-api -f"
echo -e "4. Check monitoring logs: tail -f /var/log/airecruiter/monitor.log"