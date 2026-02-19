# Deploying Hoonr to Google Cloud Platform

This guide explains how to deploy the Hoonr application (API + Web) to Google Cloud Run using the provided automation script.

## Prerequisites

1.  **Google Cloud SDK (`gcloud`)**: Must be installed and authenticated on your machine.
    *   Install: https://cloud.google.com/sdk/docs/install
    *   Login: `gcloud auth login`
2.  **Project ID**: You must have a GCP Project created (e.g., `hoonr`).
3.  **Permissions**: Your account needs permissions to enable APIs (Cloud Run, Artifact Registry, Cloud Build) and deploy services (Editor/Owner role is easiest for setup).

## Deployment Steps

1.  **Navigate to Project Root**:
    Ensure you are in the `Hoonr` root directory.

2.  **Run the Script**:
    ```bash
    ./deployment/deploy_gcp.sh
    ```

3.  **Follow Prompts**:
    *   The script will ask for your `OPENAI_API_KEY` (if not already set in your terminal environment).
    *   It will ask to confirm your Project ID.

## What the Script Does

1.  **Enables APIs**: Activates Artifact Registry, Cloud Run, and Cloud Build.
2.  **Creates Repository**: Sets up a Docker repository in Artifact Registry (`hoonr-repo`).
3.  **Builds & Deploys API**:
    *   Submits `apps/api` build to Cloud Build.
    *   Deploys to Cloud Run with `OPENAI_API_KEY`.
    *   Retrieves the generated URL (e.g., `https://hoonr-api-xyz.run.app`).
4.  **Builds & Deploys Web**:
    *   Submits `apps/web` build to Cloud Build.
    *   **Crucial**: Injects the API URL as `NEXT_PUBLIC_API_URL` during the build so the compiled Next.js app knows where to connect.
    *   Deploys to Cloud Run.

## Troubleshooting

*   **Permissions Error**: Ensure you ran `gcloud auth login` and `gcloud config set project <your-project-id>`.
*   **Build Failures**: Check the Cloud Build logs provided in the terminal link.
*   **Environment Variables**: If deployment succeeds but app fails, verify variables in Cloud Run console.
