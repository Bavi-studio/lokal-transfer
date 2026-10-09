from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token: str
    token_generated: bool = False
    max_file_size: int = 2 * 1024 * 1024 * 1024
    max_text_size: int = 1024 * 1024
    max_concurrent_uploads: int = 2
    max_storage_bytes: int = 10 * 1024 * 1024 * 1024
    text_history_limit: int = 500
    chunk_size: int = 1024 * 1024
    allowed_hosts: tuple[str, ...] = ()

    @property
    def token_file(self) -> Path:
        return self.data_dir / ".access-token"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "metadata.sqlite3"


def load_settings() -> Settings:
    # Keep configurable storage under the project root. This prevents an accidental
    # environment-variable typo from selecting a drive root or arbitrary user folder.
    project_root = Path(__file__).resolve().parent.parent
    raw_value = os.environ.get("LOCAL_TRANSFER_DATA_DIR", "data").strip()
    # On Windows, reject drive-qualified and UNC paths unless the operator opts in.
    raw_data_dir = Path(raw_value).expanduser()
    allow_external = os.environ.get("LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR", "").strip() == "1"
    if raw_data_dir.is_absolute() or ".." in raw_data_dir.parts or (len(raw_value) >= 2 and raw_value[1] == ":") or raw_value.startswith(("\\\\", "//")):
        if not allow_external:
            raise ValueError("External/absolute LOCAL_TRANSFER_DATA_DIR requires LOCAL_TRANSFER_ALLOW_EXTERNAL_DATA_DIR=1")
        data_dir = raw_data_dir.resolve()
    else:
        # Treat Windows separators consistently if configuration is inspected on another OS.
        if os.name != "nt":
            raw_data_dir = Path(raw_value.replace("\\", os.sep))
        data_dir = (project_root / raw_data_dir).resolve()
        try:
            data_dir.relative_to(project_root)
        except ValueError as exc:
            raise ValueError("LOCAL_TRANSFER_DATA_DIR must stay inside the project directory") from exc
    token = os.environ.get("LOCAL_TRANSFER_TOKEN", "").strip()
    token_generated = False
    if not token:
        token_generated = True
        token_file = data_dir / ".access-token"
        if token_file.is_symlink():
            raise ValueError("Access token file must not be a symlink")
        try:
            token = token_file.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
    if len(token) < 32 or not token.isascii():
        raise ValueError("Access token must contain at least 32 ASCII characters")
    return Settings(
        data_dir=data_dir,
        token=token,
        token_generated=token_generated,
        max_file_size=_positive_int("LOCAL_TRANSFER_MAX_FILE_SIZE", 2 * 1024 * 1024 * 1024),
        max_text_size=_positive_int("LOCAL_TRANSFER_MAX_TEXT_SIZE", 1024 * 1024),
        max_concurrent_uploads=_positive_int("LOCAL_TRANSFER_MAX_CONCURRENT_UPLOADS", 2),
        max_storage_bytes=_positive_int("LOCAL_TRANSFER_MAX_STORAGE_BYTES", 10 * 1024 * 1024 * 1024),
        text_history_limit=_positive_int("LOCAL_TRANSFER_TEXT_HISTORY_LIMIT", 500),
        chunk_size=_positive_int("LOCAL_TRANSFER_CHUNK_SIZE", 1024 * 1024),
        allowed_hosts=tuple(host.strip().lower() for host in os.environ.get("LOCAL_TRANSFER_ALLOWED_HOSTS", "").split(",") if host.strip()),
    )


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value
