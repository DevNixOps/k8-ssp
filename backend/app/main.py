import logging
import os
from pathlib import Path
from collections import defaultdict
from contextlib import asynccontextmanager

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
    container: str | None = None,
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
async def live_logs(ws: WebSocket, namespace: str, pod: str, token: str, container: str | None = None, tail: int = 50):
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
