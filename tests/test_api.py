import base64
import importlib
import os
import tempfile

from fastapi.testclient import TestClient

def test_file_flow(monkeypatch):
    with tempfile.TemporaryDirectory() as folder:
        monkeypatch.setenv("STORAGE_DIR", folder)
        monkeypatch.setenv("ADMIN_PASSWORD", "a" * 40)
        monkeypatch.setenv("API_TOKEN", "b" * 40)
        monkeypatch.setenv("SESSION_SECRET", "c" * 40)
        import app as module
        importlib.reload(module)
        client = TestClient(module.app, base_url="https://testserver")
        h = {"X-API-Key": "b" * 40}
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/api/list").status_code == 401
        write = client.get("/api/write", params={"name": "hello.txt", "data": base64.b64encode("Привет".encode()).decode()}, headers=h)
        assert write.status_code == 200, write.text
        assert client.get("/api/read", params={"name": "hello.txt"}, headers=h).json()["text"] == "Привет"
        assert len(client.get("/api/list", headers=h).json()["files"]) == 1
        assert client.get("/api/read", params={"name": "../etc/passwd"}, headers=h).status_code == 400
        assert client.get("/api/write", params={"name": "abc.txt", "data": "!"}, headers=h).status_code == 400
        login = client.post("/login", data={"password": "a" * 40})
        assert login.status_code == 200
        cookies = login.cookies
        assert client.get("/ui/files", cookies=cookies).status_code == 200
        res = client.post("/ui/text", json={"name": "pasted.txt", "text": "Тест"}, headers={"X-Atlas-UI": "1"}, cookies=cookies)
        assert res.status_code == 200, res.text
        res = client.delete("/ui/files", params={"name": "pasted.txt"}, headers={"X-Atlas-UI": "1"}, cookies=cookies)
        assert res.status_code == 200
