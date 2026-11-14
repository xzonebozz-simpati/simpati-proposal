"""Konfigurasi Gemini untuk PROPOSAL_MANAGEMENT V8."""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_env() -> None:
    """Baca .env lokal tanpa menimpa environment yang sudah tersedia."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_local_env()

# Urutan model sesuai keputusan proyek: utama lalu fallback.
DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-3.5-flash")


def get_api_key() -> str:
    # AI_API_KEY mempertahankan kompatibilitas .env V7/Jarvis-style key naming.
    return (os.getenv("AI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def get_models() -> list[str]:
    raw = os.getenv("GEMINI_MODELS", "").strip()
    if raw:
        models = [item.strip().removeprefix("models/") for item in raw.split(",") if item.strip()]
        # Jangan pernah menjalankan model kosong/duplikat.
        return list(dict.fromkeys(models)) or list(DEFAULT_MODELS)
    return list(DEFAULT_MODELS)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


MAX_RETRIES = _int_env("GEMINI_MAX_RETRIES", 2, 1, 4)  # total attempts per model
REQUEST_TIMEOUT = _float_env("GEMINI_TIMEOUT", 90.0, 10.0, 180.0)
TOTAL_TIMEOUT = _float_env("GEMINI_TOTAL_TIMEOUT", 170.0, 30.0, 300.0)
IMAGE_MAX_SIDE = _int_env("GEMINI_IMAGE_MAX_SIDE", 1280, 256, 2048)


def ai_configuration_status() -> dict:
    models = get_models()
    return {
        "provider": "gemini",
        "model": models[0],
        "fallback_models": models[1:],
        "configured": bool(get_api_key()),
        "api_key_present": bool(get_api_key()),
    }
