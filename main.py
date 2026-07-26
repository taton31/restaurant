from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from headers import emulator

load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


LATITUDE = float(required_env("YANDEX_LATITUDE"))
LONGITUDE = float(required_env("YANDEX_LONGITUDE"))
LAYOUT_URL = required_env("YANDEX_LAYOUT_URL")
REFERER = required_env("YANDEX_REFERER")
USER_AGENT = required_env("YANDEX_USER_AGENT")
APP_VERSION = required_env("YANDEX_APP_VERSION")


def build_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://eda.yandex.ru",
        "pragma": "no-cache",
        "referer": REFERER,
        "user-agent": USER_AGENT,
        "x-app-version": APP_VERSION,
        "x-client-session": emulator.generate_id(),
        "x-device-id": emulator.generate_id(),
        "x-platform": "desktop_web",
        "x-retpath-y": REFERER,
        "x-taxi": f"{USER_AGENT} platform=eats_desktop_web",
        "x-ya-client-time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "x-ya-coordinates": f"latitude={LATITUDE},longitude={LONGITUDE}",
    }


def fetch_layout() -> dict:
    json_data = {"location": {"latitude": LATITUDE, "longitude": LONGITUDE}}
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.post(LAYOUT_URL, headers=build_headers(), json=json_data)
        response.raise_for_status()
        return response.json()


if __name__ == "__main__":
    payload = fetch_layout()
    print(payload)
