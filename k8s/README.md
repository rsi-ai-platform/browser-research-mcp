# Deploying browser-research to GKE (`nlx-browser`)

browser-research is the **headless-Chromium browser tier** of the NLX fetch ladder.
It runs on the **`rsi-workspaces` GKE Autopilot cluster**, namespace **`nlx`**, as the
**`nlx-browser`** Deployment, fronted by an **internal** LoadBalancer
(`10.10.0.34:8080`, in-cluster DNS `nlx-browser.nlx.svc.cluster.local:8080`).
`nlx-fetch` (authority-web-search) escalates to it for JS-render / interaction.

> Why pushes "weren't reaching the cluster": the only CD workflow targeted **Cloud
> Run** (`browser-research-mcp`), a service the NL-tier no longer routes to. The live
> pods are on **GKE**. `deploy-gke.yml` fixes that; `deploy-cloud-run.yml` is now
> manual-only.

| | |
|---|---|
| Cluster | `rsi-workspaces` (Autopilot) · `asia-south1` |
| Namespace / Deployment / container | `nlx` / `nlx-browser` / `nlx-browser` |
| Image repo | `asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp` |
| Service | internal LB `10.10.0.34:8080` (selector `app=nlx-browser`) |
| Pod SA | `nlx-fetch` (Workload Identity) |
| Readiness/liveness | `tcpSocket: 8080` |

---

## A. One-time local prerequisites

```bash
gcloud components install gke-gcloud-auth-plugin       # kubectl ↔ GKE auth
gcloud auth login                                      # your account
gcloud config set project silverfox-454313
# your account needs: roles/container.developer (deploy) + roles/artifactregistry.writer (push)
```

Get cluster credentials (writes `~/.kube/config`):

```bash
gcloud container clusters get-credentials rsi-workspaces \
  --region asia-south1 --project silverfox-454313
kubectl -n nlx get deploy nlx-browser          # sanity check access
```

---

## B. Manual deploy (the everyday path)

```bash
cd mcp-servers/browser-research
SHA=$(git rev-parse --short HEAD)
IMG="asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp:$SHA"

# 1. Build (amd64 — the cluster is amd64; Chromium layer is ~92 MB / image ~800 MB)
gcloud auth configure-docker asia-south1-docker.pkg.dev --quiet
docker build --platform linux/amd64 -t "$IMG" .

# 2. Push to Artifact Registry
docker push "$IMG"

# 3. Roll it onto the live Deployment (image-only; all other config preserved)
kubectl -n nlx set image deployment/nlx-browser nlx-browser="$IMG"
kubectl -n nlx annotate deployment/nlx-browser \
  kubernetes.io/change-cause="manual $SHA $(date -u +%FT%TZ)" --overwrite

# 4. Wait for the rollout (gates on the tcp:8080 readiness probe)
kubectl -n nlx rollout status deployment/nlx-browser --timeout=300s

# 5. Verify the running image + pods
kubectl -n nlx get deploy nlx-browser \
  -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
kubectl -n nlx get pods -l app=nlx-browser
```

### Build with Cloud Build instead (recommended on Apple-silicon / slow laptops)

The Chromium image is heavy and must be `linux/amd64`. Offload the build:

```bash
cd mcp-servers/browser-research
SHA=$(git rev-parse --short HEAD)
IMG="asia-south1-docker.pkg.dev/silverfox-454313/agentic-rag/browser-research-mcp:$SHA"
gcloud builds submit --tag "$IMG" --project silverfox-454313 .   # builds amd64 in-cloud + pushes
# then steps 3–5 above (skip the local docker build/push)
```

### Smoke test (must be in-cluster — the LB is internal)

```bash
kubectl -n nlx run smoke --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://nlx-browser.nlx.svc.cluster.local:8080/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list","params":{}}'
# expect 401/403 → server up and HybridAuth enforced (id-token required).
```

### Rollback

```bash
kubectl -n nlx rollout undo deployment/nlx-browser           # to the previous revision
kubectl -n nlx rollout history deployment/nlx-browser        # list revisions
kubectl -n nlx rollout undo deployment/nlx-browser --to-revision=N
```

---

## C. CI/CD (push → GKE) — one-time setup

`deploy-gke.yml` builds + pushes + rolls `nlx-browser` on every push to `main`,
authenticating via Workload Identity Federation (no stored key). The deployer SA
that `WIF_SA` points at needs **`roles/container.developer`** in addition to AR
writer. Grant it once:

```bash
PROJECT=silverfox-454313
DEPLOYER="<your-deployer-sa>@${PROJECT}.iam.gserviceaccount.com"   # the WIF_SA secret value
# create it if it doesn't exist yet:
gcloud iam service-accounts create browser-research-deployer --project "$PROJECT" || true
for ROLE in roles/artifactregistry.writer roles/container.developer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${DEPLOYER}" --role="$ROLE"
done
```

Then point the GitHub repo secrets at it (Settings → Secrets and variables → Actions):

- `WIF_PROVIDER` — `projects/<NUM>/locations/global/workloadIdentityPools/github-actions/providers/github`
- `WIF_SA` — the deployer SA email above

(Reuse the existing WIF pool from the other MCPs — same provider, just bind this SA.)

After that, every `git push` to `main` deploys to the cluster; watch it under the
repo's **Actions** tab. Re-create the whole object from scratch with
`kubectl apply -f k8s/` (then CI rolls the image).

---

## D. kubectl access without `gcloud` (e.g. headless/CI debugging)

```bash
TOK=$(gcloud auth print-access-token)
EP=$(curl -s -H "Authorization: Bearer $TOK" \
  "https://container.googleapis.com/v1/projects/silverfox-454313/locations/asia-south1/clusters/rsi-workspaces" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["endpoint"])')
# build a kubeconfig with the cluster CA + your token, then:
kubectl --server="https://$EP" --token="$TOK" -n nlx get deploy nlx-browser
```
