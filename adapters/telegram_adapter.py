"""
Адаптер Telegram (Telethon userbot).
В Docker трафик идёт через Xray (SOCKS5 → VLESS → внешний сервер).

Контракт:
  GET  /status    → {"state": "connected|needs_auth|needs_code|needs_password"}
  GET  /qr        → PNG QR для входа из приложения Telegram
  POST /login     → form: phone  → запрос кода
  POST /code      → form: code   → подтверждение
  POST /password  → form: password → 2FA
  POST /send      → json: {peer_id, text}
  POST /logout    → удалить сессию

Запуск:
  uvicorn adapters.telegram_adapter:app --host 0.0.0.0 --port 9001
"""

import io
import os
from pathlib import Path

import httpx
import socks
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, Response
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

API_ID       = int(os.environ.get("TG_API_ID", "0") or "0")
API_HASH     = os.environ.get("TG_API_HASH", "")
CORE_URL     = os.environ.get("CORE_URL", "http://core:8000").rstrip("/")
ADAPTER_NAME = os.environ.get("ADAPTER_NAME", "telegram")
SESSION_FILE = os.environ.get("TG_SESSION_FILE", "/sessions/telegram.session")

# Xray SOCKS5 — работает только если TG_ENABLED=true и xray-сервис запущен
PROXY_HOST = os.environ.get("TG_PROXY_HOST", "xray")
PROXY_PORT = int(os.environ.get("TG_PROXY_PORT", "1080"))
USE_PROXY  = os.environ.get("TG_ENABLED", "true").lower() == "true"


def _read_session() -> str:
    try:
        return Path(SESSION_FILE).read_text().strip()
    except FileNotFoundError:
        return ""


def _save_session() -> None:
    p = Path(SESSION_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(client.session.save())


proxy = (socks.SOCKS5, PROXY_HOST, PROXY_PORT) if USE_PROXY else None

app    = FastAPI()
client = (
    TelegramClient(StringSession(_read_session()), API_ID, API_HASH, proxy=proxy)
    if API_ID and API_HASH else None
)

_login = {"phone": None, "hash": None, "needs_password": False}

_NOT_CONFIGURED = JSONResponse(
    {"error": "TG_API_ID / TG_API_HASH не заданы — заполните в настройках"},
    status_code=503,
)


@app.on_event("startup")
async def _startup():
    if not API_ID or not API_HASH:
        return  # не сконфигурирован — не подключаемся, /status вернёт needs_auth
    await client.connect()

    @client.on(events.NewMessage(incoming=True))
    async def _on_msg(event):
        if not event.is_private:
            return
        sender = await event.get_sender()
        name   = " ".join(filter(None, [
            getattr(sender, "first_name", None),
            getattr(sender, "last_name",  None),
        ])) or None
        phone = getattr(sender, "phone", None)
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        async with httpx.AsyncClient(timeout=20) as cli:
            await cli.post(f"{CORE_URL}/incoming", json={
                "adapter":  ADAPTER_NAME,
                "peer_id":  str(sender.id),
                "msg_id":   str(event.id),
                "text":     event.raw_text or "",
                "name":     name,
                "phone":    phone,
            })


@app.get("/status")
async def status():
    if not API_ID or not API_HASH:
        return {"state": "needs_auth", "hint": "TG_API_ID / TG_API_HASH не заданы"}
    if await client.is_user_authorized():
        return {"state": "connected"}
    if _login["needs_password"]:
        return {"state": "needs_password"}
    if _login["hash"]:
        return {"state": "needs_code"}
    return {"state": "needs_auth"}


@app.get("/qr")
async def qr():
    if client is None:
        return _NOT_CONFIGURED
    if await client.is_user_authorized():
        return JSONResponse({"state": "connected"})
    try:
        import qrcode
    except ImportError:
        return JSONResponse({"error": "qrcode not installed"}, status_code=501)
    qr_login = await client.qr_login()
    img = qrcode.make(qr_login.url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/login")
async def login(phone: str = Form(...)):
    if client is None:
        return _NOT_CONFIGURED
    sent = await client.send_code_request(phone)
    _login.update(phone=phone, hash=sent.phone_code_hash, needs_password=False)
    return {"ok": True, "state": "needs_code"}


@app.post("/code")
async def code(code: str = Form(...)):
    if client is None:
        return _NOT_CONFIGURED
    try:
        await client.sign_in(_login["phone"], code, phone_code_hash=_login["hash"])
    except SessionPasswordNeededError:
        _login["needs_password"] = True
        return {"ok": True, "state": "needs_password"}
    _save_session()
    _login.update(phone=None, hash=None, needs_password=False)
    return {"ok": True, "state": "connected"}


@app.post("/password")
async def password(password: str = Form(...)):
    if client is None:
        return _NOT_CONFIGURED
    await client.sign_in(password=password)
    _save_session()
    _login.update(phone=None, hash=None, needs_password=False)
    return {"ok": True, "state": "connected"}


@app.post("/logout")
async def logout():
    if client is None:
        return _NOT_CONFIGURED
    await client.log_out()
    p = Path(SESSION_FILE)
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.post("/send")
async def send(req: Request):
    if client is None:
        return _NOT_CONFIGURED
    body = await req.json()
    await client.send_message(int(body["peer_id"]), body["text"])
    return {"ok": True}
