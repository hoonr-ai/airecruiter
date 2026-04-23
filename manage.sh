#!/bin/bash

# AI Recruiter Management Script
# Provides easy commands for managing the application

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

API_DIR="/home/ubuntu/codebase/airecruiter/apps/api"
WEB_DIR="/home/ubuntu/codebase/airecruiter/apps/web"

show_help() {
    echo -e "${BLUE}AI Recruiter Management Script${NC}"
    echo ""
    echo "Usage: ./manage.sh [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  status      - Show status of all services"
    echo "  start       - Start all services"  
    echo "  stop        - Stop all services"
    echo "  restart     - Restart all services"
    echo "  deploy      - Redeploy application (git pull, build, restart)"
    echo "  logs        - Show logs for all services"
    echo "  api-logs    - Show API logs only"
    echo "  web-logs    - Show web logs only"
    echo "  health      - Perform health checks"
    echo "  monitor     - Real-time monitoring dashboard"
    echo "  cleanup     - Clean logs and temporary files"
    echo ""
}

check_service() {
    local service_name=$1
    if systemctl is-active --quiet $service_name; then
        echo -e "${GREEN}✓${NC} $service_name: Running"
    else
        echo -e "${RED}✗${NC} $service_name: Stopped"
    fi
}

show_status() {
    echo -e "${BLUE}📊 Service Status${NC}"
    echo "===================="
    check_service "airecruiter-api"
    check_service "airecruiter-web"
    check_service "nginx"
    echo ""
    
    echo -e "${BLUE}🗄️ Database Status${NC}"
    echo "===================="
    echo "Using Azure Database for PostgreSQL (managed service)"
    echo ""
    
    echo -e "${BLUE}🌐 Network Status${NC}"
    echo "===================="
    if curl -s http://localhost:8000/docs > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} API: Responding locally on port 8000"
    else
        echo -e "${RED}✗${NC} API: Not responding locally on port 8000"
    fi
    
    if curl -s http://localhost:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}✓${NC} Web: Responding locally on port 3000"
    else
        echo -e "${RED}✗${NC} Web: Not responding locally on port 3000"
    fi
    
    # Check external access if domain is configured
    if [ ! -z "$DOMAIN_NAME" ]; then
        if curl -s http://$DOMAIN_NAME/api/docs > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} API: Responding externally via $DOMAIN_NAME"
        else
            echo -e "${YELLOW}⚠${NC} API: Not accessible via $DOMAIN_NAME (check DNS/firewall)"
        fi
        
        if curl -s http://$DOMAIN_NAME > /dev/null 2>&1; then
            echo -e "${GREEN}✓${NC} Web: Responding externally via $DOMAIN_NAME"
        else
            echo -e "${YELLOW}⚠${NC} Web: Not accessible via $DOMAIN_NAME (check DNS/firewall)"
        fi
    fi
    echo ""
}

deploy_application() {
    echo -e "${BLUE}🚀 Deploying AI Recruiter application...${NC}"
    
    # Pull latest code
    echo -e "${BLUE}📥 Pulling latest code...${NC}"
    git pull || {
        echo -e "${RED}✗${NC} Git pull failed"
        return 1
    }
    
    # Stop services
    echo -e "${BLUE}🛑 Stopping services...${NC}"
    sudo systemctl stop airecruiter-web airecruiter-api
    
    # Install dependencies
    echo -e "${BLUE}📦 Installing API dependencies...${NC}"
    cd "$API_DIR"
    source venv/bin/activate
    pip install -r requirements.txt
    
    echo -e "${BLUE}📦 Installing web dependencies...${NC}"
    cd "$WEB_DIR"
    npm install --legacy-peer-deps
    
    # Build web application
    echo -e "${BLUE}🏗️ Building web application...${NC}"
    npm run build
    
    # Reload systemd and restart services
    echo -e "${BLUE}🔄 Reloading services...${NC}"
    sudo systemctl daemon-reload
    sudo systemctl start airecruiter-api
    sleep 3
    sudo systemctl start airecruiter-web
    
    echo -e "${GREEN}✓${NC} Deployment completed successfully!"
    
    # Show status
    sleep 2
    show_status
}

start_services() {
    echo -e "${BLUE}🚀 Starting services...${NC}"
    sudo systemctl start nginx
    sleep 2
    sudo systemctl start airecruiter-api
    sleep 3
    sudo systemctl start airecruiter-web
    echo -e "${GREEN}✓ Services started${NC}"
}

stop_services() {
    echo -e "${BLUE}🛑 Stopping services...${NC}"
    sudo systemctl stop airecruiter-web airecruiter-api
    echo -e "${GREEN}✓ Application services stopped${NC}"
    echo "Azure Database for PostgreSQL continues running (managed service)"
}

restart_services() {
    echo -e "${BLUE}🔄 Restarting services...${NC}"
    sudo systemctl restart airecruiter-api
    sleep 3
    sudo systemctl restart airecruiter-web
    sudo systemctl reload nginx
    echo -e "${GREEN}✓ Services restarted${NC}"
}

show_logs() {
    echo -e "${BLUE}📋 Showing logs (Press Ctrl+C to stop)${NC}"
    sudo journalctl -f -u airecruiter-api -u airecruiter-web -u nginx
}

api_logs() {
    echo -e "${BLUE}📋 API Logs (Press Ctrl+C to stop)${NC}"
    sudo journalctl -f -u airecruiter-api
}

web_logs() {
    echo -e "${BLUE}📋 Web Logs (Press Ctrl+C to stop)${NC}"
    sudo journalctl -f -u airecruiter-web
}

health_check() {
    echo -e "${BLUE}🏥 Performing health checks...${NC}"
    echo ""
    
    # Database connection check
    echo -n "Database connection: "
    if sudo -u postgres psql -d airecruiter_db -c "SELECT 1;" > /dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi
    
    # Redis connection check (currently not used)
    # echo -n "Redis connection: "
    # if redis-cli ping > /dev/null 2>&1; then
    #     echo -e "${GREEN}OK${NC}"
    # else
    #     echo -e "${RED}FAILED${NC}"
    # fi
    
    # API endpoint check
    echo -n "API health: "
    if curl -s http://localhost:8000/docs > /dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi
    
    # Web app check  
    echo -n "Web app: "
    if curl -s http://localhost:3000 > /dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}FAILED${NC}"
    fi
    
    # Disk space check
    echo -n "Disk space: "
    DISK_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
    if [ $DISK_USAGE -lt 80 ]; then
        echo -e "${GREEN}OK (${DISK_USAGE}% used)${NC}"
    elif [ $DISK_USAGE -lt 90 ]; then
        echo -e "${YELLOW}WARNING (${DISK_USAGE}% used)${NC}"
    else
        echo -e "${RED}CRITICAL (${DISK_USAGE}% used)${NC}"
    fi
    
    # Memory check
    echo -n "Memory usage: "
    MEM_USAGE=$(free | awk '/^Mem:/ {printf "%.1f", $3/$2 * 100.0}')
    if (( $(echo "$MEM_USAGE < 80" | bc -l) )); then
        echo -e "${GREEN}OK (${MEM_USAGE}% used)${NC}"
    elif (( $(echo "$MEM_USAGE < 90" | bc -l) )); then
        echo -e "${YELLOW}WARNING (${MEM_USAGE}% used)${NC}"
    else
        echo -e "${RED}CRITICAL (${MEM_USAGE}% used)${NC}"
    fi
    echo ""
}

update_app() {
    echo -e "${BLUE}📥 Updating application...${NC}"
    
    # Pull latest code
    git pull origin main
    
    # Update API dependencies
    echo -e "${BLUE}🐍 Updating API dependencies...${NC}"
    cd "$API_DIR"
    source venv/bin/activate
    pip install -r requirements.txt --upgrade
    
    # Update web dependencies
    echo -e "${BLUE}⚛️ Updating web dependencies...${NC}"
    cd "$WEB_DIR"
    npm install --legacy-peer-deps
    npm run build
    
    # Restart services
    restart_services
    echo -e "${GREEN}✓ Application updated${NC}"
}



monitor_dashboard() {
    echo -e "${BLUE}📊 Real-time monitoring (Press Ctrl+C to stop)${NC}"
    echo "======================================================"
    
    while true; do
        clear
        echo -e "${BLUE}AI Recruiter Monitoring Dashboard${NC}"
        echo "Updated: $(date)"
        echo "======================================================"
        
        # System resources
        echo -e "${BLUE}System Resources:${NC}"
        echo "CPU: $(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)% used"
        echo "Memory: $(free -h | awk '/^Mem:/ {printf "%.1f%% (", $3/$2 * 100.0} {print $3"/"$2")"}')"
        echo "Disk: $(df -h / | awk 'NR==2 {print $5" ("$3"/"$2")"}')"
        
        echo ""
        echo -e "${BLUE}Service Status:${NC}"
        check_service "airecruiter-api"
        check_service "airecruiter-web"
        check_service "nginx"
        
        echo ""
        echo -e "${BLUE}Recent Activity:${NC}"
        echo "API requests (last 1m):"
        sudo journalctl -u airecruiter-api --since "1 minute ago" | grep -c "INFO" || echo "0"
        
        echo "Error logs (last 5m):"
        sudo journalctl -u airecruiter-api -u airecruiter-web --since "5 minutes ago" | grep -c "ERROR" || echo "0"
        
        sleep 5
    done
}

cleanup() {
    echo -e "${BLUE}🧹 Cleaning up logs and temporary files...${NC}"
    
    # Clean old logs
    sudo journalctl --vacuum-time=7d
    
    # Clean temporary files
    sudo rm -rf /tmp/airecruiter_*
    
    # Clean Python cache
    find "$API_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$API_DIR" -name "*.pyc" -delete 2>/dev/null || true
    
    # Clean npm cache
    cd "$WEB_DIR"
    npm cache clean --force 2>/dev/null || true
    
    echo -e "${GREEN}✓ Cleanup completed${NC}"
}

# Main command handling
case "${1:-help}" in
    "status")
        show_status
        ;;
    "start")
        start_services
        ;;
    "stop")
        stop_services
        ;;
    "restart")
        restart_services
        ;;
    "deploy")
        deploy_application
        ;;
    "logs")
        show_logs
        ;;
    "api-logs")
        api_logs
        ;;
    "web-logs")
        web_logs
        ;;
    "health")
        health_check
        ;;
    "update")
        update_app
        ;;
    "monitor")
        monitor_dashboard
        ;;
    "cleanup")
        cleanup
        ;;
    "help"|*)
        show_help
        ;;
esac