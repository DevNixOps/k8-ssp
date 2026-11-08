# K8s Self-Service Portal

Developer portal to view Deployments, Pods, Services, Helm releases and Events, inspect pod logs (snapshot or live tail), and restart/scale deployments, with login and role/namespace-based access.

**Stack:** FastAPI + `kubernetes-asyncio` (backend) · React + TypeScript + Vite (frontend) · JWT auth · WebSocket live logs · Helm CLI for releases · Docker + Helm chart for deployment.

## Run locally (no cluster needed — mock mode)

```bash
# backend (mock cluster by default)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload --port 8000

# frontend (another terminal; proxies /api to :8000)
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Demo logins (default `SSP_USERS`): `viewer/viewer` (read-only), `dev/dev` (restart/scale in `dev`,`default`), `admin/admin` (everything, all namespaces).

Tests: `cd backend && PYTHONPATH=. .venv/bin/pytest`

## Roles

| Role | Read | Restart/Scale | Namespaces |
|---|---|---|---|
| viewer | yes | no | all non-system, or the user's list |
| developer | yes | yes | user's list (or all) |
| admin | yes | yes | all incl. system |

Users: `SSP_USERS="user:password:role[:ns1|ns2],..."`. Restart/scale are written to the `ssp.audit` log.

## Run against a real cluster (e.g. your Lightsail cluster)

Option A — from your laptop with a kubeconfig (needs `helm` on PATH for the Helm tab):

```bash
SSP_MOCK_MODE=false SSP_KUBECONFIG=~/lightsail-kubeconfig SSP_JWT_SECRET=<32+ chars> \
  PYTHONPATH=. .venv/bin/uvicorn app.main:app --port 8000
```

Option B — deploy in the cluster:

```bash
docker build -t <registry>/k8-ssp:0.1.0 .   # push it, or import into the cluster's runtime (k3s: `k3s ctr images import`)
helm install ssp deploy/helm/k8-ssp \
  --set image.repository=<registry>/k8-ssp --set image.tag=0.1.0 \
  --set users="admin:<strong-pw>:admin,dev:<pw>:developer:default"
kubectl port-forward svc/ssp 8080:80     # http://localhost:8080
```

Chart RBAC is a ClusterRole: read namespaces/pods/logs/services/configmaps/events/deployments; `patch` on deployments (`rbac.allowActions`); read Secrets for Helm release listing (`rbac.helmReleases`) and the *List secrets* action (`rbac.listSecrets`) — set both to `false` to remove Secret access. The UI is action-driven (buttons for list deployments/pods/services/configmaps/secrets/Helm/events and pod logs); secret *values* are never returned, only names and key names, and only to developer/admin users. Per-user namespace limits are enforced by the portal, not by Kubernetes.

## Notes / hardening for real use
- Put it behind HTTPS (ingress/reverse proxy); change all default passwords and `SSP_JWT_SECRET`.
- Passwords come from env for demo simplicity; swap in OIDC (Keycloak/GitHub) for production.
- Log tail is capped (`SSP_MAX_LOG_TAIL`) and live streams are limited per user (`SSP_MAX_STREAMS_PER_USER`).
- The live-log WebSocket passes the JWT as a query parameter; avoid logging URLs at the proxy.
