from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Literal
import ipaddress
import sqlite3
import time

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import authenticate, pair
from .config import Config
from .service import Service
from .storage import fsync_dir, version


class BodyTooLarge(Exception):
    pass


class SecurityGate:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self.networks = [ipaddress.ip_network(network) for network in config.allowed_networks]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        peer = (scope.get('client') or ('', 0))[0]
        try:
            address = ipaddress.ip_address(peer)
            address = getattr(address, 'ipv4_mapped', None) or address
            permitted = any(address in network for network in self.networks)
        except ValueError:
            permitted = False
        if not permitted:
            return await JSONResponse({'detail': 'Client is outside the allowed LAN networks'}, 403)(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        host = headers.get("host", "").split(":")[0].lower()
        if host not in [h.lower() for h in self.config.hosts]:
            return await JSONResponse({"detail": "Host not allowed; configure the exact laptop address"}, 400)(scope, receive, send)
        origin = headers.get("origin")
        mutation = scope["method"] not in ("GET", "HEAD", "OPTIONS")
        bearer = headers.get("authorization", "").startswith("Bearer ")
        if mutation and ((origin and origin not in self.config.origins) or (not bearer and "cookie" in headers and not origin)):
            return await JSONResponse({"detail": "Origin not allowed"}, 403)(scope, receive, send)
        cap = 32 * 1024 * 1024 if scope["path"].endswith("/data") else 1024 * 1024
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
        if length < 0 or length > cap:
            return await JSONResponse({"detail": "Request exceeds the bounded request limit"}, 413)(scope, receive, send)
        consumed = 0
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > cap:
                raise BodyTooLarge()
            return message
        async def safe_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; worker-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
                ])
            await send(message)
        await self.app(scope, bounded_receive, safe_send)


class PairBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(default="Browser", min_length=1, max_length=100)
    client: Literal["browser", "cli"] = "browser"


class JobBody(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    expected_entries: int | None = Field(default=None, ge=0)


class Entry(BaseModel):
    path: str = Field(min_length=1, max_length=16000)
    size: int = Field(ge=0, le=2**53 - 1, strict=True)
    modified: int = Field(default=0, ge=0, strict=True)
    kind: Literal["file", "directory"] = "file"
    key: str | None = Field(default=None, min_length=1, max_length=32768)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Manifest(BaseModel):
    entries: list[Entry] = Field(min_length=1, max_length=500)
    allow_conflicts: bool = False


class SealBody(BaseModel):
    expected_entries: int | None = Field(default=None, ge=0)


class ResolveBody(BaseModel):
    action: Literal['rename', 'skip']
    path: str | None = Field(default=None, min_length=1, max_length=16000)


class CompleteBody(BaseModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class PinnedFileResponse(FileResponse):
    chunk_size = 1024 * 1024

    def __init__(self, path, pinned, limiter=None, idle_seconds=30, authorize=None, **kwargs):
        super().__init__(path, **kwargs)
        self.pinned = pinned
        self.limiter = limiter
        self.idle_seconds = idle_seconds
        self.authorize = authorize

    async def __call__(self, scope, receive, send):
        failure = None
        async with anyio.create_task_group() as tasks:
            async def watch_disconnect():
                while True:
                    if (await receive())['type'] == 'http.disconnect':
                        tasks.cancel_scope.cancel()
                        return
                    await anyio.sleep(0)  # Also yield for immediate in-process ASGI receivers.
            async def stream():
                nonlocal failure
                try:
                    async with self.limiter or nullcontext():
                        await self.serve(scope, receive, send)
                except Exception as exc:
                    failure = exc
                finally:
                    tasks.cancel_scope.cancel()
            tasks.start_soon(watch_disconnect)
            tasks.start_soon(stream)
        if failure:
            raise failure

    async def serve(self, scope, receive, send):
        scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"}}
        async def checked_send(message):
            if message["type"] in ("http.response.start", "http.response.body"):
                if self.authorize:
                    await anyio.to_thread.run_sync(self.authorize)
                if await anyio.to_thread.run_sync(version, Path(self.path)) != self.pinned:
                    raise RuntimeError("Source changed during download; response aborted")
            with anyio.fail_after(self.idle_seconds):
                await send(message)
        await super().__call__(scope, receive, checked_send)


def create_app(config=None):
    config = config or Config.load()

    @asynccontextmanager
    async def lifespan(app):
        service = Service(config)
        app.state.service = service
        service.on_shutdown = getattr(app.state, 'shutdown', None)
        try:
            await service.disk(service.recover)
            await service.cleanup_orphans()
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(service.verification_loop)
                tasks.start_soon(service.maintenance_loop)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            service.close()

    app = FastAPI(title="LAN Transfer", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(SecurityGate, config=config)

    def svc(request: Request):
        return request.app.state.service

    def session(request: Request, service=Depends(svc)):
        authorization = request.headers.get("authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else request.cookies.get("lan_session", "")
        return authenticate(service.repo, token, config.session_days)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.exception_handler(BodyTooLarge)
    async def body_error(request, exc):
        return JSONResponse({"detail": "Request body too large"}, 413)

    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse({"detail": "Storage access failed. Check free space, filesystem file-size limits, permissions and disk health."}, 507)

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse({'detail': 'Transfer database is temporarily unavailable; retry shortly.'}, 503, headers={'Retry-After': '2'})

    @app.get("/api/v1/health")
    def health(service=Depends(svc)):
        try:
            service.repo.one('SELECT 1')
        except sqlite3.Error:
            return JSONResponse({'status': 'degraded', 'protocol': 1, 'service': 'LAN Transfer'}, 503)
        if service.verification_error or service.maintenance_error:
            return JSONResponse({'status': 'degraded', 'protocol': 1, 'service': 'LAN Transfer'}, 503)
        return {"status": "online", "protocol": 1, "service": "LAN Transfer"}

    @app.post("/api/v1/pair")
    def pairing(body: PairBody, request: Request, response: Response, service=Depends(svc)):
        if body.client == "browser" and request.headers.get("origin") not in config.origins:
            raise HTTPException(403, "Browser pairing requires a configured same-origin request")
        token, sid = pair(service.repo, body.code, body.name, request.client.host if request.client else "unknown")
        if body.client == "browser":
            response.set_cookie("lan_session", token, httponly=True, samesite="strict", secure=config.secure, max_age=config.session_days * 86400)
            return {"id": sid}
        return {"id": sid, "token": token}

    @app.get("/api/v1/sessions")
    def sessions(user=Depends(session)):
        return user

    @app.delete("/api/v1/sessions/current")
    async def logout(response: Response, user=Depends(session), service=Depends(svc)):
        await service.revoke_session(user['id'])
        response.delete_cookie("lan_session")
        return {"revoked": True}

    @app.get("/api/v1/capabilities")
    def capabilities(user=Depends(session), service=Depends(svc)):
        return {"chunk_size": config.chunk_size, "active_files": config.active_files, "secure": config.secure,
                "disk": service.disk_status(), "protocol": 1, "identity": service.repo.one("SELECT value FROM settings WHERE key='identity'")["value"]}

    @app.post("/api/v1/jobs")
    def jobs_create(body: JobBody, user=Depends(session), service=Depends(svc)):
        return service.create_job(user["id"], body.key, body.name, body.expected_entries)

    @app.get('/api/v1/jobs/{jid}')
    def job_get(jid: str, user=Depends(session), service=Depends(svc)):
        return service.job(jid, user['id'])

    @app.get("/api/v1/jobs")
    def jobs_list(after: float = 0, after_id: str = '', limit: int = Query(100, ge=1, le=500), user=Depends(session), service=Depends(svc)):
        return service.repo.all("SELECT j.*,COALESCE(SUM(CASE WHEN u.state='skipped' THEN 0 ELSE u.size END),0) total,COALESCE(SUM(u.offset),0) committed,COUNT(u.id) count,SUM(CASE WHEN u.state='skipped' THEN 1 ELSE 0 END) skipped FROM jobs j LEFT JOIN uploads u ON u.job=j.id WHERE j.owner=? AND (j.created>? OR (?!='' AND j.created=? AND j.id>?)) GROUP BY j.id ORDER BY j.created,j.id LIMIT ?", (user["id"], after, after_id, after, after_id, limit))

    @app.post("/api/v1/jobs/{jid}/manifest")
    def manifest(jid: str, body: Manifest, user=Depends(session), service=Depends(svc)):
        if any(e.kind == "directory" and e.size != 0 for e in body.entries):
            raise HTTPException(422, "Directories must have size zero")
        return service.register(jid, user["id"], body.entries, body.allow_conflicts)

    @app.post("/api/v1/jobs/{jid}/seal")
    def seal(jid: str, body: SealBody | None = None, user=Depends(session), service=Depends(svc)):
        return service.seal(jid, user["id"], body.expected_entries if body else None)

    @app.get("/api/v1/jobs/{jid}/entries")
    def entries(jid: str, after: int = 0, limit: int = Query(500, ge=1, le=500), user=Depends(session), service=Depends(svc)):
        service.job(jid, user["id"])
        from .manifests import describe
        return describe(service.repo, service.repo.all("SELECT * FROM uploads WHERE job=? AND seq>? ORDER BY seq LIMIT ?", (jid, after, limit)))

    @app.post('/api/v1/uploads/{uid}/resolve')
    async def resolve_conflict(uid: str, body: ResolveBody, user=Depends(session), service=Depends(svc)):
        from .manifests import resolve
        async with service.lock(uid):
            await service.disk(service.upload, uid, user['id'])
            await service.disk(resolve, service.repo, uid, body.action, body.path)
            return await service.disk(service.upload, uid, user['id'])

    @app.get("/api/v1/uploads/{uid}")
    def upload_status(uid: str, user=Depends(session), service=Depends(svc)):
        result = service.upload(uid, user["id"])
        result.pop("owner", None)
        return result

    @app.get("/api/v1/uploads/{uid}/receipts")
    def receipts(uid: str, after: int = 0, limit: int = Query(500, ge=1, le=500), user=Depends(session), service=Depends(svc)):
        service.upload(uid, user["id"])
        return service.repo.all("SELECT offset,length,sha256 FROM receipts WHERE upload=? AND offset>=? ORDER BY offset LIMIT ?", (uid, after, limit))

    @app.put("/api/v1/uploads/{uid}/data", openapi_extra={
        "description": "Stream one atomic sequential chunk. Query status and receipts after an ambiguous acknowledgement. File bytes are never parsed as a buffered request body.",
        "requestBody": {"required": True, "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
        "parameters": [
            {"name": "Upload-Offset", "in": "header", "required": True, "schema": {"type": "integer", "minimum": 0}},
            {"name": "Upload-Length", "in": "header", "required": True, "schema": {"type": "integer", "minimum": 1, "maximum": 33554432}},
            {"name": "Upload-SHA256", "in": "header", "required": True, "schema": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
            {"name": "Upload-Request-ID", "in": "header", "required": True, "schema": {"type": "string", "minLength": 1, "maxLength": 100}},
        ],
    })
    async def data(uid: str, request: Request, user=Depends(session), service=Depends(svc)):
        import re
        try:
            offset = int(request.headers["upload-offset"])
            length = int(request.headers["upload-length"])
            digest = request.headers["upload-sha256"]
            rid = request.headers["upload-request-id"]
            if offset < 0 or not re.fullmatch("[a-f0-9]{64}", digest) or not 1 <= len(rid) <= 100:
                raise ValueError()
            if request.headers.get("content-type") != "application/octet-stream":
                raise ValueError()
        except (ValueError, KeyError):
            raise HTTPException(422, "Valid octet-stream and Upload-* headers are required") from None
        return await service.append(uid, user["id"], request, offset, length, digest, rid)

    @app.post("/api/v1/uploads/{uid}/complete", status_code=202)
    async def complete(uid: str, body: CompleteBody, user=Depends(session), service=Depends(svc)):
        async with service.lock(uid):
            result = await service.disk(service.queue_verification, uid, user["id"], body.sha256)
            service.verification_ready.set()
            return result

    @app.post("/api/v1/uploads/{uid}/{action}")
    async def control_file(uid: str, action: Literal["pause", "resume", "cancel"], user=Depends(session), service=Depends(svc)):
        async with service.lock(uid):
            row = await service.disk(service.upload, uid, user["id"])
            if row["state"] in ("completed", "canceled", "skipped", "conflict") or (action != 'cancel' and row['state'] in ('repair_required', 'verifying', 'finalizing')):
                raise HTTPException(409, "This file cannot change state now")
            if row['job_state'] in ('canceled', 'registering', 'completed'):
                raise HTTPException(409, 'This job cannot transfer now')
            state = {"pause": "paused", "resume": "uploading", "cancel": "canceled"}[action]
            await service.disk(service.repo.execute, "UPDATE uploads SET state=?,error=NULL WHERE id=?", (state, uid))
            await service.disk(service.refresh_job, row["job"])
            return await service.disk(service.upload, uid, user["id"])

    @app.delete('/api/v1/jobs/{jid}/incomplete')
    async def discard_incomplete(jid: str, user=Depends(session), service=Depends(svc)):
        return await service.discard_incomplete(jid, user['id'])

    @app.post("/api/v1/jobs/{jid}/{action}")
    async def control_job(jid: str, action: Literal["pause", "resume", "cancel"], user=Depends(session), service=Depends(svc)):
        return await service.control_job(jid, action, user['id'])

    @app.get("/api/v1/shares")
    def shares(after: float = 0, after_id: str = '', limit: int = Query(100, ge=1, le=500), user=Depends(session), service=Depends(svc)):
        return service.list_shares(after, after_id, limit)

    @app.get("/api/v1/shares/{sid}/manifest")
    def share_manifest(sid: str, after: int = 0, limit: int = Query(500, ge=1, le=500), user=Depends(session), service=Depends(svc)):
        service.share(sid)
        job = service.repo.one("SELECT state FROM jobs WHERE id=?", (sid,))
        return {"state": job["state"] if job else "completed", "entries": service.repo.all("SELECT * FROM files WHERE share=? AND seq>? ORDER BY seq LIMIT ?", (sid, after, limit))}

    @app.get("/api/v1/files/{fid}/metadata")
    async def file_metadata(fid: str, user=Depends(session), service=Depends(svc)):
        return await anyio.to_thread.run_sync(service.metadata, fid, limiter=service.metadata_workers)

    @app.get("/api/v1/files/{fid}/content")
    @app.head("/api/v1/files/{fid}/content")
    def content(fid: str, user=Depends(session), service=Depends(svc)):
        row, path = service.file(fid)
        def authorize():
            service.share(row['share'])
            if not service.repo.one('SELECT id FROM files WHERE id=?', (fid,)):
                raise RuntimeError('Share was refreshed during download')
            if not service.repo.one('SELECT id FROM sessions WHERE id=? AND revoked=0 AND created>?', (user['id'], time.time() - config.session_days * 86400)):
                raise RuntimeError('Device was unpaired during download')
        return PinnedFileResponse(path, row['version'], limiter=service.downloads, idle_seconds=config.download_idle_seconds,
                                  authorize=authorize, filename=path.name, media_type='application/octet-stream', headers={'ETag': '"' + row['version'] + '"'})

    if (config.frontend / "assets").exists():
        app.mount("/assets", StaticFiles(directory=config.frontend / "assets"), name="assets")

    @app.get("/")
    def index():
        index_path = config.frontend / "index.html"
        if not index_path.exists():
            return JSONResponse({"detail": "Frontend not built. Run scripts/setup.ps1 first."}, 503)
        return FileResponse(index_path)

    return app
