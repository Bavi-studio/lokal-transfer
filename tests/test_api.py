def test_health_is_public(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_requires_token(client):
    assert client.get("/api/files").status_code == 401
    assert client.get("/api/text").status_code == 401


def test_upload_download_list_and_delete(client, auth):
    body = b"hello\x00world"
    upload = client.post("/api/files?filename=hello.txt", content=body, headers={**auth, "Content-Type": "application/octet-stream"})
    assert upload.status_code == 201, upload.text
    item = upload.json()
    assert item["size"] == len(body)

    listing = client.get("/api/files", headers=auth)
    assert listing.status_code == 200
    assert listing.json()["items"][0]["id"] == item["id"]

    download = client.get(f"/api/files/{item['id']}", headers=auth)
    assert download.status_code == 200
    assert download.content == body

    deleted = client.delete(f"/api/files/{item['id']}", headers=auth)
    assert deleted.status_code == 204
    assert client.get(f"/api/files/{item['id']}", headers=auth).status_code == 404


def test_empty_file_and_unicode_duplicate_names(client, auth):
    first = client.post("/api/files?filename=%D1%84%D0%B0%D0%B9%D0%BB.txt", content=b"", headers=auth)
    second = client.post("/api/files?filename=%D1%84%D0%B0%D0%B9%D0%BB.txt", content=b"x", headers=auth)
    assert first.status_code == second.status_code == 201
    assert first.json()["size"] == 0
    assert first.json()["name"] == "файл.txt"
    assert second.json()["name"] == "файл (1).txt"


def test_upload_rejects_too_large_content_length(client, auth):
    response = client.post("/api/files?filename=big.bin", content=b"x" * 1025, headers={**auth, "Content-Length": "1025"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert client.get("/api/files", headers=auth).json()["items"] == []


def test_upload_rejects_streamed_overflow_without_content_length(client, auth):
    response = client.post("/api/files?filename=big.bin", content=b"x" * 1025, headers={**auth, "Transfer-Encoding": "chunked"})
    assert response.status_code == 413
    assert client.get("/api/files", headers=auth).json()["items"] == []


def test_path_like_filename_is_reduced_to_display_basename(client, auth):
    response = client.post("/api/files?filename=..%2F..%2Fsecret.txt", content=b"x", headers=auth)
    assert response.status_code == 201
    assert response.json()["name"] == "secret.txt"


def test_text_round_trip_and_limit(client, auth):
    created = client.post("/api/text", json={"text": "Привет 🌍"}, headers=auth)
    assert created.status_code == 201
    assert created.json()["text"] == "Привет 🌍"
    listed = client.get("/api/text", headers=auth)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["text"] == "Привет 🌍"
    too_long = client.post("/api/text", json={"text": "я" * 65}, headers=auth)
    assert too_long.status_code in (413, 422)


def test_missing_file_returns_structured_404(client, auth):
    response = client.get("/api/files/00000000000000000000000000000000", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "file_not_found"


def test_frontend_is_served_locally(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Локальный обменник" in response.text
    assert "cdn" not in response.text.lower()


def test_download_ticket_is_short_lived_and_single_use(client, auth):
    upload = client.post("/api/files?filename=%D1%84%D0%B0%D0%B9%D0%BB.txt", content=b"download", headers=auth)
    assert upload.status_code == 201
    file_id = upload.json()["id"]
    ticket_response = client.post(f"/api/files/{file_id}/download-ticket", headers=auth)
    assert ticket_response.status_code == 200
    ticket_url = ticket_response.json()["url"]
    download = client.get(ticket_url)
    assert download.status_code == 200
    assert download.content == b"download"
    assert "filename*=UTF-8''%D1%84%D0%B0%D0%B9%D0%BB.txt" in download.headers["content-disposition"]
    assert client.get(ticket_url).status_code == 404


def test_download_ticket_requires_authorization(client, auth):
    upload = client.post("/api/files?filename=a.txt", content=b"x", headers=auth)
    file_id = upload.json()["id"]
    assert client.post(f"/api/files/{file_id}/download-ticket").status_code == 401


def test_frontend_assets_are_served_locally(client):
    css = client.get("/styles.css")
    js = client.get("/app.js")
    assert css.status_code == 200 and "@media" in css.text
    assert js.status_code == 200 and "XMLHttpRequest" in js.text
    assert "https://" not in css.text
    assert "https://" not in js.text


def test_filename_normalization_removes_controls_and_windows_reserved_names(client, auth):
    response = client.post(
        "/api/files?filename=CON.txt%0D%0AInjected%00.txt",
        content=b"x",
        headers=auth,
    )
    assert response.status_code == 201
    name = response.json()["name"]
    assert "\r" not in name and "\n" not in name and "\x00" not in name
    assert not name.startswith("CON.")
    assert len(name) <= 240


def test_storage_metadata_cannot_escape_files_directory(client, auth, tmp_path):
    upload = client.post("/api/files?filename=safe.txt", content=b"safe", headers=auth)
    assert upload.status_code == 201
    file_id = upload.json()["id"]
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must remain untouched")

    with client.app.state.storage._connect() as db:
        db.execute("UPDATE files SET stored_name = ? WHERE id = ?", ("../outside.bin", file_id))

    download = client.get(f"/api/files/{file_id}", headers=auth)
    assert download.status_code == 404
    deletion = client.delete(f"/api/files/{file_id}", headers=auth)
    assert deletion.status_code == 500
    assert outside.read_bytes() == b"must remain untouched"


def test_concurrent_duplicate_uploads_never_overwrite(client, auth):
    from concurrent.futures import ThreadPoolExecutor

    def upload(index):
        return client.post(
            "/api/files?filename=duplicate.txt",
            content=f"payload-{index}".encode(),
            headers=auth,
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(pool.map(upload, range(5)))
    assert all(response.status_code == 201 for response in responses)
    names = [response.json()["name"] for response in responses]
    assert len(set(names)) == 5
    contents = []
    for response in responses:
        file_id = response.json()["id"]
        downloaded = client.get(f"/api/files/{file_id}", headers=auth)
        assert downloaded.status_code == 200
        contents.append(downloaded.content)
    assert set(contents) == {f"payload-{i}".encode() for i in range(5)}


def test_storage_quota_rejects_upload_before_publishing(settings, auth):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from app.main import create_app

    limited = replace(settings, max_storage_bytes=3)
    with TestClient(create_app(limited)) as limited_client:
        first = limited_client.post("/api/files?filename=one.bin", content=b"123", headers=auth)
        second = limited_client.post("/api/files?filename=two.bin", content=b"4", headers=auth)
        assert first.status_code == 201
        assert second.status_code == 507
        assert second.json()["error"]["code"] == "storage_quota_exceeded"
        assert [item["name"] for item in limited_client.get("/api/files", headers=auth).json()["items"]] == ["one.bin"]


def test_disk_write_failure_does_not_publish_file(client, auth, monkeypatch):
    import errno
    import app.main as main_module

    def fail_link(*args, **kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(main_module.os, "link", fail_link)
    response = client.post("/api/files?filename=full.bin", content=b"payload", headers=auth)
    assert response.status_code == 507
    assert client.get("/api/files", headers=auth).json()["items"] == []
    assert list(client.app.state.settings.staging_dir.iterdir()) == []


def test_text_request_body_is_bounded_before_json_parsing(client, auth):
    response = client.post(
        "/api/text",
        content=b" " * 5000,
        headers={**auth, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "text_too_large"


def test_generated_token_is_persisted_but_not_logged(tmp_path, monkeypatch, caplog):
    from app.config import load_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.delenv("LOCAL_TRANSFER_TOKEN", raising=False)
    monkeypatch.setenv("LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR", "1")
    monkeypatch.setenv("LOCAL_TRANSFER_DATA_DIR", str(tmp_path / "private-data"))
    settings = load_settings()
    assert len(settings.token) >= 32
    assert not settings.token_file.exists()
    with TestClient(create_app(settings)):
        assert settings.token_file.read_text(encoding="ascii").strip() == settings.token
    reloaded = load_settings()
    assert reloaded.token == settings.token
    assert settings.token not in caplog.text


def test_download_ticket_secret_is_cookie_not_url_and_is_single_use(client, auth):
    upload = client.post("/api/files?filename=secret.txt", content=b"data", headers=auth)
    file_id = upload.json()["id"]
    response = client.post(f"/api/files/{file_id}/download-ticket", headers=auth)
    ticket = response.json()
    assert ticket["url"].startswith("/api/downloads/")
    ticket_id = ticket["url"].rsplit("/", 1)[-1]
    assert len(ticket_id) == 32
    cookie_name = f"local_transfer_download_{ticket_id}"
    assert cookie_name in response.headers["set-cookie"]
    cookie_value = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    assert cookie_value != ticket_id
    first = client.get(ticket["url"])
    assert first.status_code == 200 and first.content == b"data"
    assert client.get(ticket["url"]).status_code == 404


def test_download_has_security_headers(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_host_header_rejects_nonlocal_domains(client):
    response = client.get("/", headers={"Host": "attacker.example"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"


def test_private_ip_host_is_allowed(client):
    response = client.get("/api/health", headers={"Host": "192.168.1.25:8000"})
    assert response.status_code == 200


def test_startup_removes_stale_staging_files(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app

    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    stale = settings.staging_dir / "upload-interrupted.part"
    stale.write_bytes(b"partial")
    with TestClient(create_app(settings)) as local_client:
        assert local_client.get("/api/health").status_code == 200
    assert not stale.exists()


def test_download_and_delete_use_only_server_generated_storage_names(client, auth, tmp_path):
    upload = client.post("/api/files?filename=normal.txt", content=b"content", headers=auth)
    file_id = upload.json()["id"]
    settings = client.app.state.settings
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not a transfer file")
    # Even a malicious stored path in metadata must not make delete touch outside files.
    with client.app.state.storage._connect() as db:
        db.execute("UPDATE files SET stored_name = ? WHERE id = ?", (str(outside), file_id))
    response = client.delete(f"/api/files/{file_id}", headers=auth)
    assert response.status_code == 500
    assert outside.read_bytes() == b"not a transfer file"


def test_api_documentation_endpoints_are_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_access_token_file_is_not_served(client):
    token_file = client.app.state.settings.token_file
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("do-not-serve-this-secret", encoding="ascii")
    response = client.get("/.access-token")
    assert response.status_code == 404
    assert "do-not-serve-this-secret" not in response.text


def test_short_configured_token_is_rejected(monkeypatch, tmp_path):
    from app.config import load_settings
    import pytest

    monkeypatch.setenv("LOCAL_TRANSFER_TOKEN", "short")
    monkeypatch.setenv("LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR", "1")
    monkeypatch.setenv("LOCAL_TRANSFER_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(ValueError, match="at least 32 ASCII characters"):
        load_settings()


def test_unicode_joiner_is_preserved_in_filename(client, auth):
    name = "family-👨‍👩‍👧‍👦.txt"
    from urllib.parse import quote
    response = client.post(f"/api/files?filename={quote(name)}", content=b"x", headers=auth)
    assert response.status_code == 201
    assert response.json()["name"] == name


def test_active_download_tickets_are_bounded(client, auth, monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(main_module, "_MAX_ACTIVE_DOWNLOAD_TICKETS", 1)
    first_file = client.post("/api/files?filename=one.txt", content=b"1", headers=auth).json()
    second_file = client.post("/api/files?filename=two.txt", content=b"2", headers=auth).json()
    first_ticket = client.post(f"/api/files/{first_file['id']}/download-ticket", headers=auth)
    second_ticket = client.post(f"/api/files/{second_file['id']}/download-ticket", headers=auth)
    assert first_ticket.status_code == 200
    assert second_ticket.status_code == 429
    assert second_ticket.json()["error"]["code"] == "too_many_download_tickets"


def test_symlink_in_storage_is_never_served_or_deleted(client, auth, tmp_path):
    upload = client.post("/api/files?filename=linked.txt", content=b"stored", headers=auth)
    file_id = upload.json()["id"]
    settings = client.app.state.settings
    stored_path = settings.files_dir / f"{file_id}.bin"
    outside = tmp_path / "outside-secret.bin"
    outside.write_bytes(b"outside-secret")
    stored_path.unlink()
    try:
        stored_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        # Symlink creation may be unavailable on a restricted Windows account.
        pytest.skip("Symlink creation is not permitted in this environment")
    response = client.get(f"/api/files/{file_id}", headers=auth)
    assert response.status_code == 404
    assert outside.read_bytes() == b"outside-secret"
    deletion = client.delete(f"/api/files/{file_id}", headers=auth)
    assert deletion.status_code == 500
    assert outside.read_bytes() == b"outside-secret"
