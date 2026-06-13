# Continuous Deployment to Cloud Run

On every push to `main`, [`deploy-cloud-run.yml`](workflows/deploy-cloud-run.yml)
rebuilds the Docker image, pushes it to Artifact Registry, and rolls it onto
the existing Cloud Run service `browser-research-mcp`. The service URL
(`https://browser-research-mcp-pef65a33ta-el.a.run.app`) never changes, so
the DeepInsights backend's `BROWSER_RESEARCH_MCP_URL` setting stays put.

Auth uses **Workload Identity Federation** — no long-lived service-account
key in GitHub secrets.

## One-time GCP setup

The same WIF pool already wired for `rbi-dbie-mcp` works for this repo —
just add this repo to the provider's `attribute.repository` allowlist and
create a deployer SA for this service.

```bash
PROJECT=silverfox-454313
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SERVICE=browser-research-mcp
SA="${SERVICE}-deployer"
GITHUB_REPO="rsi-ai-platform/${SERVICE}"

# 1. Create the deployer SA
gcloud iam service-accounts create "$SA" \
  --project="$PROJECT" \
  --display-name="GitHub Actions → Cloud Run deployer for ${SERVICE}"

SA_EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"

# 2. Grant the SA what it needs (build + push + deploy + impersonate runtime SA)
for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA_EMAIL" --role="$ROLE"
done

# 3. Bind the GitHub repo to the SA via the existing WIF pool
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project="$PROJECT" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-actions/attribute.repository/${GITHUB_REPO}"
```

## GitHub secrets to set

```
WIF_PROVIDER   projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-actions/providers/github
WIF_SA         browser-research-mcp-deployer@silverfox-454313.iam.gserviceaccount.com
```

Set via the web UI (Settings → Secrets and variables → Actions) or:

```bash
gh secret set WIF_PROVIDER --repo rsi-ai-platform/browser-research-mcp --body "projects/<NUM>/locations/global/workloadIdentityPools/github-actions/providers/github"
gh secret set WIF_SA       --repo rsi-ai-platform/browser-research-mcp --body "browser-research-mcp-deployer@silverfox-454313.iam.gserviceaccount.com"
```

## Manual deploy fallback

If CI is misbehaving, deploy directly from a developer workstation:

```bash
SHA=$(git rev-parse --short HEAD)
docker buildx build --platform linux/amd64 \
  -t asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp:latest \
  -t asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp:$SHA \
  --push .
gcloud run deploy browser-research-mcp \
  --image=asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp:$SHA \
  --region=asia-south1 --project=silverfox-454313 --quiet
```
