import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(os.getenv("STORAGE_DIR", "/data")).resolve()
ROOT.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "files"
DATA.mkdir(exist_ok=True)
CHUNKS = ROOT / "chunks"
CHUNKS.mkdir(exist_ok=True)
MAX_SIZE = int(os.getenv("MAX_FILE_BYTES", "10485760"))
ADMIN = os.getenv("ADMIN_PASSWORD", "")
TOKEN = os.getenv("API_TOKEN", "")
SECRET = os.getenv("SESSION_SECRET", "")
if not all([ADMIN, TOKEN, SECRET]) or min(map(len, [ADMIN, TOKEN, SECRET])) < 24:
    raise RuntimeError("Set ADMIN_PASSWORD, API_TOKEN and SESSION_SECRET (24+ chars)")
app = FastAPI(title="Atlas File Gateway", docs_url=None, redoc_url=None, openapi_url=None)

def valid_name(name: str) -> str:
    if not name or len(name) > 160 or name in (".", "..") or not re.fullmatch(r"[\w.() +@-]+", name, re.UNICODE):
        raise HTTPException(400, "Invalid filename: use letters, digits, spaces, ._-()+@")
    if name.startswith("."):
        raise HTTPException(400, "Hidden files are not allowed")
    return name

def target(name: str) -> Path:
    return DATA / valid_name(name)

def save(name: str, data: bytes):
    if len(data) > MAX_SIZE:
        raise HTTPException(413, "File too large")
    p = target(name)
    tmp = DATA / ("." + secrets.token_hex(12))
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return {"name": p.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

def check_token(request: Request):
    key = request.headers.get("X-API-Key") or request.query_params.get("token", "")
    if not hmac.compare_digest(key, TOKEN):
        raise HTTPException(401, "Invalid API token")

def sign(payload: str):
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def current_session(request: Request):
    value = request.cookies.get("atlas_session", "")
    try:
        exp, nonce, signature = value.split(".")
        raw = f"{exp}.{nonce}"
        return int(exp) >= time.time() and hmac.compare_digest(sign(raw), signature)
    except (ValueError, TypeError):
        return False

def admin_only(request: Request):
    if not current_session(request):
        raise HTTPException(401, "Please log in")
    if request.method in ("POST", "DELETE", "PUT"):
        # Non-simple request header; browsers cannot forge this cross-origin without CORS.
        if request.headers.get("X-Atlas-UI") != "1":
            raise HTTPException(403, "Missing UI security header")

def file_listing():
    return [
        {"name": p.name, "size": p.stat().st_size, "modified": int(p.stat().st_mtime)}
        for p in sorted(DATA.iterdir(), key=lambda x: x.name.lower()) if p.is_file() and not p.name.startswith(".")
    ]

@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse("static/index.html")

@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password = str(form.get("password", ""))
    if not hmac.compare_digest(password, ADMIN):
        raise HTTPException(401, "Wrong password")
    expires = str(int(time.time() + 8 * 3600))
    raw = f"{expires}.{secrets.token_hex(12)}"
    resp = JSONResponse({"ok": True})
    resp.set_cookie("atlas_session", raw + "." + sign(raw), httponly=True,
                    secure=True, samesite="strict", max_age=28800)
    return resp

@app.post("/logout")
def logout(request: Request):
    admin_only(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("atlas_session")
    return resp

@app.get("/ui/files")
def ui_files(request: Request):
    admin_only(request)
    return file_listing()

@app.post("/ui/text")
async def ui_text(request: Request):
    admin_only(request)
    body = await request.json()
    data = str(body.get("text", "")).encode("utf-8")
    return save(str(body.get("name", "")), data)

@app.post("/ui/upload")
async def ui_upload(request: Request, file: UploadFile = File(...)):
    admin_only(request)
    chunks = bytearray()
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        chunks.extend(part)
        if len(chunks) > MAX_SIZE:
            raise HTTPException(413, "File too large")
    return save(file.filename or "", bytes(chunks))

@app.get("/ui/read")
def ui_read(request: Request, name: str):
    admin_only(request)
    p = target(name)
    if not p.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(p, filename=p.name)

@app.delete("/ui/files")
def ui_delete(request: Request, name: str):
    admin_only(request)
    p = target(name)
    if not p.is_file():
        raise HTTPException(404, "Not found")
    p.unlink()
    return {"deleted": name}

@app.get("/api/list")
def api_list(request: Request):
    check_token(request)
    return {"files": file_listing()}

@app.get("/api/read")
def api_read(request: Request, name: str, encoding: str = "text"):
    check_token(request)
    p = target(name)
    if not p.is_file():
        raise HTTPException(404, "Not found")
    data = p.read_bytes()
    if encoding == "base64":
        return {"name": name, "base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    if encoding != "text":
        raise HTTPException(400, "encoding must be text or base64")
    try:
        return {"name": name, "text": data.decode("utf-8")}
    except UnicodeDecodeError:
        raise HTTPException(415, "Binary file: use encoding=base64")

@app.get("/api/write")
def api_write(request: Request, name: str, data: str):
    check_token(request)
    if len(data) > 6000:
        raise HTTPException(413, "GET too long; use chunks or Web UI")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Invalid base64")
    return save(name, decoded)

@app.get("/api/chunk")
def api_chunk(request: Request, upload_id: str, index: int, total: int, data: str):
    check_token(request)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", upload_id) or not (1 <= total <= 5000) or not (0 <= index < total):
        raise HTTPException(400, "Invalid chunk coordinates")
    if len(data) > 6000:
        raise HTTPException(413, "Chunk too long")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Invalid base64")
    directory = CHUNKS / upload_id
    directory.mkdir(exist_ok=True)
    (directory / f"{index:05d}").write_bytes(decoded)
    return {"upload_id": upload_id, "received": index, "total": total}

@app.get("/api/finish")
def api_finish(request: Request, upload_id: str, total: int, name: str, sha256: str):
    check_token(request)
    valid_name(name)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", upload_id) or not (1 <= total <= 5000):
        raise HTTPException(400, "Invalid upload")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
        raise HTTPException(400, "Invalid SHA256")
    directory = CHUNKS / upload_id
    parts = [directory / f"{i:05d}" for i in range(total)]
    if not all(p.is_file() for p in parts):
        raise HTTPException(409, "Missing chunks")
    data = bytearray()
    for p in parts:
        data.extend(p.read_bytes())
        if len(data) > MAX_SIZE:
            raise HTTPException(413, "File too large")
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha256.lower()):
        raise HTTPException(409, "SHA256 mismatch")
    result = save(name, bytes(data))
    for p in parts:
        p.unlink()
    directory.rmdir()
    return result


def shared_signature(name: str, expires: int, digest: str) -> str:
    payload = f"share-read-v1\\n{name}\\n{expires}\\n{digest}"
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

@app.post("/ui/share")
async def ui_share(request: Request):
    admin_only(request)
    body = await request.json()
    name = valid_name(str(body.get("name", "")))
    p = target(name)
    if not p.is_file():
        raise HTTPException(404, "Not found")
    expires = int(time.time()) + 600
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    sig = shared_signature(name, expires, digest)
    from urllib.parse import urlencode
    path = "/shared?" + urlencode({"name": name, "expires": expires, "signature": sig})
    return {"path": path, "expires": expires}

@app.get("/shared")
def shared_read(name: str, expires: int, signature: str):
    name = valid_name(name)
    if expires < int(time.time()) or expires > int(time.time()) + 600:
        raise HTTPException(403, "Link expired or invalid")
    p = target(name)
    if not p.is_file():
        raise HTTPException(404, "Not found")
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    expected = shared_signature(name, expires, digest)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(403, "Invalid share signature")
    from fastapi.responses import PlainTextResponse, Response
    data = p.read_bytes()
    try:
        content = data.decode("utf-8")
        response = PlainTextResponse(content)
    except UnicodeDecodeError:
        response = Response(data, media_type="application/octet-stream")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response

@app.get("/health")
def health():
    return {"status": "ok"}
