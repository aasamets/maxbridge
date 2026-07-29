"""
MAX адаптер через pymax (WebClient, QR-авторизация, self-hosted).

Авторизация:
  1. Адаптер запускается, запрашивает QR у MAX-серверов
  2. GET /qr — возвращает PNG QR-кода
  3. Пользователь сканирует QR в приложении MAX:
     Профиль → Устройства → Привязать устройство → Сканировать QR
  4. Сессия сохраняется в SESSION_DIR/session.db (volume)

Контракт:
  GET  /status  → {"state": "connected|needs_auth|unavailable"}
  GET  /qr      → PNG QR-кода (пока не подключён)
  POST /send    → json: {peer_id: str, text: str}
  POST /logout  → сбросить сессию и переавторизоваться
  POST /reconnect → перезапустить клиент
  POST /login|code|password → stub для совместимости с UI

Переменные окружения:
  SESSION_DIR  — директория для сессии (default: /sessions/max)
  CORE_URL     — адрес ядра (default: http://core:8000)
  ADAPTER_NAME — имя адаптера (default: max)
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil

import httpx
import qrcode
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pymax import Message, WebClient
from pymax.auth.providers import QrHandler

CORE_URL     = os.environ.get("CORE_URL", "http://core:8000").rstrip("/")
ADAPTER_NAME = os.environ.get("ADAPTER_NAME", "max")
SESSION_DIR  = os.environ.get("SESSION_DIR", "/sessions/max")

app = FastAPI()

_state: str           = "needs_auth"
_current_qr_png: bytes | None = None
_client: WebClient | None     = None
_client_task: asyncio.Task | None = None


# ── QR handler ───────────────────────────────────────────────────────────────

class _QrHandler:
    """Перехватывает QR URL от pymax и сохраняет PNG для HTTP-эндпоинта."""

    async def show_qr(self, qr_url: str) -> None:
        global _current_qr_png, _state
        qr_obj = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr_obj.add_data(qr_url)
        buf = io.BytesIO()
        qr_obj.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        _current_qr_png = buf.getvalue()
        _state = "needs_auth"


# ── Клиент pymax ─────────────────────────────────────────────────────────────

async def _push_incoming(client: WebClient, msg: Message) -> None:
    """Пушит входящее сообщение в ядро."""
    chat_id = msg.chat_id
    sender  = msg.sender
    text    = msg.text

    # Пытаемся получить номер телефона отправителя для CRM-маппинга в Битриксе
    phone: str | None = None
    if sender is not None:
        try:
            user = await client.get_user(sender)
            if user and user.phone:
                phone = f"+{user.phone}"
        except Exception:
            pass

    async with httpx.AsyncClient(timeout=10) as cli:
        await cli.post(f"{CORE_URL}/incoming", json={
            "adapter": ADAPTER_NAME,
            "peer_id": str(chat_id),
            "msg_id":  "",
            "text":    text,
            "name":    None,
            "phone":   phone or str(sender),
        })


async def _run_client() -> None:
    global _state, _client, _current_qr_png

    os.makedirs(SESSION_DIR, exist_ok=True)

    _client = WebClient(
        session_name="session.db",
        work_dir=SESSION_DIR,
        qr_provider=_QrHandler(),
    )

    @_client.on_start()
    async def on_start(client: WebClient) -> None:
        global _state, _current_qr_png
        _state = "connected"
        _current_qr_png = None
        # Авто-определение номера из сессии
        try:
            me = client.me
            if me and me.contact.phone:
                phone = f"+{me.contact.phone}"
                async with httpx.AsyncClient(timeout=5) as _cli:
                    await _cli.post(f"{CORE_URL}/api/adapter_phone",
                                    json={"adapter": ADAPTER_NAME, "phone": phone})
        except Exception:
            pass

    @_client.on_message()
    async def on_message(msg: Message, client: WebClient) -> None:
        if not msg.text:
            return
        me = client.me
        if me and msg.sender == me.contact.id:
            return
        try:
            await _push_incoming(client, msg)
        except Exception as exc:
            print(f"[MAX] Ошибка отправки в core: {exc}")

    _state = "needs_auth"
    _current_qr_png = None
    try:
        await _client.start()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[MAX] Клиент упал: {exc}")
    finally:
        _state = "needs_auth"
        _current_qr_png = None


async def _supervisor() -> None:
    """Перезапускает клиент при падении."""
    global _client_task
    while True:
        if _client_task is None or _client_task.done():
            _client_task = asyncio.create_task(_run_client())
        await asyncio.sleep(5)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_supervisor())


# ── Эндпоинты ────────────────────────────────────────────────────────────────

@app.get("/status")
def status():
    return {"state": _state}


@app.get("/qr")
async def qr():
    if _state == "connected":
        return JSONResponse({"state": "connected"})
    if _current_qr_png:
        return Response(content=_current_qr_png, media_type="image/png")
    return JSONResponse({"state": _state, "hint": "QR ещё не готов — подождите"}, status_code=202)


@app.post("/send")
async def send(req: Request):
    body    = await req.json()
    peer_id = str(body["peer_id"])
    text    = body.get("text", "")

    if _state != "connected" or _client is None:
        return JSONResponse({"error": f"не подключён (state={_state})"}, status_code=503)

    try:
        if peer_id.startswith("+"):
            # Телефонный номер — ищем через MAX API и открываем личный чат
            user = await _client.search_by_phone(peer_id.lstrip("+"))
            if user is None:
                return JSONResponse({"error": f"Пользователь {peer_id} не найден в MAX"}, status_code=404)
            me = _client.me
            chat_id_int = _client.get_chat_id(me.contact.id, user.id)
            # При старте клиента чаты ещё не загружены — get_chat_id возвращает 0
            if chat_id_int == 0:
                return JSONResponse(
                    {"error": "Чат не найден (клиент только запустился, повторите через 10–15 секунд)"},
                    status_code=503,
                )
        else:
            chat_id_int = int(peer_id)
        await _client.send_message(chat_id_int, text)
        return {"ok": True}
    except ValueError:
        return JSONResponse({"error": f"некорректный peer_id: {peer_id}"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/reconnect")
async def reconnect():
    global _client_task, _client
    if _client_task and not _client_task.done():
        _client_task.cancel()
        try:
            await _client_task
        except (asyncio.CancelledError, Exception):
            pass
    _client_task = None
    _client = None
    return {"ok": True, "state": _state}


@app.post("/logout")
async def logout():
    global _state, _current_qr_png, _client_task, _client
    if _client_task and not _client_task.done():
        _client_task.cancel()
        try:
            await _client_task
        except (asyncio.CancelledError, Exception):
            pass
    _client_task = None
    _client = None
    _current_qr_png = None
    _state = "needs_auth"
    shutil.rmtree(SESSION_DIR, ignore_errors=True)
    return {"ok": True}


# Stub-эндпоинты для совместимости с UI (MAX авторизуется по QR, не по телефону)
@app.post("/login")
async def login():
    return {"ok": True, "state": _state}

@app.post("/code")
async def code():
    return {"ok": True, "state": _state}

@app.post("/password")
async def password_ep():
    return {"ok": True, "state": _state}
