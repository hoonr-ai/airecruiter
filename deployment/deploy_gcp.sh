#!/bin/bash
set -e

# --- Configuration ---
# You can customize these variables or override them with environment variables
PROJECT_ID="${PROJECT_ID:-hoonr-486315}" 
REGION="${REGION:-us-central1}"
REPO_NAME="hoonr-repo"
API_IMAGE="hoonr-api"
WEB_IMAGE="hoonr-web"

# Path to gcloud
GCLOUD_BIN="/Users/vishak/Projects/Y/google-cloud-sdk/bin/gcloud"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}==============================================${NC}"
echo -e "${GREEN}   Hoonr.ai Deployment to Google Cloud Run    ${NC}"
echo -e "${GREEN}==============================================${NC}"

# 0. Prerequisites
if ! [ -x "$GCLOUD_BIN" ]; then
    echo -e "${RED}Error: 'gcloud' CLI not found at $GCLOUD_BIN.${NC}"
    exit 1
fi

# Check for open keys
# Check for open keys
if [ -z "$OPENAI_API_KEY" ]; then
    # Try to load from apps/api/.env
    if [ -f "apps/api/.env" ]; then
        echo "Loading configuration from apps/api/.env..."
        
        echo "Preparing isolated environment for configuration loading..."
        # Create a temporary virtual environment to avoid system package conflicts (PEP 668)
        python3 -m venv .deploy_venv
        
        # Install dotenv cleanly in the venv
        .deploy_venv/bin/pip install -q python-dotenv

        # Create key extraction script
        cat <<'EOF' > extract_env.py
import os
from dotenv import dotenv_values
config = dotenv_values('apps/api/.env')
for k, v in config.items():
    if v:
        # Escape single quotes for shell safe export
        safe_v = str(v).replace("'", "'\\\\''")
        print(f"export {k}='{safe_v}'")
EOF
        
        # Run using the venv python
        eval $(.deploy_venv/bin/python extract_env.py)
        
        # Cleanup
        rm extract_env.py
        rm -rf .deploy_venv
    fi

    if [ -z "$OPENAI_API_KEY" ]; then
        echo -e "${YELLOW}Warning: OPENAI_API_KEY environment variable is not set.${NC}"
        read -rs -p "Enter your OpenAI API Key: " OPENAI_API_KEY
        echo
        if [ -z "$OPENAI_API_KEY" ]; then
            echo -e "${RED}Error: API Key is required for the backend to function.${NC}"
            exit 1
        fi
    fi
else
    # Even if OPENAI_API_KEY is set, we might need other vars from .env
     if [ -f "apps/api/.env" ]; then
        echo "Loading additional configuration from apps/api/.env..."
        
        echo "Preparing isolated environment for configuration loading..."
        # Create a temporary virtual environment to avoid system package conflicts (PEP 668)
        python3 -m venv .deploy_venv
        
        # Install dotenv cleanly in the venv
        .deploy_venv/bin/pip install -q python-dotenv

        cat <<'EOF' > extract_env.py
import os
from dotenv import dotenv_values
config = dotenv_values('apps/api/.env')
for k, v in config.items():
    if v:
        safe_v = str(v).replace("'", "'\\\\''")
        print(f"export {k}='{safe_v}'")
EOF
        eval $(.deploy_venv/bin/python extract_env.py)
        rm extract_env.py
        rm -rf .deploy_venv
    fi
fi

# Confirm Project
CURRENT_PROJECT=$($GCLOUD_BIN config get-value project 2>/dev/null)
echo -e "\nTarget GCP Project ID: ${YELLOW}$PROJECT_ID${NC} (Current gcloud active: $CURRENT_PROJECT)"
read -p "Proceed with Project ID '$PROJECT_ID'? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    read -p "Enter target GCP Project ID: " PROJECT_ID
fi

# Set project context
echo "Setting active project to $PROJECT_ID..."
$GCLOUD_BIN config set project $PROJECT_ID

# 1. Enable APIs
echo -e "\n${GREEN}[1/6] Enabling necessary GCP APIs...${NC}"
$GCLOUD_BIN services enable artifactregistry.googleapis.com run.googleapis.com cloudbuild.googleapis.com

# 2. Configure Artifact Registry
echo -e "\n${GREEN}[2/6] Configuring Artifact Registry...${NC}"
if ! $GCLOUD_BIN artifacts repositories describe $REPO_NAME --location=$REGION --project=$PROJECT_ID &>/dev/null; then
    echo "Creating repository '$REPO_NAME' in $REGION..."
    $GCLOUD_BIN artifacts repositories create $REPO_NAME \
        --repository-format=docker \
        --location=$REGION \
        --description="Hoonr Docker Repository" \
        --project=$PROJECT_ID
else
    echo "Repository '$REPO_NAME' already exists."
fi

# 3. Build & Deploy Backend (API)
echo -e "\n${GREEN}[3/6] Building Backend (apps/api)...${NC}"
cd apps/api 2>/dev/null || cd ../apps/api
# We use 'gcloud builds submit' to build in the cloud
$GCLOUD_BIN builds submit --tag $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$API_IMAGE:latest .
cd ../.. || cd .. # Return to root

echo -e "\n${GREEN}[4/6] Deploying Backend to Cloud Run...${NC}"
$GCLOUD_BIN run deploy $API_IMAGE \
    --image $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$API_IMAGE:latest \
    --platform managed \
    --region $REGION \
    --allow-unauthenticated \
    --set-env-vars "OPENAI_API_KEY=$OPENAI_API_KEY,JOBDIVA_API_URL=$JOBDIVA_API_URL,JOBDIVA_CLIENT_ID=$JOBDIVA_CLIENT_ID,JOBDIVA_USERNAME=$JOBDIVA_USERNAME,JOBDIVA_PASSWORD=$JOBDIVA_PASSWORD,DATABASE_URL=$DATABASE_URL,ENCRYPTION_KEY=$ENCRYPTION_KEY,ENCRYPTION_SALT=$ENCRYPTION_SALT" \
    --project=$PROJECT_ID

# Capture API URL
API_URL=$($GCLOUD_BIN run services describe $API_IMAGE --platform managed --region $REGION --format 'value(status.url)' --project=$PROJECT_ID)
echo -e "Backend Deployed at: ${YELLOW}$API_URL${NC}"

# 4. Build & Deploy Frontend (Web)
# Important: Next.js builds (using NEXT_PUBLIC_) "bake in" environment variables at build time.
# We must pass the API_URL we just got as a build argument.

echo -e "\n${GREEN}[5/6] Building Frontend (apps/web)...${NC}"
cd apps/web

# Create a temporary Cloud Build config to pass build-args cleanly
cat <<EOF > cloudbuild_web_temp.yaml
steps:
  - name: 'gcr.io/cloud-builders/docker'
    args: [
      'build',
      '--build-arg', 'NEXT_PUBLIC_API_URL=$API_URL',
      '-t', '$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$WEB_IMAGE:latest',
      '.'
    ]
images:
  - '$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$WEB_IMAGE:latest'
EOF

$GCLOUD_BIN builds submit --config cloudbuild_web_temp.yaml .
rm cloudbuild_web_temp.yaml
cd ../..

echo -e "\n${GREEN}[6/6] Deploying Frontend to Cloud Run...${NC}"
$GCLOUD_BIN run deploy $WEB_IMAGE \
    --image $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$WEB_IMAGE:latest \
    --platform managed \
    --region $REGION \
    --allow-unauthenticated \
    --set-env-vars "NEXT_PUBLIC_API_URL=$API_URL" \
    --project=$PROJECT_ID

# Final Summary
WEB_URL=$($GCLOUD_BIN run services describe $WEB_IMAGE --platform managed --region $REGION --format 'value(status.url)' --project=$PROJECT_ID)


echo -e "\n${GREEN}==============================================${NC}"
echo -e "${GREEN}   Deployment Successful!   ${NC}"
echo -e "${GREEN}==============================================${NC}"
echo -e "Backend API:  ${YELLOW}$API_URL${NC}"
echo -e "Frontend App: ${YELLOW}$WEB_URL${NC}"
echo -e "Use the Frontend URL to access your application."
