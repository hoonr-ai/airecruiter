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

## Postgres tuning & diagnostics (Azure Database for PostgreSQL)

`postgres-tuning.sh` and `sql/*.sql` are for the production Azure Postgres
server (`job-diva-db`), separate from the GCP deploy flow above. Each is
safe to run against production as described below — none of them require a
maintenance window on their own, with the one exception called out under
`monitored-jobs-rebuild.sql`.

*   **`postgres-tuning.sh`** — sets `log_lock_waits`, the idle-in-transaction
    timeout, and enables `pg_stat_statements`. Run with no `APPLY` set first
    (dry run, prints what would change); add `APPLY=1` to actually change
    server parameters. Before setting the idle timeout for the first time,
    run `sql/check-idle-in-transaction.sql` a few times at busy hours — any
    session it lists would be ended by the new limit — then re-run with
    `IDLE_CHECK_DONE=1` (or `SKIP_IDLE_TIMEOUT=1` to leave that one setting
    alone). `pg_stat_statements` needs a server **restart** the first time it's
    enabled; the script never restarts on its own — it prints the `az`
    command for a quiet window when one is needed. See the script's own
    header comment for full usage and required env vars.
*   **`sql/check-idle-in-transaction.sql`** — read-only; safe to run anytime,
    including on production, as a prerequisite check for the idle timeout
    above.
*   **`sql/top-queries.sql`** — read-only; safe to run anytime against
    `pg_stat_statements` once it's enabled.
*   **`sql/monitored-jobs-rebuild.sql`** — **not** safe to run casually: it
    holds an ACCESS EXCLUSIVE lock on `monitored_jobs` for its whole run
    (seconds, at current table size). Run it in a quiet window, via `psql`
    directly rather than through the app, and only after the churn fixes
    that keep the table from immediately bloating back are already live —
    see the script's own header comment.
