"""Gemini GenerateContent adapter dengan retry terbatas dan fallback model.
Mempertahankan kontrak JSON V7 yang digunakan oleh frontend proposal.
"""
from __future__ import annotations
import base64
import io
import json
import logging
import random
import re
import time
from typing import Any
import requests

from PIL import Image, ImageOps, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

from .prompts import SYSTEM_PROMPT
from .ai_config import IMAGE_MAX_SIDE, MAX_RETRIES, REQUEST_TIMEOUT, TOTAL_TIMEOUT, get_api_key, get_models

logger = logging.getLogger("proposal.ai")
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

KEYS = ["kata_pengantar", "latar_belakang", "profil_umkm", "permasalahan_kebutuhan", "maksud_tujuan", "rencana_pemanfaatan", "penutup"]

SYSTEM_RULES = SYSTEM_PROMPT


def _decode_b64(value: str) -> bytes:
    encoded = (value or "").strip()
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    encoded = re.sub(r"\s+", "", encoded).replace("-", "+").replace("_", "/")
    if not encoded or re.search(r"[^A-Za-z0-9+/=]", encoded):
        raise ValueError("Karakter base64 tidak dikenal")
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    return base64.b64decode(encoded, validate=True)


def _normalize_image(raw: bytes) -> bytes:
    """Pulihkan gambar yang masih terbaca lalu kirim sebagai JPEG standar."""
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            if source.width < 2 or source.height < 2:
                raise ValueError("dimensi gambar tidak valid")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE), Image.Resampling.LANCZOS)
            if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True, progressive=False)
            result = output.getvalue()
            if not result:
                raise ValueError("hasil normalisasi kosong")
            return result
    except Exception as exc:
        raise ValueError(f"file bukan gambar yang dapat dibaca atau rusak ({exc})") from exc


def _rupiah(value: float) -> str:
    return "Rp " + f"{int(round(value)):,}".replace(",", ".")


def _build_rab_text(rab: list[dict[str, Any]]) -> str:
    if not rab:
        return "(RAB belum diisi)"
    lines = []
    total = 0.0
    for i, item in enumerate(rab, 1):
        subtotal = float(item["qty"]) * float(item["price"])
        total += subtotal
        unit = (item.get("unit") or "").strip()
        qty = int(item["qty"]) if float(item["qty"]).is_integer() else item["qty"]
        lines.append(f"{i}. {item['name']} | {qty} {unit} x {_rupiah(item['price'])} = {_rupiah(subtotal)}")
    lines.append(f"TOTAL RAB = {_rupiah(total)}")
    return "\n".join(lines)


def _build_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    # Bukan bahan narasi: gambar dikirim terpisah; id sumber/verifier hanya untuk antarmuka.
    clean = {k: v for k, v in payload.items()
             if k not in {"survey_images", "support_images", "source_survey_id", "data_source", "verifier"}}
    clean["rab_text_final"] = _build_rab_text(payload.get("rab") or [])
    text = (
        "DATA PROPOSAL (gunakan hanya informasi ini):\n"
        + json.dumps(clean, ensure_ascii=False, indent=2)
        + "\n\nTUGAS: Susun tujuh bagian narasi sesuai system instruction. "
          "Judul proposal harus tetap persis seperti isian DPS dan jangan ditulis ulang."
    )
    parts: list[dict[str, Any]] = [{"text": text}]
    for group_key, group_label in (("survey_images", "DOKUMENTASI KONDISI UMKM"), ("support_images", "GAMBAR PENDUKUNG PERMOHONAN")):
        for image in payload.get(group_key) or []:
            try:
                raw = _decode_b64(image["data"])
                normalized = _normalize_image(raw)
            except Exception as exc:
                raise AIServiceError(f"Gambar {image.get('name', 'lampiran')} tidak dapat diproses: {exc}", 422) from exc
            parts.append({"text": f"{group_label}: {image.get('name', 'gambar')}"})
            # Gemini REST JSON uses lowerCamelCase inlineData, not inline_data.
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(normalized).decode("ascii")}})
    return parts


def _extract_json(text: str) -> dict[str, str]:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I | re.S).strip()
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Respons Gemini tidak berisi JSON proposal.")
    data = json.loads(value[start:end + 1])
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key].strip() for key in KEYS):
        raise ValueError("Respons Gemini belum memuat seluruh tujuh bagian narasi.")
    return {key: data[key].strip() for key in KEYS}


class AIServiceError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


class _Failure(Exception):
    def __init__(self, action: str, status: int | None, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.action, self.status, self.message, self.retry_after = action, status, message, retry_after


def _error_message(response: requests.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", "")) or response.text[:400]
    except Exception:
        return response.text[:400]


def _call_model(model: str, api_key: str, body: dict[str, Any], timeout: float) -> dict[str, str]:
    try:
        response = requests.post(API_URL.format(model=model), headers={"x-goog-api-key": api_key, "Content-Type": "application/json"}, json=body, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise _Failure("retry", None, f"koneksi/timeout: {type(exc).__name__}") from exc
    status = response.status_code
    if status == 200:
        try:
            result = response.json()
            block = (result.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise _Failure("fatal", 422, f"Permintaan diblokir Gemini ({block}). Periksa data proposal.")
            candidates = result.get("candidates") or []
            chunks = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
            text = "".join(part.get("text", "") for part in chunks if not part.get("thought")).strip()
            if not text:
                raise _Failure("retry", 200, "respons kosong dari model")
            try:
                return _extract_json(text)
            except (ValueError, json.JSONDecodeError) as exc:
                raise _Failure("retry", 200, f"format JSON dari model tidak valid: {exc}") from exc
        except _Failure:
            raise
        except Exception as exc:
            raise _Failure("retry", 200, f"respons Gemini tidak dapat dibaca: {type(exc).__name__}") from exc

    message = _error_message(response)
    retry_after = None
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        pass
    if status in (401, 403) or (status == 400 and "api key" in message.lower()):
        raise _Failure("fatal", 500, "API key Gemini tidak valid atau tidak memiliki akses. Periksa AI_API_KEY pada pengaturan server.")
    if status == 404 or (status == 400 and "model" in message.lower()):
        raise _Failure("next", status, f"model {model} tidak tersedia: {message[:220]}")
    if status == 400:
        raise _Failure("fatal", 422, f"Permintaan ditolak Gemini: {message[:350]}")
    if status in RETRYABLE_STATUS:
        raise _Failure("retry", status, message or f"HTTP {status}", retry_after)
    raise _Failure("fatal", status if 400 <= status < 600 else 502, message or f"HTTP {status}")


def generate_narrative(payload: dict[str, Any]) -> dict[str, str]:
    api_key = get_api_key()
    if not api_key:
        raise AIServiceError("API key Gemini belum diisi. Isi AI_API_KEY pada pengaturan server (Environment Variables).", 500)
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_RULES}]},
        "contents": [{"role": "user", "parts": _build_parts(payload)}],
        "generationConfig": {"temperature": 0.35, "maxOutputTokens": 8192, "responseMimeType": "application/json"},
    }
    models = get_models()
    deadline = time.monotonic() + TOTAL_TIMEOUT
    tried: list[str] = []
    last: _Failure | None = None
    for model in models:
        tried.append(model)
        for attempt in range(1, MAX_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining < 8:
                break
            try:
                result = _call_model(model, api_key, body, min(REQUEST_TIMEOUT, remaining))
                logger.info("Proposal berhasil dibuat dengan model %s (percobaan %s)", model, attempt)
                return result
            except _Failure as failure:
                last = failure
                logger.warning("Gemini %s percobaan %s gagal [%s]: %s", model, attempt, failure.status, failure.message[:300])
                if failure.action == "fatal":
                    raise AIServiceError(failure.message, failure.status or 500) from failure
                if failure.action == "next":
                    break
                if attempt >= MAX_RETRIES:
                    break
                # Exponential backoff dengan jitter, maksimum 12 detik.
                delay = min((2 ** (attempt - 1)) + random.uniform(0, 0.75), 12.0)
                if failure.retry_after is not None:
                    delay = max(delay, min(failure.retry_after, 20.0))
                if deadline - time.monotonic() <= delay + 8:
                    break
                time.sleep(delay)
        if deadline - time.monotonic() < 8:
            break
    if last and last.status == 429:
        http_status, reason = 429, "batas kuota atau laju permintaan Gemini tercapai"
    elif last and last.status == 400:
        http_status, reason = 422, "permintaan proposal ditolak Gemini"
    else:
        http_status, reason = 503, "layanan Gemini sementara tidak tersedia"
    detail = f" Detail terakhir: {last.message[:220]}" if last else ""
    raise AIServiceError(f"{reason.capitalize()}. Model yang dicoba: {', '.join(tried)}. Silakan coba kembali beberapa saat lagi.{detail}", http_status)
