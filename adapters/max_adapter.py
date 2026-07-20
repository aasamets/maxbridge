"""
MAX адаптер через GREEN-API (green-api.com/max).

Авторизация MAX-аккаунта — один раз в кабинете GREEN-API по QR.
Адаптер подключается к GREEN-API по REST, получает сообщения через polling.

Контракт:
  GET  /status              → {"state": "connected|needs_auth|unavailable"}
  POST /webhook             → вебхук GREEN-API (альтернатива polling)
  POST /send                → json: {peer_id, text}
  POST /reconnect           → перепроверить подключение
  POST /login|code|password|logout  → stub для совместимости с UI

Переменные окружения:
  GREENAPI_ID_INSTANCE  — idInstance из кабинета GREEN-API
  GREENAPI_TOKEN        — apiTokenInstance из кабинета GREEN-API
  CORE_URL              — адрес ядра (default: http://core:8000)
  ADAPTER_NAME          — имя адаптера (default: max)

Запуск:
  uvicorn adapters.max_adapter:app --host 0.0.0.0 --port 9002
"""

import asyncio
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CORE_URL     = os.environ.get("CORE_URL", "http://core:8000").rstrip("/")
ADAPTER_NAME = os.environ.get("ADAPTER_NAME", "max")
ID_INSTANCE  = os.environ.get("GREENAPI_ID_INSTANCE", "").strip()
API_TOKEN    = os.environ.get("GREENAPI_TOKEN", "").strip()

app = FastAPI()

_state: str = "needs_auth"
_greenapi = None
_event_loop: asyncio.AbstractEventLoop | None = None
_polling_active: bool = False


def _configured() -> bool:
    return bool(ID_INSTANCE and API_TOKEN)


def _make_client():
    from max_api_client_python import API  # noqa: PLC0415
    return API.GreenAPI(ID_INSTANCE, API_TOKEN)


async def _push_incoming(body: dict) -> None:
    sender   = body.get("senderData", {})
    msg_data = body.get("messageData", {})

    chat_id = sender.get("chatId", "")
    msg_id  = body.get("idMessage", "")
    name    = sender.get("chatName") or sender.get("senderName", "")
    phone   = chat_id.split("@")[0] if "@" in chat_id else None

    type_msg = msg_data.get("typeMessage", "")
    text = ""
    if type_msg == "textMessage":
        text = msg_data.get("textMessageData", {}).get("textMessage", "")
    elif type_msg == "extendedTextMessage":
        text = msg_data.get("extendedTextMessageData", {}).get("text", "")

    if not chat_id:
        return

    async with httpx.AsyncClient(timeout=10) as cli:
        await cli.post(f"{CORE_URL}/incoming", json={
            "adapter": ADAPTER_NAME,
            "peer_id": chat_id,
            "msg_id":  msg_id,
            "text":    text,
            "name":    name,
            "phone":   phone,
        })


def _sync_handler(type_webhook: str, body: dict) -> None:
    """Синхронный обработчик, вызываемый из потока polling."""
    if type_webhook == "incomingMessageReceived" and _event_loop:
        asyncio.run_coroutine_threadsafe(_push_incoming(body), _event_loop)


async def _start_polling() -> None:
    global _state, _greenapi, _event_loop, _polling_active

    _event_loop = asyncio.get_event_loop()
    try:
        _greenapi = _make_client()

        # Проверяем состояние инстанса
        state_resp = await asyncio.to_thread(lambda: _greenapi.account.getStateInstance())
        instance_state = (state_resp.data or {}).get("stateInstance", "")

        if instance_state != "authorized":
            _state = "needs_auth"
            return

        _state = "connected"
        _polling_active = True

        # Blocking poll — завершится когда stopReceivingNotifications будет вызван
        await asyncio.to_thread(
            _greenapi.webhooks.startReceivingNotifications, _sync_handler
        )
    except Exception:
        _state = "unavailable"
    finally:
        _polling_active = False


async def _supervisor() -> None:
    """Перезапускает polling при падении или после logout."""
    while True:
        if _configured() and not _polling_active:
            await _start_polling()
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(_supervisor())


# ── Эндпоинты ──────────────────────────────────────────────────

@app.get("/status")
def status():
    return {"state": _state}


@app.post("/webhook")
async def webhook(req: Request):
    """Принимает события от GREEN-API (если вебхук настроен в кабинете)."""
    body = await req.json()
    if body.get("typeWebhook") == "incomingMessageReceived":
        await _push_incoming(body)
    return {"ok": True}


@app.post("/send")
async def send(req: Request):
    body    = await req.json()
    chat_id = str(body["peer_id"])
    text    = body.get("text", "")

    if not _configured():
        return JSONResponse({"error": "GREEN-API не настроен — укажите GREENAPI_ID_INSTANCE и GREENAPI_TOKEN"}, status_code=503)
    if _state != "connected" or not _greenapi:
        return JSONResponse({"error": f"не подключён (state={_state})"}, status_code=503)

    try:
        resp = await asyncio.to_thread(lambda: _greenapi.sending.sendMessage(chat_id, text))
        return {"ok": True, "idMessage": (resp.data or {}).get("idMessage")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/reconnect")
async def reconnect():
    global _state, _greenapi

    if not _configured():
        _state = "needs_auth"
        return {"ok": False, "state": _state}

    try:
        client = _make_client()
        state_resp = await asyncio.to_thread(lambda: client.account.getStateInstance())
        instance_state = (state_resp.data or {}).get("stateInstance", "")
        _state = "connected" if instance_state == "authorized" else "needs_auth"
        if _state == "connected":
            _greenapi = client
    except Exception:
        _state = "unavailable"

    return {"ok": _state == "connected", "state": _state}


@app.post("/logout")
async def logout():
    global _state, _greenapi

    if _greenapi and _polling_active:
        try:
            await asyncio.to_thread(_greenapi.webhooks.stopReceivingNotifications)
        except Exception:
            pass

    _greenapi = None
    _state = "needs_auth"
    return {"ok": True}


# Stub-эндпоинты для совместимости с UI (MAX не использует телефон/код)
@app.post("/login")
async def login():
    return {"ok": True, "state": _state}

@app.post("/code")
async def code():
    return {"ok": True, "state": _state}

@app.post("/password")
async def password():
    return {"ok": True, "state": _state}
