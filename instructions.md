# Build a Kubernetes Self-Service Portal with AI — Complete Guide

By the end you will have a working web portal where developers log in and see **Deployments, Pods, Services, Helm releases and Events**, read **pod logs**, watch **live logs**, and **restart / scale** deployments — with **role-based access**.

| Layer | Technology | Why |
|---|---|---|
| Backend API | Python 3.9+ / FastAPI | Quick to write, auto-generated API docs at `/docs`, built-in WebSocket |
| Kubernetes access | `kubernetes-asyncio` + Helm CLI | Official client; Helm has no good Python SDK |
| Auth | JWT + roles (viewer / developer / admin) | Simple and demonstrable |
| Frontend | React + TypeScript + Vite | Great AI code-generation support |
| Packaging | Docker + Helm chart | Deploy the portal onto any cluster |

> **Mock mode:** the backend ships with a fake in-memory cluster, so you can build, run and demo everything **without any Kubernetes cluster**. Connecting a real cluster is the last step.

## 0. Prerequisites

- Python 3.9+ (tested on 3.9 and 3.13) and Node.js 20+ (`python3 --version`, `node --version`). On Python 3.9 avoid `X | None` type hints — the code uses `typing.Optional` so it runs on 3.9.
- Optional (for real clusters / deployment): `kubectl`, `helm`, `docker`

## 1. Generate all project files from this document

This document is the single source of truth. Every file appears below under a heading like ``#### `path/to/file` `` followed by a code block. Save this document as `instructions.md` in an empty folder, then run this **extractor** (it creates every file):

```bash
python3 - <<'EOF'
import re, pathlib
text = pathlib.Path("instructions.md").read_text()
pat = re.compile(r"^#### `([^`]+)`\n\n```[^\n]*\n(.*?)\n```$", re.S | re.M)
files = pat.findall(text)
for path, body in files:
    p = pathlib.Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body + "\n")
print(f"Created {len(files)} files")
EOF
```

You should see `Created 25 files`. The sections below explain what each part does — read them to learn how it is built (or to re-create files by hand / with your AI assistant).

## 2. Backend — configuration and authentication

**Concepts:** settings come from `SSP_*` environment variables (pydantic-settings). Login issues a signed JWT; every API call checks the token, the user's **role** and the **namespaces** they may access.

#### `backend/requirements.txt`

```text
fastapi>=0.115
uvicorn[standard]>=0.30
kubernetes-asyncio>=30.0
PyJWT>=2.8
pydantic-settings>=2.3
pytest>=8
httpx>=0.27
python-multipart>=0.0.9
```

#### `backend/app/__init__.py`

```python

```

#### `backend/app/config.py`

```python
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSP_")

    mock_mode: bool = True  # set SSP_MOCK_MODE=false to talk to a real cluster
    kubeconfig: Optional[str] = None  # None -> in-cluster, then default kubeconfig
    jwt_secret: str = "change-me-in-production-use-32+-bytes"
    token_ttl_minutes: int = 60
    # "user:password:role[:ns1|ns2]" comma-separated. Roles: viewer, developer, admin
    users: str = "viewer:viewer:viewer,dev:dev:developer:dev|default,admin:admin:admin"
    max_log_tail: int = 5000
    max_streams_per_user: int = 3
    hidden_namespaces: str = "kube-system,kube-public,kube-node-lease"
    cors_origins: str = "http://localhost:5173"


settings = Settings()
```

#### `backend/app/auth.py`

```python
import hmac
import time
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from .config import settings

ROLE_RANK = {"viewer": 1, "developer": 2, "admin": 3}
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


@dataclass
class User:
    name: str
    role: str
    namespaces: list[str]  # empty means all (admin) / unrestricted

    def can_access(self, ns: str) -> bool:
        return self.role == "admin" or not self.namespaces or ns in self.namespaces


def _load_users() -> dict[str, tuple[str, str, list[str]]]:
    users = {}
    for entry in filter(None, (e.strip() for e in settings.users.split(","))):
        parts = entry.split(":")
        name, pw, role = parts[0], parts[1], parts[2]
        nss = parts[3].split("|") if len(parts) > 3 else []
        users[name] = (pw, role, nss)
    return users


def authenticate(username: str, password: str) -> Optional[User]:
    rec = _load_users().get(username)
    if rec and hmac.compare_digest(rec[0].encode(), password.encode()):
        return User(username, rec[1], rec[2])
    return None


def create_token(user: User) -> str:
    claims = {
        "sub": user.name,
        "role": user.role,
        "ns": user.namespaces,
        "exp": int(time.time()) + settings.token_ttl_minutes * 60,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> User:
    try:
        c = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return User(c["sub"], c["role"], c.get("ns", []))


def current_user(token: str = Depends(oauth2)) -> User:
    return decode_token(token)


def require_role(min_role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{min_role}'")
        return user

    return dep


def check_namespace(user: User, ns: str) -> None:
    if not user.can_access(ns):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"No access to namespace '{ns}'")
```

## 3. Backend — cluster providers (mock + real)

**Concepts:** a `Provider` interface hides where data comes from. `MockProvider` is an in-memory fake cluster (demos/tests); `RealProvider` uses the Kubernetes API and the `helm` CLI. Switch with `SSP_MOCK_MODE`. Live logs are an async generator that yields lines as they arrive.

#### `backend/app/providers/__init__.py`

```python
from typing import Optional

from ..config import settings
from .base import Provider

_provider: Optional[Provider] = None


def get_provider() -> Provider:
    global _provider
    if _provider is None:
        if settings.mock_mode:
            from .mock import MockProvider

            _provider = MockProvider()
        else:
            from .real import RealProvider

            _provider = RealProvider()
    return _provider
```

#### `backend/app/providers/base.py`

```python
from typing import AsyncIterator, Optional, Protocol


class NotFound(Exception):
    pass


class Provider(Protocol):
    async def namespaces(self) -> list[str]: ...
    async def deployments(self, ns: str) -> list[dict]: ...
    async def pods(self, ns: str) -> list[dict]: ...
    async def services(self, ns: str) -> list[dict]: ...
    async def events(self, ns: str) -> list[dict]: ...
    async def configmaps(self, ns: str) -> list[dict]: ...
    async def secrets(self, ns: str) -> list[dict]: ...  # metadata only, never values
    async def helm_releases(self, ns: Optional[str]) -> list[dict]: ...
    async def helm_release(self, ns: str, name: str) -> dict: ...
    async def logs(self, ns: str, pod: str, container: Optional[str], tail: int, previous: bool) -> str: ...
    def stream_logs(self, ns: str, pod: str, container: Optional[str], tail: int) -> AsyncIterator[str]: ...
    async def restart(self, ns: str, deployment: str) -> None: ...
    async def scale(self, ns: str, deployment: str, replicas: int) -> None: ...
```

#### `backend/app/providers/mock.py`

```python
import asyncio
import copy
from datetime import datetime, timezone

from .base import NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MockProvider:
    """In-memory cluster for demos, development and tests."""

    def __init__(self) -> None:
        self._deps = {
            "default": [
                {"name": "web", "replicas": 2, "ready": 2, "image": "nginx:1.27"},
                {"name": "api", "replicas": 3, "ready": 2, "image": "demo/api:1.4.0"},
            ],
            "dev": [{"name": "worker", "replicas": 1, "ready": 1, "image": "demo/worker:0.9"}],
            "kube-system": [{"name": "coredns", "replicas": 2, "ready": 2, "image": "coredns:1.11"}],
        }
        self._pods = {
            "default": [
                self._pod("web-6d9f7-abcde", "Running", "web", "nginx:1.27"),
                self._pod("web-6d9f7-fghij", "Running", "web", "nginx:1.27"),
                self._pod("api-5c8b4-klmno", "Running", "api", "demo/api:1.4.0"),
                self._pod("api-5c8b4-pqrst", "CrashLoopBackOff", "api", "demo/api:1.4.0", restarts=7, ready=False),
            ],
            "dev": [self._pod("worker-7f6d5-uvwxy", "Running", "worker", "demo/worker:0.9")],
            "kube-system": [self._pod("coredns-1-zzzzz", "Running", "coredns", "coredns:1.11")],
        }
        self._svcs = {
            "default": [
                {"name": "web", "type": "LoadBalancer", "cluster_ip": "10.43.1.10", "ports": ["80/TCP"]},
                {"name": "api", "type": "ClusterIP", "cluster_ip": "10.43.1.11", "ports": ["8080/TCP"]},
            ],
            "dev": [{"name": "worker", "type": "ClusterIP", "cluster_ip": "10.43.2.5", "ports": ["9000/TCP"]}],
            "kube-system": [],
        }
        self._helm = [
            {"name": "web", "namespace": "default", "chart": "nginx-18.1.0", "app_version": "1.27", "revision": 3, "status": "deployed", "updated": _now()},
            {"name": "worker", "namespace": "dev", "chart": "worker-0.2.0", "app_version": "0.9", "revision": 1, "status": "deployed", "updated": _now()},
        ]

    @staticmethod
    def _pod(name, phase, app, image, restarts=0, ready=True):
        return {
            "name": name,
            "phase": phase,
            "ready": ready,
            "restarts": restarts,
            "node": "node-1",
            "ip": "10.42.0.12",
            "containers": [app],
            "image": image,
            "created": _now(),
        }

    def _ns(self, ns: str) -> None:
        if ns not in self._deps:
            raise NotFound(f"namespace {ns}")

    async def namespaces(self):
        return sorted(self._deps)

    async def deployments(self, ns):
        self._ns(ns)
        return copy.deepcopy(self._deps[ns])

    async def pods(self, ns):
        self._ns(ns)
        return copy.deepcopy(self._pods[ns])

    async def services(self, ns):
        self._ns(ns)
        return copy.deepcopy(self._svcs[ns])

    async def events(self, ns):
        self._ns(ns)
        if ns != "default":
            return []
        return [
            {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container", "object": "Pod/api-5c8b4-pqrst", "time": _now()},
            {"type": "Normal", "reason": "Pulled", "message": "Image already present", "object": "Pod/web-6d9f7-abcde", "time": _now()},
        ]

    async def configmaps(self, ns):
        self._ns(ns)
        if ns == "kube-system":
            return [{"name": "coredns", "keys": ["Corefile"], "created": _now()}]
        return [
            {"name": "app-config", "keys": ["LOG_LEVEL", "FEATURE_FLAGS", "API_URL"], "created": _now()},
            {"name": "nginx-conf", "keys": ["nginx.conf"], "created": _now()},
        ]

    async def secrets(self, ns):
        self._ns(ns)
        if ns == "kube-system":
            return []
        return [
            {"name": "db-credentials", "type": "Opaque", "keys": ["username", "password"], "created": _now()},
            {"name": "registry-pull", "type": "kubernetes.io/dockerconfigjson", "keys": [".dockerconfigjson"], "created": _now()},
        ]

    async def helm_releases(self, ns):
        return [r for r in self._helm if ns is None or r["namespace"] == ns]

    async def helm_release(self, ns, name):
        for r in self._helm:
            if r["namespace"] == ns and r["name"] == name:
                history = [{"revision": i, "status": "superseded" if i < r["revision"] else "deployed"} for i in range(1, r["revision"] + 1)]
                return {**r, "values": {"replicaCount": 2, "image": {"tag": r["app_version"]}}, "history": history}
        raise NotFound(f"release {name}")

    def _find_pod(self, ns, pod):
        self._ns(ns)
        if not any(p["name"] == pod for p in self._pods[ns]):
            raise NotFound(f"pod {pod}")

    async def logs(self, ns, pod, container, tail, previous):
        self._find_pod(ns, pod)
        return "\n".join(f"{_now()} INFO request handled id={i}" for i in range(min(tail, 50)))

    async def stream_logs(self, ns, pod, container, tail):
        self._find_pod(ns, pod)
        i = 0
        while True:
            i += 1
            yield f"{_now()} INFO live event #{i} pod={pod}"
            await asyncio.sleep(1)

    def _dep(self, ns, name):
        self._ns(ns)
        for d in self._deps[ns]:
            if d["name"] == name:
                return d
        raise NotFound(f"deployment {name}")

    async def restart(self, ns, deployment):
        self._dep(ns, deployment)

    async def scale(self, ns, deployment, replicas):
        d = self._dep(ns, deployment)
        d["replicas"] = d["ready"] = replicas
```

#### `backend/app/providers/real.py`

```python
import asyncio
import functools
import json
from datetime import datetime, timezone
from typing import Optional

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.exceptions import ApiException

from ..config import settings
from .base import NotFound


def _iso(ts) -> Optional[str]:
    return ts.isoformat() if ts else None


def _wrap(fn):
    @functools.wraps(fn)
    async def inner(*a, **kw):
        try:
            return await fn(*a, **kw)
        except ApiException as e:
            if e.status == 404:
                raise NotFound(e.reason)
            raise

    return inner


class RealProvider:
    """Talks to a live cluster via kubeconfig or in-cluster ServiceAccount."""

    def __init__(self) -> None:
        self._ready = False
        self._lock = asyncio.Lock()

    async def _api(self):
        async with self._lock:
            if not self._ready:
                if settings.kubeconfig:
                    await config.load_kube_config(config_file=settings.kubeconfig)
                else:
                    try:
                        config.load_incluster_config()
                    except config.ConfigException:
                        await config.load_kube_config()
                self._ready = True
        c = client.ApiClient()
        return c, client.CoreV1Api(c), client.AppsV1Api(c)

    @_wrap
    async def namespaces(self):
        c, core, _ = await self._api()
        try:
            return sorted(n.metadata.name for n in (await core.list_namespace()).items)
        finally:
            await c.close()

    @_wrap
    async def deployments(self, ns):
        c, _, apps = await self._api()
        try:
            return [
                {
                    "name": d.metadata.name,
                    "replicas": d.spec.replicas or 0,
                    "ready": d.status.ready_replicas or 0,
                    "image": ", ".join(x.image for x in d.spec.template.spec.containers),
                }
                for d in (await apps.list_namespaced_deployment(ns)).items
            ]
        finally:
            await c.close()

    @_wrap
    async def pods(self, ns):
        c, core, _ = await self._api()
        try:
            res = []
            for p in (await core.list_namespaced_pod(ns)).items:
                cs = p.status.container_statuses or []
                res.append(
                    {
                        "name": p.metadata.name,
                        "phase": next(
                            (s.state.waiting.reason for s in cs if s.state and s.state.waiting and s.state.waiting.reason),
                            p.status.phase,
                        ),
                        "ready": bool(cs) and all(s.ready for s in cs),
                        "restarts": sum(s.restart_count for s in cs),
                        "node": p.spec.node_name,
                        "ip": p.status.pod_ip,
                        "containers": [x.name for x in p.spec.containers],
                        "image": ", ".join(x.image for x in p.spec.containers),
                        "created": _iso(p.metadata.creation_timestamp),
                    }
                )
            return res
        finally:
            await c.close()

    @_wrap
    async def services(self, ns):
        c, core, _ = await self._api()
        try:
            return [
                {
                    "name": s.metadata.name,
                    "type": s.spec.type,
                    "cluster_ip": s.spec.cluster_ip,
                    "ports": [f"{p.port}/{p.protocol}" for p in s.spec.ports or []],
                }
                for s in (await core.list_namespaced_service(ns)).items
            ]
        finally:
            await c.close()

    @_wrap
    async def events(self, ns):
        c, core, _ = await self._api()
        try:
            items = (await core.list_namespaced_event(ns)).items

            def ts(e):
                return e.last_timestamp or e.event_time or e.metadata.creation_timestamp

            items.sort(key=ts, reverse=True)
            return [
                {
                    "type": e.type,
                    "reason": e.reason,
                    "message": e.message,
                    "object": f"{e.involved_object.kind}/{e.involved_object.name}",
                    "time": _iso(ts(e)),
                }
                for e in items[:100]
            ]
        finally:
            await c.close()

    @_wrap
    async def configmaps(self, ns):
        c, core, _ = await self._api()
        try:
            return [
                {
                    "name": m.metadata.name,
                    "keys": sorted([*(m.data or {}), *(m.binary_data or {})]),
                    "created": _iso(m.metadata.creation_timestamp),
                }
                for m in (await core.list_namespaced_config_map(ns)).items
            ]
        finally:
            await c.close()

    @_wrap
    async def secrets(self, ns):
        c, core, _ = await self._api()
        try:
            # Only names/types/key names are returned; secret values never leave the backend.
            return [
                {
                    "name": s.metadata.name,
                    "type": s.type,
                    "keys": sorted(s.data or {}),
                    "created": _iso(s.metadata.creation_timestamp),
                }
                for s in (await core.list_namespaced_secret(ns)).items
                if s.type != "helm.sh/release.v1"
            ]
        finally:
            await c.close()

    async def _helm(self, *args: str):
        kube = ["--kubeconfig", settings.kubeconfig] if settings.kubeconfig else []
        proc = await asyncio.create_subprocess_exec(
            "helm", *args, "-o", "json", *kube,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode().strip()
            if "not found" in msg:
                raise NotFound(msg)
            raise RuntimeError(msg)
        return json.loads(out or "null")

    async def helm_releases(self, ns):
        rows = await self._helm("list", *(["-n", ns] if ns else ["-A"])) or []
        return [
            {"name": r["name"], "namespace": r["namespace"], "chart": r["chart"], "app_version": r["app_version"],
             "revision": int(r["revision"]), "status": r["status"], "updated": r["updated"]}
            for r in rows
        ]

    async def helm_release(self, ns, name):
        rels = [r for r in await self.helm_releases(ns) if r["name"] == name]
        if not rels:
            raise NotFound(f"release {name}")
        values = await self._helm("get", "values", name, "-n", ns)
        history = await self._helm("history", name, "-n", ns)
        hist = [{"revision": h["revision"], "status": h["status"], "chart": h["chart"], "updated": h["updated"]} for h in history]
        return {**rels[0], "values": values, "history": hist}

    @_wrap
    async def logs(self, ns, pod, container, tail, previous):
        c, core, _ = await self._api()
        try:
            return await core.read_namespaced_pod_log(
                pod, ns, container=container, tail_lines=tail, previous=previous, timestamps=True
            )
        finally:
            await c.close()

    async def stream_logs(self, ns, pod, container, tail):
        c, core, _ = await self._api()
        resp = None
        try:
            try:
                resp = await core.read_namespaced_pod_log(
                    pod, ns, container=container, tail_lines=tail, follow=True, timestamps=True,
                    _preload_content=False,
                )
            except ApiException as e:
                if e.status == 404:
                    raise NotFound(e.reason)
                raise
            async for line in resp.content:
                yield line.decode(errors="replace").rstrip("\n")
        finally:
            if resp is not None:
                resp.release()
            await c.close()

    @_wrap
    async def restart(self, ns, deployment):
        c, _, apps = await self._api()
        try:
            patch = {"spec": {"template": {"metadata": {"annotations": {
                "kubectl.kubernetes.io/restartedAt": datetime.now(timezone.utc).isoformat()}}}}}
            await apps.patch_namespaced_deployment(deployment, ns, patch)
        finally:
            await c.close()

    @_wrap
    async def scale(self, ns, deployment, replicas):
        c, _, apps = await self._api()
        try:
            await apps.patch_namespaced_deployment_scale(deployment, ns, {"spec": {"replicas": replicas}})
        finally:
            await c.close()
```

## 4. Backend — the API

**Concepts:** REST endpoints under `/api`, a dependency (`_ns_user`) that enforces namespace access, `require_role("developer")` guarding restart/scale, an audit log, and a **WebSocket** endpoint for live logs (token passed as a query parameter because browsers cannot set headers on WebSockets). Log size and concurrent streams per user are capped.

#### `backend/app/main.py`

```python
import logging
import os
from pathlib import Path
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field

from .auth import (User, authenticate, check_namespace, create_token, current_user,
                   decode_token, require_role)
from .config import settings
from .providers import get_provider
from .providers.base import NotFound

audit = logging.getLogger("ssp.audit")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="K8s Self-Service Portal")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in settings.cors_origins.split(",") if o],
    allow_methods=["*"],
    allow_headers=["*"],
)

_streams: dict[str, int] = defaultdict(int)


@app.exception_handler(NotFound)
async def _not_found(_, exc: NotFound):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": f"Not found: {exc}"}, status_code=404)


@app.get("/api/health")
async def health():
    return {"status": "ok", "mock": settings.mock_mode}


@app.post("/api/auth/login")
async def login(form: OAuth2PasswordRequestForm = Depends()):
    user = authenticate(form.username, form.password)
    if not user:
        raise HTTPException(401, "Invalid credentials")
    audit.info("login user=%s", user.name)
    return {"access_token": create_token(user), "token_type": "bearer"}


@app.get("/api/me")
async def me(user: User = Depends(current_user)):
    return {"name": user.name, "role": user.role, "namespaces": user.namespaces}


@app.get("/api/namespaces")
async def namespaces(user: User = Depends(current_user)):
    hidden = set(settings.hidden_namespaces.split(","))
    names = await get_provider().namespaces()
    return [n for n in names if user.can_access(n) and (user.role == "admin" or n not in hidden)]


def _ns_user(namespace: str, user: User = Depends(current_user)) -> str:
    check_namespace(user, namespace)
    return namespace


@app.get("/api/namespaces/{namespace}/deployments")
async def deployments(ns: str = Depends(_ns_user)):
    return await get_provider().deployments(ns)


@app.get("/api/namespaces/{namespace}/pods")
async def pods(ns: str = Depends(_ns_user)):
    return await get_provider().pods(ns)


@app.get("/api/namespaces/{namespace}/services")
async def services(ns: str = Depends(_ns_user)):
    return await get_provider().services(ns)


@app.get("/api/namespaces/{namespace}/events")
async def events(ns: str = Depends(_ns_user)):
    return await get_provider().events(ns)


@app.get("/api/namespaces/{namespace}/configmaps")
async def configmaps(ns: str = Depends(_ns_user)):
    return await get_provider().configmaps(ns)


@app.get("/api/namespaces/{namespace}/secrets")
async def secrets(ns: str = Depends(_ns_user), user: User = Depends(require_role("developer"))):
    audit.info("list-secrets user=%s ns=%s", user.name, ns)
    return await get_provider().secrets(ns)


@app.get("/api/helm")
async def helm_all(user: User = Depends(current_user)):
    rels = await get_provider().helm_releases(None)
    return [r for r in rels if user.can_access(r["namespace"])]


@app.get("/api/namespaces/{namespace}/helm/{release}")
async def helm_one(release: str, ns: str = Depends(_ns_user)):
    return await get_provider().helm_release(ns, release)


@app.get("/api/namespaces/{namespace}/pods/{pod}/logs")
async def pod_logs(
    pod: str,
    container: Optional[str] = None,
    tail: int = Query(200, ge=1),
    previous: bool = False,
    ns: str = Depends(_ns_user),
):
    text = await get_provider().logs(ns, pod, container, min(tail, settings.max_log_tail), previous)
    return {"pod": pod, "container": container, "logs": text}


class ScaleBody(BaseModel):
    replicas: int = Field(ge=0, le=20)


@app.post("/api/namespaces/{namespace}/deployments/{name}/restart")
async def restart(name: str, ns: str = Depends(_ns_user), user: User = Depends(require_role("developer"))):
    await get_provider().restart(ns, name)
    audit.info("restart user=%s ns=%s deployment=%s", user.name, ns, name)
    return {"status": "restarting"}


@app.post("/api/namespaces/{namespace}/deployments/{name}/scale")
async def scale(name: str, body: ScaleBody, ns: str = Depends(_ns_user), user: User = Depends(require_role("developer"))):
    await get_provider().scale(ns, name, body.replicas)
    audit.info("scale user=%s ns=%s deployment=%s replicas=%d", user.name, ns, name, body.replicas)
    return {"status": "scaled", "replicas": body.replicas}


@app.websocket("/api/ws/namespaces/{namespace}/pods/{pod}/logs")
async def live_logs(ws: WebSocket, namespace: str, pod: str, token: str, container: Optional[str] = None, tail: int = 50):
    try:
        user = decode_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    if not user.can_access(namespace):
        await ws.close(code=4403)
        return
    if _streams[user.name] >= settings.max_streams_per_user:
        await ws.close(code=4429)
        return
    await ws.accept()
    _streams[user.name] += 1
    stream = get_provider().stream_logs(namespace, pod, container, min(tail, settings.max_log_tail))
    try:
        async for line in stream:
            await ws.send_text(line)
    except NotFound:
        await ws.send_text("[portal] pod or container not found")
    except WebSocketDisconnect:
        pass
    except Exception as e:  # surface cluster errors to the viewer
        try:
            await ws.send_text(f"[portal] stream error: {e}")
        except Exception:
            pass
    finally:
        _streams[user.name] -= 1
        await stream.aclose()
        try:
            await ws.close()
        except Exception:
            pass


# Serve the built frontend (must be mounted last so /api routes take precedence)
_static = Path(os.environ.get("SSP_STATIC_DIR", "static"))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="ui")
```

## 5. Backend — tests

Tests use FastAPI's `TestClient` against mock mode: auth, role checks, namespace isolation, validation, logs, Helm and the WebSocket.

#### `backend/tests/test_api.py`

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def token(user):
    r = client.post("/api/auth/login", data={"username": user, "password": user})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_requires_auth():
    assert client.get("/api/namespaces").status_code == 401


def test_bad_login():
    assert client.post("/api/auth/login", data={"username": "dev", "password": "x"}).status_code == 401


def test_viewer_reads_but_cannot_act():
    h = token("viewer")
    assert "default" in client.get("/api/namespaces", headers=h).json()
    assert "kube-system" not in client.get("/api/namespaces", headers=h).json()
    assert len(client.get("/api/namespaces/default/pods", headers=h).json()) == 4
    assert client.post("/api/namespaces/default/deployments/web/restart", headers=h).status_code == 403


def test_developer_scoped_to_namespaces():
    h = token("dev")
    assert client.get("/api/namespaces", headers=h).json() == ["default", "dev"]
    assert client.get("/api/namespaces/kube-system/pods", headers=h).status_code == 403
    assert client.post("/api/namespaces/default/deployments/web/scale", json={"replicas": 5}, headers=h).status_code == 200
    assert client.post("/api/namespaces/default/deployments/web/scale", json={"replicas": 999}, headers=h).status_code == 422
    deps = {d["name"]: d for d in client.get("/api/namespaces/default/deployments", headers=h).json()}
    assert deps["web"]["replicas"] == 5


def test_not_found_and_logs_and_helm():
    h = token("admin")
    assert client.get("/api/namespaces/nope/pods", headers=h).status_code == 404
    assert client.get("/api/namespaces/default/pods/web-6d9f7-abcde/logs?tail=5", headers=h).json()["logs"]
    assert client.get("/api/helm", headers=h).json()
    assert client.get("/api/namespaces/default/helm/web", headers=h).json()["history"]


def test_configmaps_and_secrets():
    v = token("viewer")
    assert client.get("/api/namespaces/default/configmaps", headers=v).json()[0]["keys"]
    assert client.get("/api/namespaces/default/secrets", headers=v).status_code == 403
    d = token("dev")
    secrets = client.get("/api/namespaces/default/secrets", headers=d).json()
    assert secrets and all("value" not in s and "data" not in s for s in secrets)
    assert client.get("/api/namespaces/kube-system/secrets", headers=d).status_code == 403


def test_websocket_live_logs():
    t = token("dev")["Authorization"].split()[1]
    with client.websocket_connect(f"/api/ws/namespaces/default/pods/web-6d9f7-abcde/logs?token={t}") as ws:
        assert "live event #1" in ws.receive_text()
    with pytest.raises(Exception):
        with client.websocket_connect("/api/ws/namespaces/default/pods/x/logs?token=bad"):
            pass
```

## 6. Run and test the backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/pytest -q            # expect: 7 passed
PYTHONPATH=. .venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000/docs> for interactive API docs. Quick check from another terminal:

```bash
curl http://localhost:8000/api/health
TOKEN=$(curl -s -d 'username=dev&password=dev' http://localhost:8000/api/auth/login | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/namespaces/default/pods
```

Demo users: `viewer/viewer` (read-only), `dev/dev` (restart/scale in `dev` and `default`), `admin/admin` (everything). Leave the backend running.

## 7. Frontend (React + TypeScript + Vite)

**Concepts:** a modern dark **single-page, action-driven UI**. Choose a namespace, then click an action button — **List deployments / pods / services / configmaps / secrets / Helm releases / events** or **Get pod logs** — and only then is that data fetched and shown in a result panel on the same page (with filter, refresh and optional auto-refresh). Deployments have inline scale (− / +) and restart for developers/admins; **logs (live tail or snapshot) and Helm values/history open in a slide-over drawer**. The *List secrets* button is locked for viewers, and the API never returns secret values (only names, types and key names). `api.ts` wraps `fetch` with the JWT; `main.tsx` is the page; `LogViewer.tsx` has the drawer and log viewer; `styles.css` is plain CSS (no UI library). Vite proxies `/api` (including WebSockets) to the backend on port 8000.

#### `frontend/package.json`

```json
{
  "name": "k8-ssp-frontend",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "typescript": "^5.5.4",
    "vite": "^5.4.0"
  }
}
```

#### `frontend/tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src", "vite.config.ts"]
}
```

#### `frontend/vite.config.ts`

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://localhost:8000", ws: true } } },
});
```

#### `frontend/index.html`

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>K8s Self-Service Portal</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

#### `frontend/src/api.ts`

```ts
export type Me = { name: string; role: string; namespaces: string[] };

let token: string | null = sessionStorage.getItem("token");

export const getToken = () => token;
export const setToken = (t: string | null) => {
  token = t;
  if (t) sessionStorage.setItem("token", t);
  else sessionStorage.removeItem("token");
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { ...(init.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export async function login(username: string, password: string) {
  const r = await api<{ access_token: string }>("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username, password }),
  });
  setToken(r.access_token);
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
```

#### `frontend/src/LogViewer.tsx`

```tsx
import { ReactNode, useEffect, useRef, useState } from "react";
import { api, getToken } from "./api";

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3>{title}</h3>
          <button className="btn ghost" onClick={onClose}>✕ Close</button>
        </div>
        {children}
      </aside>
    </div>
  );
}

type Props = { ns: string; pod: string; containers: string[]; startLive?: boolean; onClose: () => void };

export default function LogViewer({ ns, pod, containers, startLive = true, onClose }: Props) {
  const [container, setContainer] = useState(containers[0] ?? "");
  const [live, setLive] = useState(startLive);
  const [previous, setPrevious] = useState(false);
  const [tail, setTail] = useState(200);
  const [filter, setFilter] = useState("");
  const [follow, setFollow] = useState(true);
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    setLines([]);
    setStatus("");
    const base = `/namespaces/${ns}/pods/${pod}`;
    if (!live) {
      let cancelled = false;
      const q = new URLSearchParams({ tail: String(tail), previous: String(previous), container });
      api<{ logs: string }>(`${base}/logs?${q}`)
        .then((r) => !cancelled && setLines(r.logs.split("\n")))
        .catch((e) => !cancelled && setStatus(e.message));
      return () => {
        cancelled = true;
      };
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = new URLSearchParams({ token: getToken() ?? "", tail: String(tail), container });
    const ws = new WebSocket(`${proto}://${location.host}/api/ws${base}/logs?${q}`);
    setStatus("connecting…");
    ws.onopen = () => setStatus("live");
    ws.onmessage = (e) => setLines((l) => [...l, e.data].slice(-5000));
    ws.onclose = (e) => setStatus(e.code >= 4400 ? `rejected (${e.code})` : "disconnected");
    return () => ws.close();
  }, [ns, pod, container, live, previous, tail]);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  const shown = filter ? lines.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : lines;

  return (
    <Drawer
      onClose={onClose}
      title={
        <>
          <span className="muted">Logs ·</span> {pod}{" "}
          <span className={`pill ${status === "live" ? "ok" : "muted"}`}><i />{status || (live ? "live" : "snapshot")}</span>
        </>
      }
    >
      <div className="toolbar">
        <select value={container} onChange={(e) => setContainer(e.target.value)}>
          {containers.map((c) => <option key={c}>{c}</option>)}
        </select>
        <div className="seg">
          <button className={live ? "on" : ""} onClick={() => setLive(true)}>Live</button>
          <button className={!live ? "on" : ""} onClick={() => setLive(false)}>Snapshot</button>
        </div>
        <select value={tail} onChange={(e) => setTail(+e.target.value)}>
          {[50, 200, 500, 1000, 5000].map((n) => <option key={n} value={n}>last {n}</option>)}
        </select>
        {!live && <label className="check"><input type="checkbox" checked={previous} onChange={(e) => setPrevious(e.target.checked)} /> previous</label>}
        <input className="grow" placeholder="Filter lines…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <label className="check"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> autoscroll</label>
        <button className="btn" onClick={() => navigator.clipboard.writeText(shown.join("\n"))}>Copy</button>
      </div>
      <pre ref={box} className="logs">{shown.join("\n") || (status ? "" : "Waiting for logs…")}</pre>
    </Drawer>
  );
}
```

#### `frontend/src/main.tsx`

```tsx
import { ReactNode, StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, ApiError, login, Me, post, setToken } from "./api";
import LogViewer, { Drawer } from "./LogViewer";
import "./styles.css";

type Dep = { name: string; replicas: number; ready: number; image: string };
type Pod = { name: string; phase: string; ready: boolean; restarts: number; node: string; containers: string[]; image: string; created: string | null };
type Svc = { name: string; type: string; cluster_ip: string; ports: string[] };
type Ev = { type: string; reason: string; message: string; object: string; time: string | null };
type Rel = { name: string; namespace: string; chart: string; app_version: string; revision: number; status: string };
type RelDetail = Rel & { values: unknown; history: { revision: number; status: string }[] };
type Cm = { name: string; keys: string[]; created: string | null };
type Sec = { name: string; type: string; keys: string[]; created: string | null };
type Kind = "ok" | "warn" | "bad" | "muted";
type View = "deployments" | "pods" | "services" | "configmaps" | "secrets" | "helm" | "events" | "logs";

const ACTIONS: { key: View; icon: string; label: string; hint: string; minRole?: string }[] = [
  { key: "deployments", icon: "🚀", label: "List deployments", hint: "Status, scale & restart" },
  { key: "pods", icon: "📦", label: "List pods", hint: "Phase, restarts, node" },
  { key: "services", icon: "🔌", label: "List services", hint: "Types, IPs and ports" },
  { key: "configmaps", icon: "🗂️", label: "List configmaps", hint: "Names and keys" },
  { key: "secrets", icon: "🔐", label: "List secrets", hint: "Names and key names only", minRole: "developer" },
  { key: "helm", icon: "⎈", label: "List Helm releases", hint: "Charts, revisions, values" },
  { key: "events", icon: "⚡", label: "List events", hint: "Recent cluster events" },
  { key: "logs", icon: "📜", label: "Get pod logs", hint: "Live tail or snapshot" },
];
const RANK: Record<string, number> = { viewer: 1, developer: 2, admin: 3 };

function useFetch<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);
  useEffect(() => {
    if (!path) return;
    let off = false;
    const load = () =>
      api<T>(path).then((d) => !off && (setData(d), setError("")), (e) => !off && setError(e.message));
    load();
    const id = refreshMs ? setInterval(load, refreshMs) : undefined;
    return () => {
      off = true;
      if (id) clearInterval(id);
    };
  }, [path, refreshMs, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

const age = (iso: string | null) => {
  if (!iso) return "–";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 129600) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
};

const Pill = ({ kind, children }: { kind: Kind; children: ReactNode }) => (
  <span className={`pill ${kind}`}><i />{children}</span>
);

const podKind = (p: Pod): Kind =>
  p.phase === "Running" && p.ready ? "ok" : p.phase === "Succeeded" ? "muted" : ["Pending", "ContainerCreating", "Running"].includes(p.phase) ? "warn" : "bad";

const Keys = ({ keys }: { keys: string[] }) => (
  <div className="chips">{keys.length ? keys.map((k) => <code key={k}>{k}</code>) : <span className="muted">none</span>}</div>
);

function Login({ onDone }: { onDone: () => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try { await login(u, p); onDone(); } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
      }}>
        <div className="logo big">⎈</div>
        <h2>Kubernetes Portal</h2>
        <p className="muted">Sign in to manage your workloads</p>
        <input placeholder="Username" value={u} onChange={(e) => setU(e.target.value)} autoFocus />
        <input placeholder="Password" type="password" value={p} onChange={(e) => setP(e.target.value)} />
        <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        {err && <p className="err">{err}</p>}
      </form>
    </div>
  );
}

function Table({ cols, rows, empty }: { cols: string[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="muted pad">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function Result({ ns, view, me, toast }: { ns: string; view: View; me: Me; toast: (m: string, bad?: boolean) => void }) {
  const meta = ACTIONS.find((a) => a.key === view)!;
  const canAct = me.role !== "viewer";
  const base = `/namespaces/${ns}`;
  const path = view === "helm" ? "/helm" : `${base}/${view === "logs" ? "pods" : view}`;
  const [auto, setAuto] = useState(false);
  const { data, error, reload } = useFetch<any[]>(path, auto ? 5000 : 0);
  const [q, setQ] = useState("");
  const [warnOnly, setWarnOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [logPod, setLogPod] = useState<{ pod: Pod; live: boolean } | null>(null);
  const [rel, setRel] = useState<RelDetail | null>(null);

  const match = (s: string) => !q || s.toLowerCase().includes(q.toLowerCase());
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try { await fn(); toast(ok); reload(); } catch (e) { toast((e as ApiError).message, true); }
    setBusy("");
  };

  let body: ReactNode = null;
  let count = 0;
  if (data) {
    if (view === "deployments") {
      const l = (data as Dep[]).filter((d) => match(d.name)); count = l.length;
      const scale = (d: Dep, n: number) => act(`s-${d.name}`, () => post(`${base}/deployments/${d.name}/scale`, { replicas: n }), `${d.name} → ${n} replicas`);
      body = <Table empty="No deployments found." cols={["Name", "Ready", "Image", ...(canAct ? ["Actions"] : [])]}
        rows={l.map((d) => [
          <strong>{d.name}</strong>,
          <div className="ready"><div className="bar"><div className={d.ready === d.replicas ? "fill ok" : "fill warn"} style={{ width: `${d.replicas ? (d.ready / d.replicas) * 100 : 100}%` }} /></div><span>{d.ready}/{d.replicas}</span></div>,
          <code>{d.image}</code>,
          ...(canAct ? [<div className="actions">
            <div className="stepper">
              <button disabled={!!busy || d.replicas <= 0} title="Scale down" onClick={() => scale(d, d.replicas - 1)}>−</button>
              <span>{d.replicas}</span>
              <button disabled={!!busy || d.replicas >= 20} title="Scale up" onClick={() => scale(d, d.replicas + 1)}>+</button>
            </div>
            <button className="btn" disabled={!!busy} onClick={() => act(`r-${d.name}`, () => post(`${base}/deployments/${d.name}/restart`), `Restarting ${d.name}`)}>↻ Restart</button>
          </div>] : []),
        ])} />;
    } else if (view === "pods" || view === "logs") {
      const l = (data as Pod[]).filter((p) => match(p.name)); count = l.length;
      body = <Table empty="No pods found." cols={["Name", "Status", "Restarts", "Age", "Node", view === "logs" ? "Get logs" : "Image"]}
        rows={l.map((p) => [
          <strong>{p.name}</strong>, <Pill kind={podKind(p)}>{p.phase}</Pill>,
          p.restarts > 3 ? <span className="bad-text">{p.restarts}</span> : p.restarts, age(p.created), p.node,
          view === "logs"
            ? <div className="actions"><button className="btn primary sm" onClick={() => setLogPod({ pod: p, live: true })}>▶ Live tail</button><button className="btn sm" onClick={() => setLogPod({ pod: p, live: false })}>Snapshot</button></div>
            : <code>{p.image}</code>,
        ])} />;
    } else if (view === "services") {
      const l = (data as Svc[]).filter((s) => match(s.name)); count = l.length;
      body = <Table empty="No services found." cols={["Name", "Type", "Cluster IP", "Ports"]}
        rows={l.map((s) => [<strong>{s.name}</strong>, <Pill kind="muted">{s.type}</Pill>, s.cluster_ip, s.ports.join(", ")])} />;
    } else if (view === "configmaps") {
      const l = (data as Cm[]).filter((c) => match(c.name)); count = l.length;
      body = <Table empty="No configmaps found." cols={["Name", "Keys", "Age"]}
        rows={l.map((c) => [<strong>{c.name}</strong>, <Keys keys={c.keys} />, age(c.created)])} />;
    } else if (view === "secrets") {
      const l = (data as Sec[]).filter((s) => match(s.name)); count = l.length;
      body = <Table empty="No secrets found." cols={["Name", "Type", "Keys (values are never shown)", "Age"]}
        rows={l.map((s) => [<strong>{s.name}</strong>, <Pill kind="muted">{s.type}</Pill>, <Keys keys={s.keys} />, age(s.created)])} />;
    } else if (view === "helm") {
      const l = (data as Rel[]).filter((r) => r.namespace === ns && match(r.name)); count = l.length;
      body = <Table empty="No Helm releases in this namespace." cols={["Release", "Chart", "App version", "Revision", "Status", ""]}
        rows={l.map((r) => [
          <strong>{r.name}</strong>, r.chart, r.app_version, r.revision, <Pill kind={r.status === "deployed" ? "ok" : "warn"}>{r.status}</Pill>,
          <button className="btn sm" onClick={() => api<RelDetail>(`/namespaces/${r.namespace}/helm/${r.name}`).then(setRel, (e) => toast(e.message, true))}>Values & history</button>,
        ])} />;
    } else if (view === "events") {
      const l = (data as Ev[]).filter((e) => (!warnOnly || e.type !== "Normal") && match(`${e.object} ${e.message} ${e.reason}`)).slice(0, 50); count = l.length;
      body = <Table empty="No events." cols={["Type", "Reason", "Object", "Message", "Age"]}
        rows={l.map((e) => [<Pill kind={e.type === "Normal" ? "muted" : "warn"}>{e.type}</Pill>, e.reason, e.object, e.message, age(e.time)])} />;
    }
  }

  return (
    <section className="card">
      <div className="card-head">
        <span className="icon">{meta.icon}</span>
        <h3>{meta.label}</h3>
        <span className="muted">in</span><code>{ns}</code>
        {data && <span className="count">{count}</span>}
        <span className="spacer" />
        {view === "events" && <label className="check"><input type="checkbox" checked={warnOnly} onChange={(e) => setWarnOnly(e.target.checked)} /> warnings only</label>}
        <input className="mini" placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto-refresh</label>
        <button className="btn sm" onClick={reload}>↻ Refresh</button>
      </div>
      {error ? <p className="err pad">{error}</p> : !data ? <div className="skeleton" /> : body}
      {logPod && <LogViewer key={logPod.pod.name + logPod.live} ns={ns} pod={logPod.pod.name} containers={logPod.pod.containers} startLive={logPod.live} onClose={() => setLogPod(null)} />}
      {rel && (
        <Drawer title={<><span className="muted">Helm ·</span> {rel.name}</>} onClose={() => setRel(null)}>
          <p className="muted">{rel.chart} · revision {rel.revision} · {rel.status}</p>
          <pre className="logs">{JSON.stringify({ values: rel.values, history: rel.history }, null, 2)}</pre>
        </Drawer>
      )}
    </section>
  );
}

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authed, setAuthed] = useState(!!sessionStorage.getItem("token"));
  const [ns, setNs] = useState("");
  const [view, setView] = useState<View | null>(null);
  const [toastMsg, setToastMsg] = useState<{ m: string; bad: boolean } | null>(null);
  const logout = useCallback(() => { setToken(null); setMe(null); setAuthed(false); setView(null); }, []);
  const toast = useCallback((m: string, bad = false) => {
    setToastMsg({ m, bad });
    setTimeout(() => setToastMsg(null), 3500);
  }, []);

  useEffect(() => {
    if (authed) api<Me>("/me").then(setMe, logout);
  }, [authed, logout]);
  const { data: namespaces } = useFetch<string[]>(authed ? "/namespaces" : null);
  useEffect(() => {
    if (namespaces?.length && !namespaces.includes(ns)) setNs(namespaces[0]);
  }, [namespaces, ns]);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  if (!me) return <p className="muted pad">Loading…</p>;

  return (
    <div className="app">
      <header>
        <div className="brand"><span className="logo">⎈</span><b>K8s Portal</b></div>
        <div className="ns-picker">
          <span className="muted">Namespace</span>
          <select value={ns} onChange={(e) => setNs(e.target.value)}>
            {(namespaces ?? []).map((n) => <option key={n}>{n}</option>)}
          </select>
        </div>
        <span className="spacer" />
        <span className="user"><b>{me.name}</b><em>{me.role}</em></span>
        <button className="btn ghost" onClick={logout}>Sign out</button>
      </header>
      <main>
        <div className="actions-grid">
          {ACTIONS.map((a) => {
            const locked = a.minRole ? RANK[me.role] < RANK[a.minRole] : false;
            return (
              <button key={a.key} disabled={!ns || locked} className={`action ${view === a.key ? "on" : ""}`}
                title={locked ? `Requires ${a.minRole} role` : a.hint} onClick={() => setView(a.key)}>
                <span className="icon">{a.icon}</span>
                <span><b>{a.label}</b><small>{locked ? `Requires ${a.minRole}` : a.hint}</small></span>
              </button>
            );
          })}
        </div>
        {!ns ? <p className="muted pad">No accessible namespaces.</p>
          : view ? <Result key={`${ns}-${view}`} ns={ns} view={view} me={me} toast={toast} />
          : <div className="empty card"><div className="logo big">⎈</div><h3>What would you like to do in <code>{ns}</code>?</h3><p className="muted">Choose an action above to get started.</p></div>}
      </main>
      {toastMsg && <div className={`toast ${toastMsg.bad ? "bad" : ""}`}>{toastMsg.m}</div>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
```

#### `frontend/src/styles.css`

```css
:root {
  --bg: #0b1020; --panel: #121933; --panel2: #1a2344; --line: #263056; --text: #e6e9f5; --muted: #8b95b8;
  --accent: #6d7cff; --accent2: #a06bff; --ok: #34d399; --warn: #fbbf24; --bad: #f87171;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text); background: var(--bg); color-scheme: dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: radial-gradient(1200px 600px at 10% -10%, #1b2a6b55, transparent), radial-gradient(900px 500px at 100% 0%, #5b2a8a40, transparent), var(--bg); min-height: 100vh; }
code { font-size: 12px; color: #aab4ff; background: #ffffff0d; padding: 2px 6px; border-radius: 6px; }
.muted { color: var(--muted); } .err { color: var(--bad); } .pad { padding: 16px 20px; } .bad-text { color: var(--bad); font-weight: 700; }
.spacer { flex: 1; }

header { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 16px; padding: 12px 28px; background: #0b1020cc; backdrop-filter: blur(14px); border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: center; gap: 10px; font-size: 17px; }
.logo { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; background: linear-gradient(135deg, var(--accent), var(--accent2)); font-size: 20px; }
.ns-picker { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.user { display: flex; flex-direction: column; line-height: 1.15; text-align: right; font-size: 13px; }
.user em { font-style: normal; font-size: 11px; color: var(--accent); text-transform: uppercase; letter-spacing: .06em; }

main { max-width: 1500px; margin: 0 auto; padding: 24px 28px 60px; display: flex; flex-direction: column; gap: 20px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; overflow: hidden; min-width: 0; box-shadow: 0 8px 30px #0006; }
.card-head { display: flex; align-items: center; gap: 10px; padding: 14px 18px; border-bottom: 1px solid var(--line); }
.card-head h3 { margin: 0; font-size: 15px; }
.icon { font-size: 18px; }
.mini { width: 170px; padding: 5px 10px !important; font-size: 13px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.actions-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.action { display: flex; align-items: center; gap: 12px; text-align: left; padding: 14px 16px; border-radius: 14px; cursor: pointer; background: linear-gradient(160deg, var(--panel2), var(--panel)); border: 1px solid var(--line); transition: .15s; }
.action .icon { font-size: 24px; display: grid; place-items: center; width: 42px; height: 42px; border-radius: 12px; background: #ffffff0d; }
.action b { display: block; font-size: 14px; } .action small { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
.action:hover:not(:disabled) { transform: translateY(-2px); border-color: var(--accent); box-shadow: 0 8px 24px #6d7cff22; }
.action.on { border-color: var(--accent); background: linear-gradient(160deg, #2a3570, var(--panel2)); box-shadow: 0 0 0 1px var(--accent) inset; }
.action:disabled { opacity: .45; cursor: not-allowed; }
.empty { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 56px 20px; text-align: center; }
.empty h3 { margin: 6px 0 0; font-size: 18px; } .empty p { margin: 0; }
.empty .logo.big { margin: 0; }
.collapse { background: none; border: 0; color: var(--muted); cursor: pointer; font-size: 14px; padding: 0; }
.count { background: var(--panel2); color: var(--muted); font-size: 12px; padding: 2px 9px; border-radius: 99px; }

.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); padding: 10px 18px; }
td { padding: 11px 18px; font-size: 13.5px; border-top: 1px solid #ffffff0a; white-space: nowrap; }
tbody tr:hover { background: #ffffff08; }

.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 99px; font-size: 12px; font-weight: 600; background: #ffffff0f; }
.pill i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--ok); background: #34d3991a; } .pill.warn { color: var(--warn); background: #fbbf241a; }
.pill.bad { color: var(--bad); background: #f871711a; } .pill.muted { color: var(--muted); }

.ready { display: flex; align-items: center; gap: 10px; }
.bar { width: 80px; height: 6px; border-radius: 9px; background: #ffffff14; overflow: hidden; }
.fill { height: 100%; border-radius: 9px; } .fill.ok { background: var(--ok); } .fill.warn { background: var(--warn); }
.actions { display: flex; gap: 10px; align-items: center; }
.stepper { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 9px; overflow: hidden; }
.stepper button { background: var(--panel2); border: 0; color: var(--text); width: 28px; height: 28px; cursor: pointer; font-size: 16px; }
.stepper button:hover:not(:disabled) { background: var(--accent); } .stepper button:disabled { opacity: .35; cursor: not-allowed; }
.stepper span { min-width: 30px; text-align: center; font-weight: 600; }

button, select, input { font: inherit; color: var(--text); }
select, input:not([type=checkbox]) { background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 7px 12px; outline: none; }
select:focus, input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px #6d7cff30; }
.btn { background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; padding: 7px 14px; cursor: pointer; transition: .15s; }
.btn:hover:not(:disabled) { border-color: var(--accent); } .btn:disabled { opacity: .5; cursor: not-allowed; }
.btn.primary { background: linear-gradient(135deg, var(--accent), var(--accent2)); border: 0; font-weight: 600; }
.btn.primary:hover:not(:disabled) { filter: brightness(1.12); }
.btn.ghost { background: none; } .btn.sm { padding: 4px 11px; font-size: 12.5px; }
.check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }

.skeleton { height: 90px; margin: 16px; border-radius: 10px; background: linear-gradient(90deg, #ffffff08, #ffffff18, #ffffff08); background-size: 200% 100%; animation: shimmer 1.4s infinite; }
@keyframes shimmer { to { background-position: -200% 0; } }

.backdrop { position: fixed; inset: 0; background: #050810a8; backdrop-filter: blur(3px); z-index: 50; display: flex; justify-content: flex-end; animation: fade .15s; }
.drawer { width: min(980px, 100vw); height: 100%; background: var(--panel); border-left: 1px solid var(--line); padding: 18px 22px; display: flex; flex-direction: column; gap: 12px; animation: slide .2s ease-out; }
.drawer-head { display: flex; justify-content: space-between; align-items: center; } .drawer-head h3 { margin: 0; display: flex; gap: 8px; align-items: center; }
@keyframes slide { from { transform: translateX(40px); opacity: 0; } } @keyframes fade { from { opacity: 0; } }
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; } .grow { flex: 1; min-width: 140px; }
.seg { display: inline-flex; background: var(--bg); border: 1px solid var(--line); border-radius: 10px; padding: 3px; }
.seg button { background: none; border: 0; padding: 4px 14px; border-radius: 8px; cursor: pointer; color: var(--muted); }
.seg button.on { background: var(--accent); color: #fff; }
.logs { flex: 1; overflow: auto; margin: 0; padding: 14px; background: #060913; border: 1px solid var(--line); border-radius: 12px; color: #b8f5d6; font: 12px/1.55 ui-monospace, "SF Mono", Menlo, monospace; white-space: pre-wrap; word-break: break-all; }

.toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--panel2); border: 1px solid var(--ok); padding: 11px 20px; border-radius: 12px; z-index: 100; box-shadow: 0 10px 40px #000a; animation: slide .2s; }
.toast.bad { border-color: var(--bad); }

.login-wrap { min-height: 100vh; display: grid; place-items: center; }
.login { width: 360px; padding: 34px; display: flex; flex-direction: column; gap: 12px; text-align: center; }
.login h2 { margin: 4px 0 0; } .login p { margin: 0 0 6px; }
.logo.big { width: 54px; height: 54px; font-size: 32px; margin: 0 auto; border-radius: 16px; }

@media (max-width: 900px) { header { flex-wrap: wrap; } } 
```

## 8. Run the frontend

In a **second terminal**:

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173>, log in as `dev` / `dev`, pick a namespace, and click the action buttons: **List pods → ▶ Logs** (or **Get pod logs → ▶ Live tail**), **List deployments** (use **− / +** and **↻ Restart**), **List configmaps**, **List secrets**, **List Helm releases → Values & history**. Log in as `viewer` to confirm *List secrets* is locked and the scale/restart buttons disappear (and that the API returns 403 if called directly).

## 8b. One-command start (both backend and frontend)

`run.sh` creates the venv, installs dependencies on first run, starts the backend on :8000, waits until it is healthy, then starts the frontend on :5173. Ctrl+C stops both.

#### `run.sh`

```bash
#!/usr/bin/env bash
# Starts backend (:8000, localhost only) and frontend (:5173). Usage: [HOST=0.0.0.0] ./run.sh [mock|real]   (default: real)
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-real}"
[ "$MODE" = "mock" ] && export SSP_MOCK_MODE=true || export SSP_MOCK_MODE=false
export SSP_JWT_SECRET="${SSP_JWT_SECRET:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')}"

if [ ! -d backend/.venv ]; then
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -q -r backend/requirements.txt
fi
[ -d frontend/node_modules ] || (cd frontend && npm install)

(cd backend && PYTHONPATH=. .venv/bin/uvicorn app.main:app --port 8000) &
BACKEND=$!
trap 'kill $BACKEND 2>/dev/null' EXIT

for _ in $(seq 1 30); do
  curl -sf localhost:8000/api/health >/dev/null && break
  sleep 1
done
curl -sf localhost:8000/api/health >/dev/null || { echo "Backend failed to start"; exit 1; }

echo "Backend ready (mode=$MODE). Open http://${HOST:-localhost}:5173  (login: admin/admin)"
cd frontend && npm run dev -- --host "${HOST:-localhost}"
```

Run it from the project root:

```bash
bash run.sh          # real mode: uses your current kubectl context (e.g. kind)
bash run.sh mock     # fake demo data, no cluster needed
```

**Access from another machine / public IP** (e.g. an EC2 or Lightsail VM): by default the UI listens on `localhost` only. Start it with

```bash
HOST=0.0.0.0 bash run.sh          # add `mock` for demo data
```

then open `http://<public-ip>:5173` and allow inbound TCP **5173** in your firewall (Lightsail: Networking tab; AWS: security group). Only the UI port is exposed; the backend stays on `127.0.0.1:8000` and the UI proxies `/api` and the live-log WebSocket to it. This is plain HTTP, so restrict the firewall rule to your own IP (or put HTTPS in front) and change the default passwords before exposing it.

Then open <http://localhost:5173> and log in as `admin` / `admin`. If login shows a proxy error (`ECONNREFUSED`), the backend is not running — check the terminal output for its error.

## 9. Package for deployment (Docker + Helm chart)

The Dockerfile builds the UI, bundles it with the backend (FastAPI serves the static files) and installs the `helm` binary. The Helm chart deploys the portal with a ServiceAccount and a read-mostly ClusterRole.

#### `Dockerfile`

```dockerfile
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ARG HELM_VERSION=v3.16.2
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz | tar xz -C /tmp \
    && mv /tmp/linux-${ARCH}/helm /usr/local/bin/helm \
    && apt-get purge -y curl && rm -rf /var/lib/apt/lists/* /tmp/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
RUN useradd -u 10001 -m ssp
USER 10001
ENV SSP_MOCK_MODE=false
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

#### `.gitignore`

```text
# Python
.venv/
venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.mypy_cache/
*.egg-info/

# Node / frontend build
node_modules/
dist/

# Local config and secrets - never commit
.env
.env.*
*kubeconfig*
*.pem
*.key

# Logs and OS/editor files
*.log
.DS_Store
.idea/
.vscode/
```

#### `deploy/helm/k8-ssp/Chart.yaml`

```yaml
apiVersion: v2
name: k8-ssp
description: Self-service Kubernetes portal
type: application
version: 0.1.0
appVersion: "0.1.0"
```

#### `deploy/helm/k8-ssp/values.yaml`

```yaml
image:
  repository: k8-ssp
  tag: latest
  pullPolicy: IfNotPresent

service:
  type: ClusterIP
  port: 80

# Portal login users: "user:password:role[:ns1|ns2]" comma-separated. CHANGE THESE.
users: "viewer:change-me:viewer,dev:change-me:developer:default,admin:change-me:admin"
# Must be >= 32 bytes. Leave empty to generate a random one on install.
jwtSecret: ""

# Cluster permissions granted to the portal ServiceAccount.
rbac:
  allowActions: true   # restart / scale deployments
  listSecrets: true    # "List secrets" action (names and key names only; values are never returned, but RBAC still grants read)
  helmReleases: true   # read Helm release Secrets (needed for helm list; exposes release data cluster-wide)

resources:
  requests: { cpu: 50m, memory: 128Mi }
  limits: { cpu: 500m, memory: 256Mi }
```

#### `deploy/helm/k8-ssp/templates/rbac.yaml`

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .Release.Name }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ .Release.Name }}
rules:
  - apiGroups: [""]
    resources: ["namespaces", "pods", "pods/log", "services", "events", "configmaps"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list"]
{{- if .Values.rbac.allowActions }}
  - apiGroups: ["apps"]
    resources: ["deployments", "deployments/scale"]
    verbs: ["patch"]
{{- end }}
{{- if or .Values.rbac.helmReleases .Values.rbac.listSecrets }}
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list"]
{{- end }}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ .Release.Name }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {{ .Release.Name }}
subjects:
  - kind: ServiceAccount
    name: {{ .Release.Name }}
    namespace: {{ .Release.Namespace }}
```

#### `deploy/helm/k8-ssp/templates/portal.yaml`

```yaml
{{- $existing := lookup "v1" "Secret" .Release.Namespace (printf "%s-config" .Release.Name) }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-config
type: Opaque
stringData:
  SSP_USERS: {{ .Values.users | quote }}
  {{- if .Values.jwtSecret }}
  SSP_JWT_SECRET: {{ .Values.jwtSecret | quote }}
  {{- else if $existing }}
  SSP_JWT_SECRET: {{ index $existing.data "SSP_JWT_SECRET" | b64dec | quote }}
  {{- else }}
  SSP_JWT_SECRET: {{ randAlphaNum 48 | quote }}
  {{- end }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ .Release.Name }}
  template:
    metadata:
      labels:
        app: {{ .Release.Name }}
      annotations:
        checksum/config: {{ .Values.users | sha256sum }}
    spec:
      serviceAccountName: {{ .Release.Name }}
      containers:
        - name: portal
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: 8000
          envFrom:
            - secretRef:
                name: {{ .Release.Name }}-config
          env:
            - name: SSP_MOCK_MODE
              value: "false"
            - name: HOME
              value: /tmp
          readinessProbe:
            httpGet: { path: /api/health, port: 8000 }
          livenessProbe:
            httpGet: { path: /api/health, port: 8000 }
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
          volumeMounts:
            - { name: tmp, mountPath: /tmp }
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
      volumes:
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ .Release.Name }}
spec:
  type: {{ .Values.service.type }}
  selector:
    app: {{ .Release.Name }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: 8000
```

## 10. Connect a real cluster

**Option A — run on your laptop with a kubeconfig** (needs `helm` on your PATH for the Helm tab):

```bash
cd backend
SSP_MOCK_MODE=false SSP_KUBECONFIG=$HOME/.kube/config \
SSP_JWT_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
PYTHONPATH=. .venv/bin/uvicorn app.main:app --port 8000
```

**Option B — deploy inside the cluster:**

```bash
docker build -t <registry>/k8-ssp:0.1.0 .      # then docker push, or import into your cluster runtime
helm lint deploy/helm/k8-ssp
helm install ssp deploy/helm/k8-ssp \
  --set image.repository=<registry>/k8-ssp --set image.tag=0.1.0 \
  --set users="admin:<strong-password>:admin,dev:<password>:developer:default"
kubectl port-forward svc/ssp 8080:80            # http://localhost:8080
```

## 11. Security checklist (discuss with students)

- Change all default passwords and the JWT secret; serve over HTTPS.
- The portal's ServiceAccount is the real security boundary: keep its ClusterRole minimal. Listing Helm releases and the *List secrets* action need Secret read access (`--set rbac.helmReleases=false --set rbac.listSecrets=false` removes it). The backend only returns secret names, types and key names, never values, and restricts the action to developer/admin.
- Per-user namespace limits are enforced by the portal, not by Kubernetes.
- The live-log WebSocket carries the token in its URL — don't log URLs at your proxy.
- Production upgrade path: OIDC login (Keycloak / GitHub) instead of env-defined users.

## 12. Build it with an AI assistant (exercise)

Instead of copying the files, ask your AI assistant step by step and compare with this guide:

1. *"Create a FastAPI backend with JWT login and roles viewer/developer/admin, using pydantic-settings."*
2. *"Add a Provider interface with a MockProvider (fake namespaces, deployments, pods, services, events, Helm releases) and a RealProvider using kubernetes-asyncio."*
3. *"Add REST endpoints for those resources, pod logs with tail/previous/container, and a WebSocket endpoint for live logs. Enforce per-user namespace access and cap log size."*
4. *"Add restart and scale endpoints for deployments, restricted to developer role, with audit logging."*
5. *"Write pytest tests for auth, roles, namespace isolation and the WebSocket."*
6. *"Create a React + TypeScript + Vite UI with login, namespace selector, tabs, and a log viewer with live tail."*
7. *"Write a multi-stage Dockerfile and a Helm chart with a least-privilege ClusterRole."*

Review every AI-generated change: run the tests, read the RBAC rules, and try to break the access controls.

## 13. Troubleshooting

| Problem | Fix |
|---|---|
| `Form data requires "python-multipart"` | `pip install -r requirements.txt` again (it is listed there) |
| UI shows 401 / loops to login | Backend restarted with a new secret — log in again |
| Live tail shows `rejected (4429)` | Too many open streams for that user — close a log window |
| Helm tab errors on a real cluster | `helm` must be on PATH and the kubeconfig must be valid |
| Port 5173 / 8000 in use | Stop the other process or change the port (and the proxy in `vite.config.ts`) |
