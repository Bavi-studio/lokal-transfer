"""Optional real-Uvicorn integration test for server-process RSS during streamed uploads."""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

try:
    import psutil
except ImportError:  # psutil is intentionally optional for the regular test suite
    psutil = None


@pytest.mark.skipif(psutil is None, reason="install psutil to measure isolated server-process RSS")
def test_real_uvicorn_large_stream_integrity_and_bounded_rss(tmp_path):
    """Uploads/downloads 24 MiB over real loopback TCP; samples only server RSS."""
    token = "test-token-for-memory-integration-123456789"
    data_dir = tmp_path / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env.update(
        {
            "LOCAL_TRANSFER_DATA_DIR": str(data_dir),
            "LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR": "1",
            "LOCAL_TRANSFER_TOKEN": token,
            "LOCAL_TRANSFER_MAX_FILE_SIZE": str(64 * 1024 * 1024),
            "LOCAL_TRANSFER_MAX_STORAGE_BYTES": str(96 * 1024 * 1024),
            "LOCAL_TRANSFER_CHUNK_SIZE": str(64 * 1024),
            "LOCAL_TRANSFER_MAX_CONCURRENT_UPLOADS": "1",
        }
    )
    project_root = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "error"],
        cwd=project_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    process_handle = psutil.Process(process.pid)
    stop_sampling = threading.Event()
    rss_samples: list[int] = []
    sampler = None
    try:
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        with httpx.Client(timeout=60) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stderr = process.stderr.read() if process.stderr else ""
                    pytest.fail(f"Uvicorn exited before startup: {stderr}")
                try:
                    if client.get(f"{base_url}/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                pytest.fail("Uvicorn did not become ready within 15 seconds")

            baseline = process_handle.memory_info().rss

            def sample_rss():
                while not stop_sampling.is_set():
                    try:
                        rss_samples.append(process_handle.memory_info().rss)
                    except psutil.Error:
                        return
                    time.sleep(0.005)

            sampler = threading.Thread(target=sample_rss, daemon=True)
            sampler.start()
            expected_hash = hashlib.sha256()
            total_bytes = 24 * 1024 * 1024
            chunk_size = 64 * 1024

            def body():
                remaining = total_bytes
                counter = 0
                while remaining:
                    size = min(chunk_size, remaining)
                    chunk = bytes([counter % 251]) * size
                    expected_hash.update(chunk)
                    yield chunk
                    remaining -= size
                    counter += 1
                    time.sleep(0.002)

            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
            uploaded = client.post(f"{base_url}/api/files?filename=large-memory-test.bin", content=body(), headers=headers)
            assert uploaded.status_code == 201, uploaded.text
            item = uploaded.json()
            actual_hash = hashlib.sha256()
            actual_size = 0
            with client.stream("GET", f"{base_url}/api/files/{item['id']}", headers=headers) as response:
                assert response.status_code == 200
                for chunk in response.iter_bytes(64 * 1024):
                    actual_hash.update(chunk)
                    actual_size += len(chunk)
            assert actual_size == total_bytes
            assert actual_hash.hexdigest() == expected_hash.hexdigest()

            stop_sampling.set()
            sampler.join(timeout=2)
            peak_rss = max(rss_samples, default=baseline)
            rss_growth = peak_rss - baseline
            print(
                f"server RSS baseline={baseline / (1024 * 1024):.1f} MiB, "
                f"peak={peak_rss / (1024 * 1024):.1f} MiB, "
                f"growth={rss_growth / (1024 * 1024):.1f} MiB; payload=24.0 MiB"
            )
            # A whole 24 MiB payload buffered in server memory would exceed this allowance.
            assert rss_growth < 20 * 1024 * 1024, (
                f"server RSS grew by {rss_growth / (1024 * 1024):.1f} MiB; "
                "expected bounded streaming memory"
            )
    finally:
        stop_sampling.set()
        if sampler is not None:
            sampler.join(timeout=2)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
