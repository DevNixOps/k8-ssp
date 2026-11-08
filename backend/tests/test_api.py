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
