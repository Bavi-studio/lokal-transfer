from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import ClientDisconnect
from pydantic import BaseModel, ValidationError

from .config import Settings, load_settings
from .storage import Storage

logger = logging.getLogger("local_transfer")
_FILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.bin$")
_MAX_ACTIVE_DOWNLOAD_TICKETS = 1024
_WINDOWS_RESERVED_RE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


class TextInput(BaseModel):
    text: str


def safe_display_name(raw_name: str) -> str:
    """Normalize a client filename for display only; never use it as a path."""
    name = unicodedata.normalize("NFC", raw_name).replace("\\", "/").split("/")[-1]
    # Remove control, format, surrogate, private-use and unassigned code points.
    bidi_controls = {0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF}
    name = "".join(
        ch for ch in name
        if unicodedata.category(ch) not in {"Cc", "Cs", "Co", "Cn"} and ord(ch) not in bidi_controls
    )
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip().rstrip(" .")
    if not name or name in {".", ".."}:
        raise HTTPException(422, detail={"code": "invalid_filename", "message": "Имя файла пустое или недопустимое"})
    if _WINDOWS_RESERVED_RE.fullmatch(name):
        name = f"_{name}"
    name = name[:240].rstrip(" .")
    if not name:
        raise HTTPException(422, detail={"code": "invalid_filename", "message": "Недопустимое имя файла"})
    return name


def _collision_name(name: str, counter: int) -> str:
    path = Path(name)
    suffix = path.suffix
    marker = f" ({counter})"
    max_stem = max(1, 240 - len(marker) - len(suffix))
    stem = path.stem[:max_stem].rstrip(" .") or "file"
    return f"{stem}{marker}{suffix}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    storage = Storage(settings.database_path)
    upload_slots = asyncio.Semaphore(settings.max_concurrent_uploads)
    mutation_lock = asyncio.Lock()
    reservation_lock = asyncio.Lock()
    reserved_bytes = 0
    download_tickets: dict[str, tuple[str, float, str]] = {}
    ticket_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The data root is configured locally and is not served by StaticFiles.
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        for directory in (settings.files_dir, settings.staging_dir):
            if directory.is_symlink():
                raise RuntimeError(f"Storage directory must not be a symlink: {directory.name}")
            directory.mkdir(parents=True, exist_ok=True)
        if settings.files_dir.resolve().parent != settings.data_dir.resolve():
            raise RuntimeError("Files directory escapes the configured data directory")
        if settings.staging_dir.resolve().parent != settings.data_dir.resolve():
            raise RuntimeError("Staging directory escapes the configured data directory")

        if settings.token_generated and not settings.token_file.exists():
            # Persist the random token in a data file, never in application/access logs.
            fd = os.open(settings.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as token_output:
                token_output.write(settings.token + "\n")
            try:
                settings.token_file.chmod(0o600)
            except OSError:
                pass  # Windows ACLs, not POSIX mode bits, govern Windows access.

        storage.initialize()
        # Staging is disposable. Log failures without exposing secrets or file contents.
        for path in settings.staging_dir.iterdir():
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError as exc:
                logger.warning("Could not remove stale staging item (%s)", type(exc).__name__)

        # Reconcile crash leftovers: remove missing-file rows and unregistered generated files.
        known: set[str] = set()
        for item in storage.stored_files():
            stored_name = item["stored_name"]
            if not _STORED_NAME_RE.fullmatch(stored_name) or stored_name != f"{item['id']}.bin":
                logger.error("Invalid stored-name metadata encountered; record will be removed")
                storage.delete_file_record(item["id"])
                continue
            path = settings.files_dir / stored_name
            if path.is_symlink() or not path.is_file() or path.resolve().parent != settings.files_dir.resolve():
                storage.delete_file_record(item["id"])
                continue
            known.add(stored_name)
        for path in settings.files_dir.iterdir():
            if path.name.endswith(".bin") and _STORED_NAME_RE.fullmatch(path.name) and path.name not in known:
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not remove orphan storage item (%s)", type(exc).__name__)

        app.state.settings = settings
        app.state.storage = storage
        yield

    app = FastAPI(title="Local Transfer API", version="1.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        hostname = (request.url.hostname or "").lower().rstrip(".")
        host_allowed = hostname in {"localhost", "localhost.localdomain"} or hostname in settings.allowed_hosts
        if not host_allowed:
            try:
                address = ipaddress.ip_address(hostname)
                host_allowed = address.is_private or address.is_loopback or address.is_link_local
            except ValueError:
                host_allowed = False
        if not host_allowed:
            response = JSONResponse(
                status_code=400,
                content={"error": {"code": "invalid_host", "message": "Используйте локальный IP-адрес сервера"}},
            )
        else:
            response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    def require_token(authorization: str | None = Header(default=None)) -> None:
        expected = f"Bearer {settings.token}"
        if not authorization or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "Требуется действительный токен доступа"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {"code": "http_error", "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content={"error": detail}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": "Параметры запроса имеют неверный формат"}},
        )

    @app.exception_handler(OSError)
    async def os_error_handler(request: Request, exc: OSError):
        logger.warning("Storage operation failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=507, content={"error": {"code": "storage_error", "message": "Ошибка чтения или записи локального хранилища"}})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        logger.error("Unhandled application error (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера"}})

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/files", dependencies=[Depends(require_token)])
    async def list_files():
        return {"items": storage.list_files()}

    async def reserve_upload_bytes(amount: int) -> None:
        nonlocal reserved_bytes
        async with reservation_lock:
            used = storage.total_file_size()
            if used + reserved_bytes + amount > settings.max_storage_bytes:
                raise HTTPException(507, detail={"code": "storage_quota_exceeded", "message": "Недостаточно доступной квоты хранилища"})
            try:
                free = shutil.disk_usage(settings.data_dir).free
            except OSError as exc:
                raise HTTPException(507, detail={"code": "storage_error", "message": "Не удалось проверить свободное место"}) from exc
            if free < amount:
                raise HTTPException(507, detail={"code": "insufficient_disk_space", "message": "На диске недостаточно свободного места"})
            reserved_bytes += amount

    async def release_upload_bytes(amount: int) -> None:
        nonlocal reserved_bytes
        async with reservation_lock:
            reserved_bytes = max(0, reserved_bytes - amount)

    @app.post("/api/files", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_token)])
    async def upload_file(request: Request, filename: str = Query(..., min_length=1, max_length=1024)):
        name = safe_display_name(filename)
        content_length = request.headers.get("content-length")
        declared_size: int | None = None
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, detail={"code": "invalid_content_length", "message": "Некорректный Content-Length"}) from exc
            if declared_size < 0:
                raise HTTPException(400, detail={"code": "invalid_content_length", "message": "Некорректный Content-Length"})
            if declared_size > settings.max_file_size:
                raise HTTPException(413, detail={"code": "file_too_large", "message": "Размер файла превышает установленный лимит"})

        async with upload_slots:
            reservation = declared_size if declared_size is not None else settings.max_file_size
            await reserve_upload_bytes(reservation)
            temp_path: Path | None = None
            published_path: Path | None = None
            size = 0
            try:
                fd, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=settings.staging_dir)
                temp_path = Path(temp_name)
                with os.fdopen(fd, "wb") as output:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > settings.max_file_size:
                            raise HTTPException(413, detail={"code": "file_too_large", "message": "Размер файла превышает установленный лимит"})
                        for offset in range(0, len(chunk), settings.chunk_size):
                            block = chunk[offset : offset + settings.chunk_size]
                            await asyncio.to_thread(output.write, block)
                    await asyncio.to_thread(output.flush)
                    await asyncio.to_thread(os.fsync, output.fileno())

                async with mutation_lock:
                    existing_names = {item["name"].casefold() for item in storage.list_files()}
                    display_name = name
                    counter = 1
                    while display_name.casefold() in existing_names:
                        display_name = _collision_name(name, counter)
                        counter += 1
                    file_id = uuid.uuid4().hex
                    stored_name = f"{file_id}.bin"
                    published_path = settings.files_dir / stored_name
                    # Hard-link publication is atomic and fails rather than overwriting a target.
                    try:
                        os.link(temp_path, published_path)
                    except FileExistsError as exc:
                        raise HTTPException(503, detail={"code": "storage_collision", "message": "Не удалось выделить уникальное имя хранения"}) from exc
                    temp_path.unlink()
                    temp_path = None
                    try:
                        item = storage.insert_file(file_id, display_name, stored_name, size)
                    except Exception:
                        published_path.unlink(missing_ok=True)
                        published_path = None
                        raise
                return item
            except HTTPException:
                raise
            except asyncio.CancelledError:
                raise
            except ClientDisconnect as exc:
                # A dropped upload is not an internal server failure. The client may
                # no longer be connected to receive this response, but cleanup below
                # still removes the unpublished staging file.
                raise HTTPException(400, detail={"code": "upload_interrupted", "message": "Передача файла была прервана"}) from exc
            except OSError as exc:
                raise HTTPException(507, detail={"code": "storage_error", "message": "Не удалось сохранить файл"}) from exc
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove failed upload temporary file (%s)", type(exc).__name__)
                if published_path is not None:
                    # A published path remains only after successful DB registration.
                    try:
                        if storage.get_file(published_path.stem) is None:
                            published_path.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove unregistered upload (%s)", type(exc).__name__)
                await release_upload_bytes(reservation)

    async def open_file_response(file_id: str):
        if not _FILE_ID_RE.fullmatch(file_id):
            raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
        # Open the file while holding the mutation lock, before a concurrent delete can move it.
        async with mutation_lock:
            item = storage.get_file(file_id)
            if item is None:
                raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
            stored_name = item.get("stored_name", "")
            if stored_name != f"{file_id}.bin" or not _STORED_NAME_RE.fullmatch(stored_name):
                raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
            base_dir = settings.files_dir.resolve()
            path = settings.files_dir / stored_name
            if path.is_symlink() or path.resolve().parent != base_dir or not path.is_file():
                raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
            try:
                file_handle = path.open("rb")
            except FileNotFoundError as exc:
                raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"}) from exc

        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", item["name"]).replace('"', "_") or "download"
        disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(item['name'], safe='')}"

        async def stream_file():
            try:
                while True:
                    chunk = await asyncio.to_thread(file_handle.read, settings.chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                file_handle.close()

        return StreamingResponse(
            stream_file(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": disposition, "Content-Length": str(item["size"]), "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/api/files/{file_id}/download-ticket", dependencies=[Depends(require_token)])
    async def create_download_ticket(file_id: str):
        if not _FILE_ID_RE.fullmatch(file_id) or storage.get_file(file_id) is None:
            raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
        now = time.monotonic()
        async with ticket_lock:
            for ticket_id, (_, expires_at, _) in list(download_tickets.items()):
                if expires_at <= now:
                    download_tickets.pop(ticket_id, None)
            if len(download_tickets) >= _MAX_ACTIVE_DOWNLOAD_TICKETS:
                raise HTTPException(429, detail={"code": "too_many_download_tickets", "message": "Слишком много незавершённых скачиваний; повторите позже"})
            ticket_id = uuid.uuid4().hex
            secret = secrets.token_urlsafe(32)
            download_tickets[ticket_id] = (file_id, now + 60, secret)
        response = JSONResponse({"url": f"/api/downloads/{ticket_id}", "expires_in": 60})
        # The URL contains only a public identifier. The actual one-time secret is
        # in a short-lived, HttpOnly, same-site cookie and never in access-log paths.
        response.set_cookie(
            key=f"local_transfer_download_{ticket_id}",
            value=secret,
            max_age=60,
            httponly=True,
            samesite="strict",
            secure=False,  # This service is intentionally HTTP-only on a trusted LAN.
            path=f"/api/downloads/{ticket_id}",
        )
        return response

    @app.get("/api/downloads/{ticket_id}")
    async def download_with_ticket(ticket_id: str, request: Request):
        if not re.fullmatch(r"[0-9a-f]{32}", ticket_id):
            raise HTTPException(404, detail={"code": "download_ticket_invalid", "message": "Ссылка скачивания недействительна или устарела"})
        cookie_name = f"local_transfer_download_{ticket_id}"
        supplied_secret = request.cookies.get(cookie_name, "")
        async with ticket_lock:
            record = download_tickets.get(ticket_id)
            if record is None or record[1] <= time.monotonic() or not supplied_secret or not hmac.compare_digest(supplied_secret.encode("utf-8"), record[2].encode("utf-8")):
                raise HTTPException(404, detail={"code": "download_ticket_invalid", "message": "Ссылка скачивания недействительна или устарела"})
            download_tickets.pop(ticket_id, None)
        response = await open_file_response(record[0])
        response.delete_cookie(cookie_name, path=f"/api/downloads/{ticket_id}", httponly=True, samesite="strict")
        return response

    @app.get("/api/files/{file_id}", dependencies=[Depends(require_token)])
    async def download_file(file_id: str):
        return await open_file_response(file_id)

    @app.delete("/api/files/{file_id}", status_code=204, dependencies=[Depends(require_token)])
    async def delete_file(file_id: str):
        if not _FILE_ID_RE.fullmatch(file_id):
            raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
        async with mutation_lock:
            item = storage.get_file(file_id)
            if item is None:
                raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
            stored_name = item.get("stored_name", "")
            if stored_name != f"{file_id}.bin" or not _STORED_NAME_RE.fullmatch(stored_name):
                raise HTTPException(500, detail={"code": "storage_integrity_error", "message": "Некорректный путь хранения"})
            path = settings.files_dir / stored_name
            if path.is_symlink() or path.resolve().parent != settings.files_dir.resolve():
                raise HTTPException(500, detail={"code": "storage_integrity_error", "message": "Некорректный путь хранения"})
            tombstone = settings.staging_dir / f"delete-{file_id}.part"
            try:
                if path.exists():
                    os.replace(path, tombstone)
                deleted = storage.delete_file_record(file_id)
                if not deleted:
                    if tombstone.exists():
                        os.replace(tombstone, path)
                    raise HTTPException(404, detail={"code": "file_not_found", "message": "Файл не найден"})
            except HTTPException:
                raise
            except OSError as exc:
                if tombstone.exists() and not path.exists():
                    try:
                        os.replace(tombstone, path)
                    except OSError:
                        logger.error("Could not roll back interrupted file deletion")
                raise HTTPException(507, detail={"code": "storage_error", "message": "Не удалось удалить файл"}) from exc
            except Exception:
                if tombstone.exists() and not path.exists():
                    try:
                        os.replace(tombstone, path)
                    except OSError:
                        logger.error("Could not roll back interrupted file deletion")
                raise
            try:
                tombstone.unlink(missing_ok=True)
            except OSError as exc:
                # The logical delete succeeded; startup staging cleanup will retry.
                logger.warning("Could not remove deleted file temporary item (%s)", type(exc).__name__)
        return Response(status_code=204)

    @app.get("/api/text", dependencies=[Depends(require_token)])
    async def list_text(limit: int = Query(default=min(100, settings.text_history_limit), ge=1, le=settings.text_history_limit)):
        return {"items": storage.list_messages(limit)}

    @app.post("/api/text", status_code=201, dependencies=[Depends(require_token)])
    async def create_text(request: Request):
        # Read and bound the raw body before JSON parsing to avoid unbounded buffering.
        max_json_bytes = settings.max_text_size * 6 + 4096  # worst-case JSON \u escapes + envelope
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_body_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(400, detail={"code": "invalid_content_length", "message": "Некорректный Content-Length"}) from exc
            if declared_body_size < 0:
                raise HTTPException(400, detail={"code": "invalid_content_length", "message": "Некорректный Content-Length"})
            if declared_body_size > max_json_bytes:
                raise HTTPException(413, detail={"code": "text_too_large", "message": "Текстовый запрос превышает установленный лимит"})
        parts: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_json_bytes:
                raise HTTPException(413, detail={"code": "text_too_large", "message": "Текстовый запрос превышает установленный лимит"})
            parts.append(chunk)
        try:
            raw = json.loads(b"".join(parts).decode("utf-8"))
            payload = TextInput.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(422, detail={"code": "invalid_text_payload", "message": "Ожидался JSON с текстовым полем text"}) from exc
        if len(payload.text.encode("utf-8")) > settings.max_text_size:
            raise HTTPException(413, detail={"code": "text_too_large", "message": "Текст превышает установленный лимит"})
        return storage.add_message(payload.text, settings.text_history_limit)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
