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
