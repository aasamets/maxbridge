"""
Ядро-релей. Маршруты:
  POST /incoming          — адаптер присылает входящее от клиента
  POST /bitrix/events     — Битрикс присылает событие (ответ оператора)
  GET  /bitrix/oauth      — OAuth-callback после авторизации приложения
  GET  /ws                — WebSocket: push статусов адаптеров в UI
  GET  /api/status        — JSON статусы всех адаптеров
  GET  /api/settings      — JSON текущих настроек (без секретов)
  POST /api/settings      — сохранить изменённые настройки в .env
  GET  /api/oauth_url     — ссылка для OAuth-авторизации в Битрикс
  GET  /                  — веб-морда (SPA)

Запуск:
  uvicorn core.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from dotenv import load_dotenv
from . import store, bitrix

load_dotenv()
store.init()

# ── Сессионная аутентификация ──────────────────────────────────
_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
_SESSION_TTL = 8 * 3600  # 8 часов
_sessions: dict[str, float] = {}  # token → expiry

_PUBLIC_PATHS = frozenset(["/login", "/bitrix/events", "/bitrix/install", "/bitrix/oauth"])
_PUBLIC_PREFIXES = ("/static", "/adapters/max/webhook")

LINE_ID = int(os.environ.get("B24_LINE_ID", "0"))

_PROXY_SOCKS = "socks5://xray:1080"
_PROXY_TEST_URL = "https://www.youtube.com"
_proxy_status: dict = {"state": "checking", "ok": None, "latency_ms": None}

ADAPTERS: dict[str, str] = {}
for _pair in os.environ.get("ADAPTERS", "").split(","):
    _pair = _pair.strip()
    if "=" in _pair:
        _n, _u = _pair.split("=", 1)
        ADAPTERS[_n.strip()] = _u.strip().rstrip("/")

_ENV_PATH = Path("/app/.env")
_STATIC   = Path(__file__).parent / "static"

def _valid_session(token: str | None) -> bool:
    if not token or token not in _sessions:
        return False
    if _sessions[token] < time.time():
        del _sessions[token]
        return False
    _sessions[token] = time.time() + _SESSION_TTL
    return True


class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)
        if not _valid_session(request.cookies.get("mb_session")):
            if request.headers.get("upgrade", "").lower() == "websocket":
                return Response(status_code=401)
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = FastAPI(title="MaxBridge")
app.add_middleware(_AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# ── WebSocket-клиенты ──────────────────────────────────────────
_ws_clients: set[WebSocket] = set()


async def _broadcast(data: dict) -> None:
    dead = set()
    for ws in _ws_clients:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)


async def _poll_adapters() -> None:
    """Каждые 5 секунд опрашивает адаптеры и рассылает статусы через WebSocket."""
    while True:
        statuses: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=5) as cli:
            for name, url in ADAPTERS.items():
                try:
                    r = await cli.get(f"{url}/status")
                    statuses[name] = r.json().get("state", "unknown")
                except Exception:
                    statuses[name] = "unavailable"
        for name, state in statuses.items():
            store.set_adapter_state(name, state)
        await _broadcast({"type": "status", "adapters": statuses})
        await asyncio.sleep(5)


async def _poll_proxy() -> None:
    """Каждые 30 секунд проверяет доступность VLESS-прокси через SOCKS5 (xray:1080)."""
    global _proxy_status
    if not os.environ.get("VLESS_URL"):
        _proxy_status = {"state": "disabled", "ok": None, "latency_ms": None}
        await _broadcast({"type": "proxy", "status": _proxy_status})
        return
    while True:
        t0 = time.time()
        try:
            async with httpx.AsyncClient(proxy=_PROXY_SOCKS, timeout=8) as cli:
                r = await cli.get(_PROXY_TEST_URL, follow_redirects=True)
                ok = r.status_code < 500
        except Exception as e:
            ok = False
            _proxy_status = {"state": "error", "ok": False, "latency_ms": None,
                             "error": str(e)[:100]}
        else:
            latency = int((time.time() - t0) * 1000)
            _proxy_status = {"state": "ok" if ok else "error", "ok": ok,
                             "latency_ms": latency if ok else None}
        await _broadcast({"type": "proxy", "status": _proxy_status})
        await asyncio.sleep(30)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_poll_adapters())
    asyncio.create_task(_poll_proxy())


# ── WebSocket ──────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.add(websocket)
    # сразу отдать кешированные состояния
    await websocket.send_text(json.dumps({
        "type": "status",
        "adapters": store.get_adapter_states(),
    }))
    await websocket.send_text(json.dumps({"type": "proxy", "status": _proxy_status}))
    try:
        while True:
            await websocket.receive_text()  # держим соединение живым
    except WebSocketDisconnect:
        _ws_clients.discard(websocket)


# ── Аутентификация ────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return (_STATIC / "login.html").read_text()


@app.post("/login")
async def login(req: Request):
    form = await req.form()
    user = str(form.get("username", ""))
    pwd  = str(form.get("password", ""))
    if user == _ADMIN_USER and pwd == _ADMIN_PASS and _ADMIN_PASS:
        token = secrets.token_urlsafe(32)
        _sessions[token] = time.time() + _SESSION_TTL
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("mb_session", token, httponly=True, samesite="lax",
                        max_age=_SESSION_TTL)
        return resp
    return RedirectResponse("/login?error=1", status_code=303)


@app.post("/logout")
async def logout(req: Request):
    token = req.cookies.get("mb_session")
    if token:
        _sessions.pop(token, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("mb_session")
    return resp


# ── SPA ───────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return (_STATIC / "index.html").read_text()


# ── API статусов ───────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    return store.get_adapter_states()


@app.get("/api/proxy/status")
async def api_proxy_status():
    return _proxy_status


# ── API настроек ───────────────────────────────────────────────
_EXPOSED_SETTINGS = [
    "PUBLIC_URL", "B24_DOMAIN", "B24_CLIENT_ID", "B24_LINE_ID", "B24_CONNECTOR_ID",
    "TG_ENABLED", "WA_ENABLED", "MAX_ENABLED",
    "TG_API_ID", "TG_API_HASH",
    "TG_PROXY_HOST", "TG_PROXY_PORT",
    "VLESS_URL",
    "GREENAPI_ID_INSTANCE", "GREENAPI_WEBHOOK_URL",
]
# Видны в UI как ***, обновляются только если пришло не "***"
_EDITABLE_SECRETS = {"B24_CLIENT_SECRET", "GREENAPI_TOKEN"}
# Не передаются в UI вообще
_READONLY_KEYS = {"ADMIN_PASS_HASH"}


@app.get("/api/settings")
async def get_settings():
    env = _read_env_file()
    result = {k: env.get(k, "") for k in _EXPOSED_SETTINGS}
    for k in _EDITABLE_SECRETS:
        result[k] = "***" if env.get(k) else ""
    return result


@app.post("/api/settings")
async def save_settings(req: Request):
    body = await req.json()
    env = _read_env_file()
    allowed = set(_EXPOSED_SETTINGS) | _EDITABLE_SECRETS
    for k, v in body.items():
        if k not in allowed or k in _READONLY_KEYS:
            continue
        if k in _EDITABLE_SECRETS and v == "***":
            continue  # пользователь не менял секрет — не трогаем
        env[k] = str(v)
    _write_env_file(env)
    return {"ok": True, "note": "Перезапустите затронутые сервисы: docker compose restart"}


# ── OAuth Битрикс ──────────────────────────────────────────────
@app.get("/api/oauth_url")
async def oauth_url():
    return {"url": bitrix.oauth_url()}


@app.get("/bitrix/install")
async def bitrix_install(code: str | None = None, error: str | None = None):
    if code:
        # Битрикс иногда шлёт OAuth-код на /bitrix/install
        return await _handle_oauth_code(code, error, "/bitrix/install")
    return {"ok": True, "hint": "Приложение установлено. Авторизуйте его по ссылке /api/oauth_url"}


@app.get("/bitrix/oauth")
async def bitrix_oauth(code: str | None = None, error: str | None = None):
    return await _handle_oauth_code(code, error, "/bitrix/oauth")


@app.get("/bitrix/events")
async def bitrix_events_get(code: str | None = None, error: str | None = None):
    """GET /bitrix/events — Битрикс шлёт OAuth-код сюда если так прописан путь в настройках приложения."""
    if not code:
        return {"ok": True}
    return await _handle_oauth_code(code, error, "/bitrix/events")


async def _handle_oauth_code(code: str | None, error: str | None, path: str):
    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code:
        return JSONResponse({"error": "no code"}, status_code=400)
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    try:
        bitrix.exchange_code(code, redirect_uri=f"{base}{path}")
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return RedirectResponse("/?oauth=ok")


# ── Входящие (клиент → Битрикс) ───────────────────────────────
@app.post("/incoming")
async def incoming(req: Request):
    body = await req.json()
    adapter  = body["adapter"]
    peer_id  = str(body["peer_id"])
    msg_id   = str(body["msg_id"])

    if store.already_seen(adapter, msg_id):
        return {"ok": True, "skipped": "duplicate"}

    external_chat_id = store.remember_chat(
        adapter, peer_id, body.get("phone"), body.get("name")
    )
    connector = bitrix.connector_id_for(adapter)

    bitrix.send_incoming_message(
        connector_id=connector,
        line_id=LINE_ID,
        external_chat_id=external_chat_id,
        peer_id=peer_id,
        text=body.get("text", ""),
        msg_external_id=msg_id,
        peer_name=body.get("name"),
        peer_phone=body.get("phone"),
        files=body.get("files"),
    )
    return {"ok": True, "external_chat_id": external_chat_id}


# ── Исходящие (Битрикс → клиент) ──────────────────────────────
@app.post("/bitrix/events")
async def bitrix_events(req: Request):
    form    = await req.form()
    payload = dict(form)

    auth = {k.split("[", 1)[1].rstrip("]"): v
            for k, v in payload.items() if k.startswith("auth[")}
    bitrix.save_tokens_from_event(auth)

    app_token = bitrix.get_application_token()
    if app_token and auth.get("application_token") != app_token:
        return JSONResponse({"error": "bad application_token"}, status_code=401)

    event = (payload.get("event") or "").upper()
    if event != "ONIMCONNECTORMESSAGEADD":
        return {"ok": True, "ignored": event}

    messages = _parse_outgoing_messages(payload)
    for m in messages:
        external_chat_id = m.get("chat_id")
        text             = m.get("text", "")
        b24_msg_id       = m.get("b24_message_id")
        if not external_chat_id or not text:
            continue

        target = store.resolve_chat(external_chat_id)
        if not target:
            continue

        adapter_url = ADAPTERS.get(target["adapter"])
        if not adapter_url:
            continue

        async with httpx.AsyncClient(timeout=30) as cli:
            await cli.post(f"{adapter_url}/send",
                           json={"peer_id": target["peer_id"], "text": _strip_bb(text)})

        connector = bitrix.connector_id_for(target["adapter"])
        try:
            bitrix.confirm_delivery(connector, LINE_ID, external_chat_id, b24_msg_id)
        except Exception:
            pass

    return {"ok": True}


# ── Прокси к адаптерам ────────────────────────────────────────
@app.get("/adapters/{name}/qr")
async def adapter_qr(name: str):
    return await _proxy(name, "/qr", "GET")


@app.post("/adapters/{name}/login")
async def adapter_login(name: str, req: Request):
    form = await req.form()
    return await _proxy(name, "/login", "POST", data=dict(form))


@app.post("/adapters/{name}/code")
async def adapter_code(name: str, req: Request):
    form = await req.form()
    return await _proxy(name, "/code", "POST", data=dict(form))


@app.post("/adapters/{name}/password")
async def adapter_password(name: str, req: Request):
    form = await req.form()
    return await _proxy(name, "/password", "POST", data=dict(form))


@app.post("/adapters/{name}/logout")
async def adapter_logout(name: str):
    return await _proxy(name, "/logout", "POST")


@app.post("/adapters/{name}/reconnect")
async def adapter_reconnect(name: str):
    return await _proxy(name, "/reconnect", "POST")


@app.post("/adapters/{name}/webhook")
async def adapter_webhook(name: str, req: Request):
    """Проброс вебхуков (GREEN-API → MAX адаптер)."""
    body = await req.body()
    url = ADAPTERS.get(name)
    if not url:
        return JSONResponse({"error": "unknown adapter"}, status_code=404)
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{url}/webhook",
            content=body,
            headers={"content-type": req.headers.get("content-type", "application/json")},
        )
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"))


async def _proxy(name: str, path: str, method: str, data=None) -> Response:
    url = ADAPTERS.get(name)
    if not url:
        return JSONResponse({"error": "unknown adapter"}, status_code=404)
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await (cli.post(f"{url}{path}", data=data) if method == "POST"
                   else cli.get(f"{url}{path}"))
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"))


# ── Вспомогательные ───────────────────────────────────────────
def _parse_outgoing_messages(payload: dict) -> list[dict]:
    rows: dict[str, dict] = {}
    for key, val in payload.items():
        if not key.startswith("data[MESSAGES]["):
            continue
        try:
            idx = key.split("data[MESSAGES][", 1)[1].split("]", 1)[0]
        except IndexError:
            continue
        row = rows.setdefault(idx, {})
        if key.endswith("[chat][id]"):
            row["chat_id"] = val
        elif key.endswith("[message][text]"):
            row["text"] = val
        elif key.endswith("[im][message_id]"):
            row["b24_message_id"] = val
    return list(rows.values())


def _strip_bb(text: str) -> str:
    text = text.replace("[br]", "\n")
    return re.sub(r"\[/?[a-zA-Z]+(=[^\]]+)?\]", "", text)


def _read_env_file() -> dict[str, str]:
    env: dict[str, str] = {}
    if not _ENV_PATH.exists():
        return env
    for line in _ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env_file(env: dict[str, str]) -> None:
    lines = []
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k in env:
                    lines.append(f"{k}={env.pop(k)}")
                    continue
            lines.append(line)
    for k, v in env.items():
        lines.append(f"{k}={v}")
    _ENV_PATH.write_text("\n".join(lines) + "\n")
