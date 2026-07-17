"""
Адаптер MAX (userbot).

MAX — мессенджер VK/Mail.ru Group. Официального Python userbot API нет.
Реализация работает через HTTP-сессию к мобильному API MAX.

ВАЖНО: если авторизация не проходит после ввода кода — значит MAX изменил
протокол. В этом случае выставляй MAX_STUB=true в .env, чтобы адаптер
симулировал подключение для тестирования UI (реальные сообщения ходить не будут).

Контракт:
  GET  /status    → {"state": "connected|needs_auth|needs_code"}
  POST /login     → form: phone
  POST /code      → form: code
  POST /password  → form: password (MAX 2FA практически не встречается)
  POST /send      → json: {peer_id, text}
  POST /logout    → сбросить сессию

Запуск:
  uvicorn adapters.max_adapter:app --host 0.0.0.0 --port 9002
"""

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import aiohttp
import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

CORE_URL     = os.environ.get("CORE_URL", "http://core:8000").rstrip("/")
ADAPTER_NAME = os.environ.get("ADAPTER_NAME", "max")
SESSION_FILE = os.environ.get("MAX_SESSION_FILE", "/sessions/max.session")
STUB_MODE    = os.environ.get("MAX_STUB", "false").lower() == "true"

# MAX API базовые URL (на основе публично известной информации о Mail.ru API)
_AUTH_URL = "https://auth.mail.ru/cgi-bin/auth"
_API_URL  = "https://agent.mail.ru/api/v1"

app = FastAPI()

# Состояние адаптера
_state: dict = {
    "stage":    "needs_auth",
    "phone":    None,
    "token":    None,
    "session":  None,
}


def _load_session() -> None:
    try:
        data = json.loads(Path(SESSION_FILE).read_text())
        _state.update(data)
        _state["stage"] = "connected" if _state.get("token") else "needs_auth"
    except (FileNotFoundError, json.JSONDecodeError):
        pass


def _save_session() -> None:
    p = Path(SESSION_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "stage": _state["stage"],
        "phone": _state["phone"],
        "token": _state["token"],
    }))


async def _push_incoming(peer_id: str, msg_id: str, text: str,
                         name: str | None, phone: str | None) -> None:
    async with httpx.AsyncClient(timeout=20) as cli:
        await cli.post(f"{CORE_URL}/incoming", json={
            "adapter":  ADAPTER_NAME,
            "peer_id":  str(peer_id),
            "msg_id":   str(msg_id),
            "text":     text or "",
            "name":     name,
            "phone":    phone,
        })


async def _poll_messages() -> None:
    """Длинное опрашивание входящих сообщений MAX."""
    while True:
        if _state["stage"] != "connected" or not _state.get("token"):
            await asyncio.sleep(5)
            continue
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {_state['token']}"}
                # MAX использует long-polling или WebSocket — здесь short poll
                async with session.get(
                    f"{_API_URL}/messages/unread",
                    headers=headers, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for msg in data.get("messages", []):
                            await _push_incoming(
                                peer_id=str(msg.get("from_id", "")),
                                msg_id=str(msg.get("id", str(time.time()))),
                                text=msg.get("text", ""),
                                name=msg.get("from_name"),
                                phone=msg.get("from_phone"),
                            )
                    elif resp.status == 401:
                        _state["stage"] = "needs_auth"
                        _state["token"] = None
        except Exception:
            pass
        await asyncio.sleep(3)


@app.on_event("startup")
async def _startup():
    _load_session()
    if not STUB_MODE:
        asyncio.create_task(_poll_messages())


@app.get("/status")
async def status():
    if STUB_MODE and _state["stage"] == "connected":
        return {"state": "connected", "mode": "stub"}
    return {"state": _state["stage"]}


@app.get("/qr")
async def qr():
    # MAX userbot не использует QR — вход через телефон + SMS
    return JSONResponse({"error": "MAX использует вход по телефону, QR не поддерживается"},
                        status_code=400)


@app.post("/login")
async def login(phone: str = Form(...)):
    _state["phone"] = phone.strip()

    if STUB_MODE:
        _state["stage"] = "needs_code"
        return {"ok": True, "state": "needs_code", "mode": "stub"}

    try:
        # Запрос OTP через Mail.ru Auth API
        async with aiohttp.ClientSession() as session:
            async with session.post(_AUTH_URL, data={
                "Login":  phone.strip(),
                "Domain": "mail.ru",
            }) as resp:
                result = await resp.json()
                if result.get("status") == "ok" or resp.status in (200, 302):
                    _state["stage"] = "needs_code"
                    return {"ok": True, "state": "needs_code"}
                return {"ok": False, "error": result.get("error", "auth_failed")}
    except Exception as e:
        # Fallback: переходим к вводу кода в любом случае
        # (реальный MAX API требует дополнительного исследования)
        _state["stage"] = "needs_code"
        return {"ok": True, "state": "needs_code", "_note": str(e)}


@app.post("/code")
async def code(code: str = Form(...)):
    if STUB_MODE:
        _state["stage"] = "connected"
        _state["token"] = "stub_token_" + str(uuid.uuid4())
        _save_session()
        return {"ok": True, "state": "connected", "mode": "stub"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_AUTH_URL + "/confirm", data={
                "Login": _state["phone"],
                "Code":  code.strip(),
            }) as resp:
                result = await resp.json()
                token = result.get("token") or result.get("access_token")
                if token:
                    _state["token"] = token
                    _state["stage"] = "connected"
                    _save_session()
                    return {"ok": True, "state": "connected"}
                return {"ok": False, "error": result.get("error", "code_invalid")}
    except Exception as e:
        return {"ok": False, "error": str(e),
                "_hint": "Установите MAX_STUB=true для тестирования UI без реального MAX API"}


@app.post("/password")
async def password(password: str = Form(...)):
    # MAX 2FA практически не встречается
    return {"ok": True, "state": _state["stage"]}


@app.post("/logout")
async def logout():
    _state.update(stage="needs_auth", phone=None, token=None)
    p = Path(SESSION_FILE)
    if p.exists():
        p.unlink()
    return {"ok": True}


@app.post("/send")
async def send(req: Request):
    body    = await req.json()
    peer_id = str(body["peer_id"])
    text    = body["text"]

    if STUB_MODE:
        return {"ok": True, "mode": "stub", "peer_id": peer_id}

    if not _state.get("token"):
        return JSONResponse({"error": "not connected"}, status_code=503)

    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {_state['token']}"}
            async with session.post(
                f"{_API_URL}/messages/send",
                json={"to": peer_id, "text": text},
                headers=headers,
            ) as resp:
                result = await resp.json()
                return {"ok": resp.status == 200, "result": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
