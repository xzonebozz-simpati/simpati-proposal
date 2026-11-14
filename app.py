"""PROPOSAL_MANAGEMENT v9 - login via SIMPATI 555, hasil verifikasi dari Survey GPS, narasi oleh Gemini.

Alur akses:
  1. Superadmin membuat akun Admin DPS (peran ADMIN_DPS + wilayah kecamatan) di SIMPATI 555.
  2. Pengguna login di halaman /login; kredensial diverifikasi ke SIMPATI 555 (password tidak disimpan di sini).
  3. Wilayah, Ketua, dan Sekretaris DPS selalu dari SIMPATI 555; hasil verifikasi dari Survey GPS
     dibatasi pada kecamatan akun tersebut. Browser tidak pernah bisa menentukan wilayahnya sendiri.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from backend import settings
from backend.ai_config import ai_configuration_status
from backend.ai_service import AIServiceError, generate_narrative
from backend.security import COOKIE_NAME, SlidingWindow, make_session, read_session
from backend.simpati_client import AccessDenied, SimpatiClient, SurveyClient, UpstreamError

logger = logging.getLogger("proposal.app")
BASE_DIR = Path(__file__).resolve().parent
VERSION = "0.9.0"

# Dokumentasi API interaktif dimatikan: aplikasi ini hanya dipakai lewat antarmukanya sendiri.
app = FastAPI(title="PROPOSAL_MANAGEMENT API", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)

simpati = SimpatiClient(settings.SIMPATI_API_URL, settings.INTEGRATION_TOKEN, settings.UPSTREAM_TIMEOUT)
survey = SurveyClient(settings.SURVEY_API_URL, settings.INTEGRATION_TOKEN, settings.UPSTREAM_TIMEOUT)

login_limiter = SlidingWindow(limit=10, window_seconds=300)                        # gagal login per IP
generate_limiter = SlidingWindow(limit=settings.GENERATE_PER_HOUR, window_seconds=3600)

# username -> (waktu cek, profil). Profil dicek ulang ke SIMPATI tiap PROFILE_TTL_SECONDS.
_profiles: dict[str, tuple[float, dict]] = {}

DENIED_MESSAGES = {
    "invalid_credentials": (401, "Username atau password salah."),
    "role_not_allowed": (403, "Akun ini tidak memiliki akses Proposal. Hanya akun Admin DPS yang dibuat Superadmin."),
    "must_change_password": (403, "Password awal belum diganti. Login ke SIMPATI 555 terlebih dahulu untuk menggantinya."),
    "scope_invalid": (403, "Wilayah akun belum dikonfigurasi dengan benar di SIMPATI 555. Hubungi Superadmin."),
    "account_inactive": (403, "Akun dinonaktifkan atau tidak ditemukan. Hubungi Superadmin."),
    "license_restricted": (403, "Lisensi SIMPATI sedang dalam Mode Terbatas. Hubungi Superadmin."),
}


# ------------------------------------------------------------------ sesi
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response, profile: dict) -> None:
    ttl = settings.SESSION_HOURS * 3600
    token = make_session(settings.SESSION_SECRET, profile["username"], profile["account_version"], ttl)
    response.set_cookie(COOKIE_NAME, token, max_age=ttl, httponly=True, secure=settings.COOKIE_SECURE,
                        samesite="lax", path="/")


def _clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=settings.COOKIE_SECURE, samesite="lax")


def current_profile(request: Request) -> dict | None:
    """Profil akun yang sedang login (None bila belum/tidak lagi sah). Selalu bersumber dari SIMPATI 555."""
    session = read_session(settings.SESSION_SECRET, request.cookies.get(COOKIE_NAME))
    if not session:
        return None
    username, version, now = session["u"], session.get("v", ""), time.time()
    cached = _profiles.get(username)
    if cached and now - cached[0] < settings.PROFILE_TTL_SECONDS and cached[1]["account_version"] == version:
        return cached[1]
    try:
        profile = simpati.profile(username)
    except AccessDenied:
        _profiles.pop(username, None)
        return None
    except UpstreamError as exc:
        # SIMPATI sesaat tidak terjangkau: pakai data terakhir yang sah dalam batas toleransi.
        if cached and now - cached[0] < settings.PROFILE_GRACE_SECONDS and cached[1]["account_version"] == version:
            logger.warning("SIMPATI tidak terjangkau; memakai profil tersimpan: %s", exc)
            return cached[1]
        raise HTTPException(status_code=503, detail="SIMPATI 555 tidak dapat dihubungi. Coba lagi sebentar.") from exc
    if profile["account_version"] != version:  # password/akun diubah Superadmin -> login ulang
        _profiles.pop(username, None)
        return None
    _profiles[username] = (now, profile)
    return profile


def require_profile(request: Request) -> dict:
    profile = current_profile(request)
    if not profile:
        raise HTTPException(status_code=401, detail="Sesi berakhir. Silakan login kembali.")
    return profile


def require_ajax(request: Request) -> None:
    """Perlindungan CSRF untuk aksi POST: header kustom memaksa preflight CORS pada permintaan lintas situs."""
    if request.headers.get("x-requested-with") != "proposal":
        raise HTTPException(status_code=403, detail="Permintaan ditolak.")


def public_profile(profile: dict) -> dict:
    return {
        "username": profile["username"], "territory": profile["territory"], "province": profile["province"],
        "regency": profile["regency"], "district": profile["district"],
        "chair": profile.get("chair", ""), "secretary": profile.get("secretary", ""),
        "survey_enabled": bool(profile.get("survei_module_allowed")) and bool(settings.SURVEY_API_URL),
    }


# ------------------------------------------------------------------ halaman & auth
def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    try:
        if current_profile(request):
            return RedirectResponse("/", status_code=303)
    except HTTPException:
        pass
    return _no_store(FileResponse(BASE_DIR / "login.html"))


class LoginInput(BaseModel):
    username: str = Field(default="", max_length=80)
    password: str = Field(default="", max_length=200)


@app.post("/api/auth/login")
def login(data: LoginInput, request: Request):
    require_ajax(request)
    problems = settings.config_problems()
    if problems:
        logger.error("Konfigurasi integrasi belum lengkap: %s", "; ".join(problems))
        raise HTTPException(status_code=503, detail="Layanan login belum dikonfigurasi. Hubungi administrator sistem.")
    ip = _client_ip(request)
    wait = login_limiter.retry_after(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Terlalu banyak percobaan gagal. Coba lagi dalam {wait} detik.",
                            headers={"Retry-After": str(wait)})
    username = data.username.strip()
    if not username or not data.password:
        raise HTTPException(status_code=422, detail="Username dan password wajib diisi.")
    try:
        profile = simpati.login(username, data.password)
    except AccessDenied as denied:
        if denied.reason == "locked":
            raise HTTPException(status_code=429, detail=f"Terlalu banyak percobaan gagal. Coba lagi dalam {denied.retry_after or 60} detik.",
                                headers={"Retry-After": str(denied.retry_after or 60)}) from denied
        if denied.reason == "invalid_credentials":
            login_limiter.hit(ip)
        status, message = DENIED_MESSAGES.get(denied.reason, (403, "Akses ditolak oleh SIMPATI 555."))
        raise HTTPException(status_code=status, detail=message) from denied
    except UpstreamError as exc:
        logger.error("Login gagal karena integrasi: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    login_limiter.reset(ip)
    _profiles[profile["username"]] = (time.time(), profile)
    response = JSONResponse({"ok": True, "profile": public_profile(profile)})
    _set_session_cookie(response, profile)
    return _no_store(response)


@app.post("/api/auth/logout")
def logout(request: Request):
    require_ajax(request)
    session = read_session(settings.SESSION_SECRET, request.cookies.get(COOKIE_NAME))
    if session:
        _profiles.pop(session["u"], None)
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return _no_store(response)


@app.get("/api/me")
def me(profile: dict = Depends(require_profile)):
    return _no_store(JSONResponse(public_profile(profile)))


@app.get("/", include_in_schema=False)
def index(request: Request):
    try:
        profile = current_profile(request)
    except HTTPException:  # SIMPATI sesaat tidak terjangkau dan tidak ada profil tersimpan
        return RedirectResponse("/login?error=upstream", status_code=303)
    if not profile:
        return RedirectResponse("/login", status_code=303)
    return _no_store(FileResponse(BASE_DIR / "index.html"))


# ------------------------------------------------------------------ penyusunan proposal
def decode_image_data(value: str) -> bytes:
    """Decode base64 dari browser, toleran terhadap awalan data-URL dan padding yang hilang."""
    encoded = (value or "").strip()
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    encoded = re.sub(r"\s+", "", encoded).replace("-", "+").replace("_", "/")
    if not encoded or re.search(r"[^A-Za-z0-9+/=]", encoded):
        raise ValueError("Karakter base64 tidak dikenal")
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    return base64.b64decode(encoded, validate=True)


class RabItem(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    qty: float = Field(ge=0)
    unit: str = Field(default="", max_length=60)
    price: float = Field(ge=0)


class ImageInput(BaseModel):
    name: str = Field(default="gambar", max_length=250)
    mime_type: str
    data: str = Field(min_length=1)


class ProposalInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    proposal_category: str = Field(default="", max_length=150)
    applicant_type: str = Field(default="", max_length=150)
    kind: str = Field(default="", max_length=150)
    data_source: str = Field(default="", max_length=30)
    source_survey_id: str | None = Field(default=None, max_length=30)
    applicant: str = Field(min_length=1, max_length=200)
    business: str = Field(min_length=1, max_length=200)
    sector: str = Field(default="", max_length=150)
    address: str = Field(default="", max_length=500)
    verifier: str = Field(default="", max_length=200)
    territory: str = Field(default="", max_length=250)  # diabaikan: selalu diganti dari profil SIMPATI
    date: str = Field(default="", max_length=40)
    condition: str = Field(min_length=1, max_length=5000)
    problem: str = Field(min_length=1, max_length=5000)
    purpose: str = Field(default="", max_length=5000)
    utilization: str = Field(default="", max_length=5000)
    rab: list[RabItem] = Field(default_factory=list, max_length=100)
    survey_images: list[ImageInput] = Field(default_factory=list, max_length=12)
    support_images: list[ImageInput] = Field(default_factory=list, max_length=12)


@app.get("/api/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/api/ai-status")
def ai_status(_: dict = Depends(require_profile)):
    return ai_configuration_status()


@app.post("/api/generate")
def generate(data: ProposalInput, request: Request, profile: dict = Depends(require_profile)):
    require_ajax(request)
    wait = generate_limiter.retry_after(profile["username"])
    if wait:
        raise HTTPException(status_code=429, detail=f"Batas penyusunan proposal per jam tercapai. Coba lagi dalam {wait // 60 + 1} menit.",
                            headers={"Retry-After": str(wait)})
    payload: dict[str, Any] = data.model_dump()
    payload["territory"] = profile["territory"]  # wilayah resmi dari SIMPATI, bukan dari browser
    if len(payload["survey_images"]) + len(payload["support_images"]) > 12:
        raise HTTPException(status_code=422, detail="Maksimal 12 gambar total untuk satu proses generate.")
    total_image_bytes = 0
    for group in (payload["survey_images"], payload["support_images"]):
        for image in group:
            if image["mime_type"] not in {"image/jpeg", "image/png", "image/webp"}:
                raise HTTPException(status_code=422, detail="Format gambar harus JPG, PNG, atau WEBP.")
            try:
                image_bytes = decode_image_data(image["data"])
            except Exception:
                raise HTTPException(status_code=422, detail=f"Data gambar {image['name']} tidak valid.")
            if len(image_bytes) > 6 * 1024 * 1024:
                raise HTTPException(status_code=413, detail=f"Gambar {image['name']} melebihi 6 MB.")
            total_image_bytes += len(image_bytes)
    if total_image_bytes > 18 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Total ukuran seluruh gambar maksimal 18 MB per proses.")
    generate_limiter.hit(profile["username"])
    try:
        narrative = generate_narrative(payload)
    except AIServiceError as exc:
        raise HTTPException(status_code=getattr(exc, "status_code", 503), detail=str(exc)) from exc
    logger.info("Proposal dibuat oleh %s (%s)", profile["username"], profile["district"])
    return {"title": data.title, "narrative": narrative, "ai_provider": ai_configuration_status()["provider"]}


# ------------------------------------------------------------------ hasil verifikasi dari Survey GPS
def _survey_guard(profile: dict) -> None:
    if not settings.SURVEY_API_URL:
        raise HTTPException(status_code=503, detail="Integrasi Survey GPS belum dikonfigurasi (SURVEY_API_URL).")
    if not profile.get("survei_module_allowed"):
        raise HTTPException(status_code=403, detail="Survei GPS tidak termasuk dalam lisensi.")


@app.get("/api/integrations/survey/status")
def survey_status(profile: dict = Depends(require_profile)):
    enabled = bool(settings.SURVEY_API_URL) and bool(profile.get("survei_module_allowed"))
    return _no_store(JSONResponse({"enabled": enabled}))


@app.get("/api/integrations/survey/verified-results")
def survey_verified_results(profile: dict = Depends(require_profile)):
    _survey_guard(profile)
    try:
        items = survey.list_verified(profile)  # wilayah diambil dari profil sesi, bukan dari permintaan
    except UpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _no_store(JSONResponse({"items": items}))


@app.get("/api/integrations/survey/verified-results/{survey_id}")
def survey_verified_result(survey_id: str, profile: dict = Depends(require_profile)):
    _survey_guard(profile)
    if not re.fullmatch(r"\d{1,18}", survey_id):
        raise HTTPException(status_code=422, detail="ID hasil verifikasi tidak valid.")
    try:
        record = survey.detail(profile, int(survey_id))
    except UpstreamError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="Hasil verifikasi tidak ditemukan pada wilayah DPS Anda.")
    return _no_store(JSONResponse(record))
