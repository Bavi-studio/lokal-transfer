from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PORT = 8000


def discover_ipv4() -> list[tuple[str, str]]:
    """Return private IPv4 addresses and interface aliases reported by Windows."""
    if os.name != "nt":
        return []
    script = r"""
$ErrorActionPreference = 'Stop'
$items = Get-NetIPConfiguration | Where-Object {
    $_.IPv4Address -and $_.NetAdapter.Status -eq 'Up'
} | ForEach-Object {
    $hasGateway = [bool]$_.IPv4DefaultGateway
    foreach ($ip in $_.IPv4Address) {
        if ($ip.IPAddress -notlike '127.*' -and $ip.IPAddress -notlike '169.254.*') {
            [PSCustomObject]@{ Interface = $_.InterfaceAlias; Address = $ip.IPAddress; HasGateway = $hasGateway }
        }
    }
}
$items | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=ROOT, capture_output=True, text=True, timeout=8, check=True,
        )
        import json
        payload = json.loads(result.stdout.strip() or "[]")
        if isinstance(payload, dict):
            payload = [payload]
        candidates = [(str(x.get("Address", "")), str(x.get("Interface", "")), bool(x.get("HasGateway"))) for x in payload]
        # Prefer an interface with a default gateway, but display all viable candidates.
        candidates.sort(key=lambda item: not item[2])
        return [(address, interface) for address, interface, _ in candidates if address]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось определить IPv4-интерфейсы: {type(exc).__name__}.")
        return []


def port_is_busy(host: str, port: int) -> bool:
    # A bind probe is authoritative for the chosen address; SO_REUSEADDR is not enabled.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
        return False
    except OSError:
        return True


def main() -> int:
    os.chdir(ROOT)
    missing = [name for name in ("fastapi", "uvicorn") if importlib.util.find_spec(name) is None]
    if missing:
        print("[ОШИБКА] Не установлены зависимости: " + ", ".join(missing))
        print("Выполни: .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 2

    host = os.environ.get("LOCAL_TRANSFER_HOST", "").strip()
    port_raw = os.environ.get("LOCAL_TRANSFER_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(port_raw)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print("[ОШИБКА] LOCAL_TRANSFER_PORT должен быть числом от 1 до 65535.")
        return 2

    candidates = discover_ipv4()
    if not host:
        # Never bind to 0.0.0.0 implicitly. If detection is ambiguous, require explicit choice.
        unique = list(dict.fromkeys(address for address, _ in candidates))
        if len(unique) == 1:
            host = unique[0]
        elif len(unique) > 1:
            print("Обнаружено несколько IPv4-адресов:")
            for address, interface in candidates:
                print(f"  {address} — {interface}")
            print("[ДИАГНОСТИКА] Нельзя достоверно выбрать интерфейс для iPhone.")
            print("Укажи LOCAL_TRANSFER_HOST перед запуском, например адрес интерфейса точки доступа.")
            return 3
        else:
            host = "127.0.0.1"
            print("[ДИАГНОСТИКА] Подходящий IPv4-адрес не определён. Запуск только для локальной проверки.")
            print("Для подключения iPhone задай LOCAL_TRANSFER_HOST равным IPv4 Windows из ipconfig.")

    if port_is_busy(host, port):
        print(f"[ОШИБКА] Адрес {host}:{port} уже занят или недоступен для прослушивания.")
        print("Останови другой сервер или задай другой LOCAL_TRANSFER_PORT. Не завершай неизвестный процесс принудительно.")
        return 4

    print(f"Каталог проекта: {ROOT}")
    print(f"Адрес сервера: http://{host}:{port}")
    if host == "127.0.0.1":
        print("Этот адрес доступен только на самом компьютере; iPhone подключиться по нему не сможет.")
    else:
        print("Для подключения iPhone открой этот адрес в Safari, подключившись к точке доступа.")
    print("Остановка: Ctrl+C. Сервер работает только в этом окне.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port), "--workers", "1"],
            cwd=ROOT,
        )
        return result.returncode
    except KeyboardInterrupt:
        print("\nСервер остановлен пользователем.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
