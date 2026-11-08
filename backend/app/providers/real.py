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
