# AI Recruiter Deployment Guide

This guide covers deployment procedures for both manual and automated (CI/CD) deployments across QA and Production environments.

## 🌍 Environment Overview

| Environment | Domain | Branch | VM |
|-------------|--------|--------|-----|
| **QA** | `qacurate.hoonr.ai` | `develop` | Azure QA VM |
| **PRODUCTION** | `curate.hoonr.ai` | `main` | Azure PROD VM |

## 🚀 Manual Deployment Commands

### Option 1: Auto-Detect from Git Branch (Recommended)

```bash
# Deploy based on current branch (automatically detects QA/PROD)
./deploy-azure.sh

# Setup SSL for detected domain  
./setup-ssl.sh

# Check environment info
./manage.sh env

# Verify deployment
./manage.sh status
```

### Option 2: Explicit Domain Override

```bash
# Deploy to QA explicitly
./deploy-azure.sh qacurate.hoonr.ai
./setup-ssl.sh qacurate.hoonr.ai

# Deploy to PROD explicitly  
./deploy-azure.sh curate.hoonr.ai
./setup-ssl.sh curate.hoonr.ai
```

### Option 3: Environment Variable Method (Same as CI/CD)

```bash
# QA Deployment
export DOMAIN_NAME="qacurate.hoonr.ai"
./deploy-azure.sh && ./setup-ssl.sh

# PROD Deployment  
export DOMAIN_NAME="curate.hoonr.ai"
./deploy-azure.sh && ./setup-ssl.sh
```

## 📋 Branch-Based Workflow

### Smart Domain Detection

The deployment scripts automatically detect the environment based on your current git branch:

| Current Branch | Auto-Detected Domain | Environment | Notes |
|----------------|---------------------|-------------|-------|
| `main` / `master` | `curate.hoonr.ai` | **PRODUCTION** | Live environment |
| `develop` / `development` | `qacurate.hoonr.ai` | **QA** | Testing environment |
| `feature/*` | `qacurate.hoonr.ai` | **QA (default)** | Feature branches default to QA |

### Development Workflow

```bash
# Working on QA Environment
git checkout develop
git pull origin develop
./deploy-azure.sh      # Auto-detects QA domain
./setup-ssl.sh         # Auto-detects QA domain

# Working on Production
git checkout main
git pull origin main
./deploy-azure.sh      # Auto-detects PROD domain
./setup-ssl.sh         # Auto-detects PROD domain
```

## 🔧 Management Commands

### Quick Management

```bash
./manage.sh status      # Check service status and connectivity
./manage.sh env         # Show environment and domain information
./manage.sh deploy      # Quick redeploy (git pull + build + restart)
./manage.sh logs        # View live logs from all services
./manage.sh restart     # Restart all services
./manage.sh health      # Perform health checks
```

### Service-Specific Commands

```bash
./manage.sh api-logs    # Show API logs only
./manage.sh web-logs    # Show web logs only
./manage.sh monitor     # Real-time monitoring dashboard
./manage.sh cleanup     # Clean logs and temporary files
```

## 🤖 CI/CD Deployment (Automated)

### GitHub Actions Workflow

The CI/CD pipeline automatically deploys based on branch pushes:

- **QA Deployment**: Push to `develop` branch
- **PROD Deployment**: Push to `main` branch

### Workflow Steps

1. **Code Push**: Push changes to `develop` or `main` branch
2. **Auto Deploy**: GitHub Actions triggers deployment
3. **Domain Detection**: Pipeline sets correct `DOMAIN_NAME` environment variable
4. **Deployment**: Runs `deploy-azure.sh` and `setup-ssl.sh`
5. **SSL Setup**: Automatically configures HTTPS certificates

### Pipeline Commands (Reference)

```bash
# QA Pipeline (develop branch)
export DOMAIN_NAME="qacurate.hoonr.ai"
bash deploy-azure.sh && bash setup-ssl.sh

# PROD Pipeline (main branch)  
export DOMAIN_NAME="curate.hoonr.ai"
bash deploy-azure.sh && bash setup-ssl.sh
```

## 📁 File Structure

```
airecruiter/
├── deploy-azure.sh           # Main deployment script
├── setup-ssl.sh             # SSL certificate setup
├── setup-production.sh      # Legacy production setup
├── manage.sh                 # Application management commands
├── nginx.conf               # Nginx configuration template
├── .github/workflows/
│   └── deploy.yml           # CI/CD pipeline configuration
└── systemd/
    ├── airecruiter-api.service
    └── airecruiter-web.service
```

## 🌐 Access URLs

### QA Environment
- **Web App**: https://qacurate.hoonr.ai
- **API Docs**: https://qacurate.hoonr.ai/api/docs
- **Direct API**: http://qacurate.hoonr.ai:8000/docs
- **Direct Web**: http://qacurate.hoonr.ai:3000

### Production Environment
- **Web App**: https://curate.hoonr.ai
- **API Docs**: https://curate.hoonr.ai/api/docs
- **Direct API**: http://curate.hoonr.ai:8000/docs
- **Direct Web**: http://curate.hoonr.ai:3000

## 🔍 Verification Commands

### Check Current Environment

```bash
# Show detected environment and domain
./manage.sh env

# Check git branch
git branch --show-current

# Verify nginx configuration
sudo grep "server_name" /etc/nginx/sites-available/airecruiter

# Check SSL certificates
sudo certbot certificates
```

### Health Checks

```bash
# Full system health check
./manage.sh health

# Check service status
./manage.sh status

# Test local connectivity
curl -s http://localhost:8000/docs
curl -s http://localhost:3000

# Test external connectivity (replace with your domain)
curl -s https://qacurate.hoonr.ai/api/docs
curl -s https://curate.hoonr.ai/api/docs
```

## 🚨 Troubleshooting

### Common Issues

#### 1. Services Not Starting
```bash
# Check service logs
./manage.sh logs

# Check individual service logs
sudo journalctl -u airecruiter-api -f
sudo journalctl -u airecruiter-web -f

# Restart services
./manage.sh restart
```

#### 2. SSL Certificate Issues
```bash
# Check certificate status
sudo certbot certificates

# Force renew certificate
sudo certbot renew --force-renewal

# Re-setup SSL
./setup-ssl.sh
```

#### 3. Nginx Configuration Issues
```bash
# Test nginx configuration
sudo nginx -t

# Check nginx error log
sudo tail -f /var/log/nginx/error.log

# Reload nginx
sudo systemctl reload nginx
```

#### 4. Wrong Domain Detection
```bash
# Check current branch
git branch --show-current

# Override domain detection
./deploy-azure.sh your-correct-domain.com
./setup-ssl.sh your-correct-domain.com

# Check environment info
./manage.sh env
```

### Emergency Commands

```bash
# Stop all services
./manage.sh stop

# Force redeploy with specific domain
DOMAIN_NAME="curate.hoonr.ai" ./deploy-azure.sh

# Quick service restart
sudo systemctl restart airecruiter-api airecruiter-web nginx

# Check if ports are open
sudo netstat -tlnp | grep -E ':(80|443|3000|8000)'
```

## 📝 Environment Configuration

### Required Environment Files

1. **API Environment** (`apps/api/.env`):
   ```bash
   # Database Configuration
   DATABASE_URL=postgresql://user:password@host:port/dbname
   
   # API Keys
   OPENAI_API_KEY=your_openai_key
   UNIPILE_API_KEY=your_unipile_key
   UNIPILE_ACCOUNT_ID=your_unipile_account_id
   
   # CORS Configuration (automatically updated by scripts)
   ALLOWED_ORIGINS=https://your-domain.com,http://your-domain.com
   ```

2. **Web Environment** (`apps/web/.env.local`):
   ```bash
   # API Configuration
   NEXT_PUBLIC_API_URL=https://your-domain.com
   ```

### Database Configuration

The application uses **Azure Database for PostgreSQL** (managed service):
- **QA**: Connected to QA database instance
- **PROD**: Connected to production database instance

Database connection strings are configured in the API `.env` file.

## 🎯 Best Practices

1. **Always test on QA first**: Deploy to `develop` branch and test before merging to `main`
2. **Use branch-based deployment**: Let the scripts auto-detect the environment
3. **Monitor deployments**: Use `./manage.sh logs` during deployment
4. **Verify health**: Run `./manage.sh status` after deployment
5. **Keep environment files secure**: Ensure `.env` files contain correct API keys
6. **Regular SSL renewal**: SSL certificates auto-renew, but monitor expiration

## 📞 Support

For deployment issues or questions:
1. Check the troubleshooting section above
2. Review service logs: `./manage.sh logs`
3. Verify environment configuration: `./manage.sh env`
4. Check system health: `./manage.sh health`

---

**Last Updated**: April 24, 2026