# Environment-Based Deployment Guide

This guide explains how to deploy the AI Recruiter application with automatic environment detection based on git branches.

## 🎯 Overview

The deployment system automatically detects which environment to deploy to based on:
1. **Command line argument** (highest priority)
2. **Environment variable** (`DOMAIN_NAME`)
3. **Git branch name** (auto-detection)
4. **Default fallback** (QA environment)

### Environment Mapping

| Git Branch | Environment | Domain | Description |
|------------|-------------|--------|-------------|
| `main` / `master` | **PRODUCTION** | `curate.hoonr.ai` | Live production environment |
| `develop` / `development` | **QA** | `qacurate.hoonr.ai` | Testing environment |
| Other branches | **QA** | `qacurate.hoonr.ai` | Default for feature branches |

## 🚀 Quick Start

### Automatic Deployment (Recommended)
```bash
# Deploy to environment based on current git branch
./deploy-azure.sh

# Check which environment will be used
./manage.sh env
```

### Manual Domain Override
```bash
# Deploy to specific domain (overrides auto-detection)
./deploy-azure.sh curate.hoonr.ai

# Or set environment variable
export DOMAIN_NAME=curate.hoonr.ai
./deploy-azure.sh
```

### SSL Setup
```bash
# Setup SSL for detected domain
./setup-ssl.sh

# Setup SSL for specific domain
./setup-ssl.sh curate.hoonr.ai
```

## 📋 Deployment Scripts

### `deploy-azure.sh` - Main Deployment Script
- **Purpose**: Deploy application with automatic environment detection
- **Features**:
  - Smart domain detection (CLI → env var → git branch → default)
  - Template-based nginx configuration
  - Automatic service management
  - Environment validation

**Usage:**
```bash
# Auto-detect environment
./deploy-azure.sh

# Specify domain
./deploy-azure.sh qacurate.hoonr.ai

# With environment variable
DOMAIN_NAME=curate.hoonr.ai ./deploy-azure.sh
```

### `setup-ssl.sh` - SSL Certificate Management
- **Purpose**: Setup SSL certificates with Let's Encrypt
- **Features**:
  - Two-stage SSL setup (avoids chicken-and-egg problem)
  - Automatic certificate renewal
  - Domain detection (same logic as deploy script)

**Usage:**
```bash
# Auto-detect domain for SSL
./setup-ssl.sh

# Specific domain
./setup-ssl.sh curate.hoonr.ai
```

### `manage.sh` - Application Management
- **Purpose**: Monitor and manage the application
- **New Commands**:
  - `./manage.sh env` - Show environment information
  - `./manage.sh status` - Service status with environment context

### `setup-production.sh` - Legacy Production Setup
- **Purpose**: Alternative production setup script
- **Note**: Use `deploy-azure.sh` + `setup-ssl.sh` instead for better automation

## 🔧 Configuration Templates

### nginx.conf Template
The nginx configuration uses template placeholders that are replaced during deployment:

```nginx
server_name {{DOMAIN_NAME}};
ssl_certificate /etc/letsencrypt/live/{{DOMAIN_NAME}}/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/{{DOMAIN_NAME}}/privkey.pem;
```

During deployment, `{{DOMAIN_NAME}}` is replaced with the actual domain.

## 📊 Environment Detection Logic

```bash
detect_domain() {
    # 1. Command line argument (highest priority)
    if [ -n "$1" ]; then
        echo "$1"
        return
    fi
    
    # 2. Environment variable
    if [ -n "$DOMAIN_NAME" ]; then
        echo "$DOMAIN_NAME"
        return
    fi
    
    # 3. Git branch detection
    current_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    case "$current_branch" in
        "main"|"master")
            echo "curate.hoonr.ai"  # PRODUCTION
            ;;
        "develop"|"development")
            echo "qacurate.hoonr.ai"  # QA
            ;;
        *)
            echo "qacurate.hoonr.ai"  # Default to QA
            ;;
    esac
}
```

## 🛠️ Common Workflows

### Deploying to QA
```bash
# Switch to develop branch
git checkout develop

# Deploy (automatically detects QA environment)
./deploy-azure.sh

# Setup SSL if needed
./setup-ssl.sh

# Check status
./manage.sh status
```

### Deploying to Production
```bash
# Switch to main branch
git checkout main

# Deploy (automatically detects PRODUCTION environment)
./deploy-azure.sh

# Setup SSL if needed
./setup-ssl.sh

# Verify deployment
./manage.sh env
curl -I https://curate.hoonr.ai
```

### Manual Run with Specific Domain
```bash
# Deploy to QA manually
./deploy-azure.sh qacurate.hoonr.ai

# Deploy to PROD manually
./deploy-azure.sh curate.hoonr.ai

# Or use environment variable
export DOMAIN_NAME=curate.hoonr.ai
./deploy-azure.sh
```

## 🔍 Verification Commands

### Check Environment
```bash
./manage.sh env
```

### Test Deployment
```bash
# Local services
curl -I http://localhost:8000/api/docs  # API
curl -I http://localhost:3000           # Web

# External access
curl -I https://curate.hoonr.ai        # PROD
curl -I https://qacurate.hoonr.ai      # QA
```

### SSL Certificate Status
```bash
# Check certificates
sudo certbot certificates

# Test SSL
openssl s_client -connect curate.hoonr.ai:443 -servername curate.hoonr.ai
```

## 🚨 Troubleshooting

### SSL Certificate Issues
1. **Certificate not found**: Run `./setup-ssl.sh`
2. **DNS issues**: Verify domain points to server IP
3. **Firewall**: Ensure ports 80 and 443 are open

### Deployment Issues
1. **Wrong domain**: Check with `./manage.sh env`
2. **Service not starting**: Check logs with `sudo journalctl -u airecruiter-api -f`
3. **Nginx errors**: Test config with `sudo nginx -t`

### Git Branch Issues
```bash
# Check current branch
git branch

# Switch environments
git checkout develop  # QA
git checkout main     # PRODUCTION
```

## 📁 File Structure

```
airecruiter/
├── deploy-azure.sh           # Main deployment script
├── setup-ssl.sh             # SSL setup script  
├── manage.sh                 # Application management
├── setup-production.sh       # Legacy production setup
├── nginx.conf                # Nginx template configuration
├── DEPLOYMENT_GUIDE.md       # This guide
└── systemd/
    ├── airecruiter-api.service
    └── airecruiter-web.service
```

## 💡 Best Practices

1. **Always use git branches** for automatic environment detection
2. **Test in QA first** before deploying to production
3. **Use SSL in production** - run `setup-ssl.sh` after deployment
4. **Monitor services** with `./manage.sh status`
5. **Check environment** with `./manage.sh env` before deployment
6. **Keep templates** - don't edit `/etc/nginx/sites-available/airecruiter` directly

## 🔄 CI/CD Integration

For automated deployments:

```bash
# In CI/CD pipeline
git checkout develop
./deploy-azure.sh  # Automatically deploys to qacurate.hoonr.ai

git checkout main  
./deploy-azure.sh  # Automatically deploys to curate.hoonr.ai
```

This system makes it easy to have different deployment targets based on git branches while maintaining configuration in a single repository.