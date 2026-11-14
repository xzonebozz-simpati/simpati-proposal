FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html login.html ./
COPY backend ./backend

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# --proxy-headers: IP asli pengguna (untuk pembatas login) terbaca di balik proxy hosting.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
