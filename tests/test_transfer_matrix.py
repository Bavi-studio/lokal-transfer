"""API-level transfer regression tests; intentionally independent of the web UI."""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def upload(client: TestClient, auth: dict[str, str], name: str, data: bytes, **headers):
    return client.post(
        "/api/files",
        params={"filename": name},
        content=data,
        headers={**auth, "Content-Type": "application/octet-stream", **headers},
    )


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("empty.bin", b""),
        ("notes.txt", "Строка 1\r\nСтрока 2\nПривет 🌍".encode("utf-8")),
        ("binary.bin", bytes(range(256)) * 32 + b"\x00\xff\x80"),
        ("файл_日本語_🙂.txt", "Unicode имя\nсодержимое".encode("utf-8")),
    ],
)
def test_upload_download_round_trip_preserves_exact_bytes(settings, auth, name, payload):
    configured = replace(settings, max_file_size=16 * 1024)
    with TestClient(create_app(configured)) as client:
        created = upload(client, auth, name, payload)
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["size"] == len(payload)
        downloaded = client.get(f"/api/files/{item['id']}", headers=auth)
        assert downloaded.status_code == 200
        assert downloaded.content == payload
        assert sha256(downloaded.content) == sha256(payload)
        assert int(downloaded.headers["content-length"]) == len(payload)


@pytest.mark.parametrize("size", [1, 127, 1024, 64 * 1024, 1024 * 1024])
def test_multiple_file_sizes_round_trip(settings, auth, size):
    # Deterministic payload avoids relying on random test data and supports exact hashes.
    configured = replace(settings, max_file_size=2 * 1024 * 1024, chunk_size=16 * 1024)
    payload = bytes((i * 31 + 7) % 256 for i in range(size))
    with TestClient(create_app(configured)) as client:
        created = upload(client, auth, f"size-{size}.bin", payload)
        assert created.status_code == 201, created.text
        got = client.get(f"/api/files/{created.json()['id']}", headers=auth)
        assert got.status_code == 200
        assert sha256(got.content) == sha256(payload)


def test_large_file_is_written_in_bounded_blocks(settings, auth, monkeypatch):
    """Checks server write block size; does not claim to measure whole-process RSS."""
    import app.main as main_module

    configured = replace(settings, max_file_size=16 * 1024 * 1024, chunk_size=64 * 1024)
    payload = bytes((i * 13) % 256 for i in range(8 * 1024 * 1024))
    observed_write_sizes: list[int] = []
    original_to_thread = main_module.asyncio.to_thread

    async def observing_to_thread(func, /, *args, **kwargs):
        if getattr(func, "__name__", "") == "write" and args and isinstance(args[0], (bytes, bytearray, memoryview)):
            observed_write_sizes.append(len(args[0]))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(main_module.asyncio, "to_thread", observing_to_thread)
    with TestClient(create_app(configured)) as client:
        created = upload(client, auth, "large.bin", payload)
        assert created.status_code == 201, created.text
        downloaded = client.get(f"/api/files/{created.json()['id']}", headers=auth)
        assert downloaded.status_code == 200
        assert sha256(downloaded.content) == sha256(payload)
    assert observed_write_sizes
    assert max(observed_write_sizes) <= configured.chunk_size
    assert sum(observed_write_sizes) == len(payload)


def test_duplicate_names_get_distinct_names_and_contents(client, auth):
    payloads = [b"first", b"second", b"third"]
    created = [upload(client, auth, "same.txt", body) for body in payloads]
    assert all(response.status_code == 201 for response in created)
    items = [response.json() for response in created]
    assert [item["name"] for item in items] == ["same.txt", "same (1).txt", "same (2).txt"]
    assert len({item["id"] for item in items}) == 3
    for item, expected in zip(items, payloads, strict=True):
        response = client.get(f"/api/files/{item['id']}", headers=auth)
        assert response.content == expected


def test_concurrent_uploads_preserve_every_payload(settings, auth):
    configured = replace(settings, max_file_size=16 * 1024, max_concurrent_uploads=8)
    payloads = [bytes([n]) * (1000 + n * 17) for n in range(8)]
    with TestClient(create_app(configured)) as client:
        def send(index):
            return upload(client, auth, "parallel.bin", payloads[index])

        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(send, range(len(payloads))))
        assert all(response.status_code == 201 for response in responses), [r.text for r in responses]
        items = [response.json() for response in responses]
        assert len({item["name"].casefold() for item in items}) == len(payloads)
        downloaded_hashes = set()
        for item in items:
            response = client.get(f"/api/files/{item['id']}", headers=auth)
            assert response.status_code == 200
            downloaded_hashes.add(sha256(response.content))
        assert downloaded_hashes == {sha256(payload) for payload in payloads}


def test_invalid_upload_parameters_are_rejected(client, auth):
    assert client.post("/api/files", content=b"x", headers=auth).status_code == 422
    assert client.post("/api/files?filename=", content=b"x", headers=auth).status_code == 422
    assert client.post("/api/files?filename=" + "a" * 1100, content=b"x", headers=auth).status_code == 422
    assert client.post("/api/files?filename=%00", content=b"x", headers=auth).status_code == 422


@pytest.mark.parametrize("filename", [".", "..", "%00", "%0Aevil.txt", "%0Devil.txt", "%5C..%5Coutside.txt"])
def test_path_like_or_control_names_never_escape_storage(client, auth, filename):
    response = client.post(f"/api/files?filename={filename}", content=b"payload", headers=auth)
    if response.status_code == 201:
        item = response.json()
        assert "/" not in item["name"] and "\\" not in item["name"]
        assert "\x00" not in item["name"] and "\r" not in item["name"] and "\n" not in item["name"]
        path = client.app.state.settings.files_dir / f"{item['id']}.bin"
        assert path.resolve().parent == client.app.state.settings.files_dir.resolve()
    else:
        assert response.status_code in (400, 422)


def test_size_limit_rejects_upload_without_publishing(settings, auth):
    configured = replace(settings, max_file_size=100)
    with TestClient(create_app(configured)) as client:
        response = upload(client, auth, "too-large.bin", b"x" * 101)
        assert response.status_code == 413
        assert client.get("/api/files", headers=auth).json()["items"] == []
        assert list(configured.staging_dir.iterdir()) == []


def test_invalid_json_text_request_is_structured(client, auth):
    response = client.post("/api/text", content=b"{broken", headers={**auth, "Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_text_payload"


def test_text_api_rejects_wrong_types_and_invalid_limit(client, auth):
    assert client.post("/api/text", json={"text": 123}, headers=auth).status_code == 422
    assert client.post("/api/text", json={}, headers=auth).status_code == 422
    assert client.get("/api/text?limit=0", headers=auth).status_code == 422
    assert client.get("/api/text?limit=not-a-number", headers=auth).status_code == 422


def test_delete_then_get_and_delete_again(client, auth):
    created = upload(client, auth, "delete-me.bin", b"delete me")
    file_id = created.json()["id"]
    assert client.delete(f"/api/files/{file_id}", headers=auth).status_code == 204
    assert client.get(f"/api/files/{file_id}", headers=auth).status_code == 404
    assert client.delete(f"/api/files/{file_id}", headers=auth).status_code == 404


def test_failed_write_does_not_publish_and_next_upload_recovers(client, auth, monkeypatch):
    import app.main as main_module

    original_link = main_module.os.link
    calls = 0

    def fail_once(source, target, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated disk failure")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(main_module.os, "link", fail_once)
    failed = upload(client, auth, "failure.bin", b"first")
    assert failed.status_code == 507
    assert list(client.app.state.settings.staging_dir.iterdir()) == []
    succeeded = upload(client, auth, "recovery.bin", b"second")
    assert succeeded.status_code == 201
    assert client.get(f"/api/files/{succeeded.json()['id']}", headers=auth).content == b"second"


def test_insufficient_disk_space_is_reported_before_upload(client, auth, monkeypatch):
    import app.main as main_module

    class Usage:
        total = 100
        used = 99
        free = 0

    monkeypatch.setattr(main_module.shutil, "disk_usage", lambda _path: Usage())
    response = upload(client, auth, "no-space.bin", b"x")
    assert response.status_code == 507
    assert response.json()["error"]["code"] == "insufficient_disk_space"
    assert client.app.state.storage.list_files() == []


def test_delete_does_not_remove_file_outside_storage_when_metadata_is_tampered(client, auth, tmp_path):
    created = upload(client, auth, "safe.bin", b"safe")
    file_id = created.json()["id"]
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must survive")
    with client.app.state.storage._connect() as db:
        db.execute("UPDATE files SET stored_name = ? WHERE id = ?", (str(outside), file_id))
    response = client.delete(f"/api/files/{file_id}", headers=auth)
    assert response.status_code >= 400
    assert outside.read_bytes() == b"must survive"


def test_download_ticket_fails_after_file_is_deleted(client, auth):
    created = upload(client, auth, "ticket.bin", b"content")
    file_id = created.json()["id"]
    ticket = client.post(f"/api/files/{file_id}/download-ticket", headers=auth)
    assert ticket.status_code == 200
    assert client.delete(f"/api/files/{file_id}", headers=auth).status_code == 204
    download = client.get(ticket.json()["url"])
    assert download.status_code == 404


def test_storage_reconciliation_removes_orphaned_generated_file(settings, auth):
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    orphan = settings.files_dir / ("a" * 32 + ".bin")
    orphan.write_bytes(b"orphan")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
    assert not orphan.exists()


def test_content_integrity_survives_failed_then_successful_transfer(settings, auth, monkeypatch):
    import app.main as main_module

    configured = replace(settings, max_file_size=128 * 1024)
    payload = bytes(range(256)) * 128
    original_link = main_module.os.link
    with TestClient(create_app(configured)) as client:
        monkeypatch.setattr(main_module.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("temporary error")))
        failed = upload(client, auth, "retry.bin", payload)
        assert failed.status_code == 507
        monkeypatch.setattr(main_module.os, "link", original_link)
        successful = upload(client, auth, "retry.bin", payload)
        assert successful.status_code == 201
        downloaded = client.get(f"/api/files/{successful.json()['id']}", headers=auth)
        assert sha256(downloaded.content) == sha256(payload)


def test_interrupted_stream_leaves_no_partial_file_and_service_recovers(settings, auth):
    """Simulates a request-body transport failure after the first chunk."""
    import httpx
    from starlette.requests import ClientDisconnect

    configured = replace(settings, max_file_size=4096, chunk_size=8)

    async def scenario():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                async def interrupted_body():
                    yield b"partial-data"
                    raise ClientDisconnect()

                interrupted = await client.post(
                    "/api/files?filename=interrupted.bin",
                    content=interrupted_body(),
                    headers={**auth, "Content-Type": "application/octet-stream"},
                )
                assert interrupted.status_code == 400
                assert interrupted.json()["error"]["code"] == "upload_interrupted"
                assert (await client.get("/api/files", headers=auth)).status_code == 200  # app remains responsive
                assert list(configured.staging_dir.iterdir()) == []
                assert list(configured.files_dir.iterdir()) == []

                recovered = await client.post(
                    "/api/files?filename=recovered.bin",
                    content=b"complete-data",
                    headers={**auth, "Content-Type": "application/octet-stream"},
                )
                assert recovered.status_code == 201
                downloaded = await client.get(f"/api/files/{recovered.json()['id']}", headers=auth)
                assert downloaded.content == b"complete-data"

    asyncio.run(scenario())


def test_upload_stream_generator_does_not_require_full_payload_buffer(settings, auth, monkeypatch):
    """A streamed body is accepted without Content-Length and has exact integrity."""
    import httpx

    configured = replace(settings, max_file_size=2 * 1024 * 1024, chunk_size=8192)
    expected = hashlib.sha256()
    total = 512 * 1024

    async def scenario():
        app = create_app(configured)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                async def body():
                    for index in range(64):
                        chunk = bytes([index]) * 8192
                        expected.update(chunk)
                        yield chunk

                response = await client.post(
                    "/api/files?filename=streamed.bin",
                    content=body(),
                    headers={**auth, "Content-Type": "application/octet-stream"},
                )
                assert response.status_code == 201, response.text
                downloaded = await client.get(f"/api/files/{response.json()['id']}", headers=auth)
                assert hashlib.sha256(downloaded.content).hexdigest() == expected.hexdigest()
                assert len(downloaded.content) == total

    asyncio.run(scenario())
