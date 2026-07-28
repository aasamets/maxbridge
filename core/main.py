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

_PUBLIC_PATHS = frozenset(["/login", "/bitrix/events", "/bitrix/install", "/bitrix/oauth",
                           "/incoming", "/bitrix/app", "/api/adapter_phone", "/bitrix/message"])
_PUBLIC_PREFIXES = ("/static", "/adapters/max/webhook")

LINE_ID = int(os.environ.get("B24_LINE_ID", "0"))
NTFY_URL = os.environ.get("NTFY_URL", "")

_PROXY_SOCKS = "socks5://xray:1080"
_PROXY_TEST_URL = "https://www.youtube.com"
_proxy_status: dict = {"state": "checking", "ok": None, "latency_ms": None}
_prev_adapter_states: dict[str, str] = {}
_adapter_phones: dict[str, str] = {}

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
        if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
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


async def _ntfy_send(title: str, message: str, priority: str = "default") -> None:
    if not NTFY_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as cli:
            await cli.post(NTFY_URL, json={
                "message": message,
                "title": title,
                "priority": priority,
                "tags": ["satellite"],
            })
    except Exception:
        pass


async def _poll_adapters() -> None:
    """Каждые 5 секунд опрашивает адаптеры, рассылает статусы через WS, шлёт ntfy-алерты."""
    global _prev_adapter_states
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
            prev = _prev_adapter_states.get(name)
            if prev is not None and prev != state:
                svc = {"whatsapp": "wa", "max": "max", "telegram": "telegram"}.get(name, name)
                if state == "unavailable":
                    asyncio.create_task(_ntfy_send(
                        f"MaxBridge: {name} недоступен",
                        f"Адаптер {name} не отвечает. Проверьте: docker compose logs {svc}",
                        "high",
                    ))
                elif state == "connected" and prev != "connected":
                    asyncio.create_task(_ntfy_send(
                        f"MaxBridge: {name} подключён",
                        f"Адаптер {name} снова работает нормально.",
                    ))

        _prev_adapter_states = dict(statuses)
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
    global NTFY_URL
    kv_ntfy = store.kv_get("ntfy_url")
    if kv_ntfy is not None:
        NTFY_URL = kv_ntfy
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
    content = (_STATIC / "index.html").read_text()
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})


# ── API статусов ───────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    return store.get_adapter_states()


@app.get("/api/proxy/status")
async def api_proxy_status():
    return _proxy_status


@app.get("/api/b24/status")
async def api_b24_status():
    """Проверяет есть ли живые токены Битрикс24 в DB (без сетевого запроса)."""
    token = store.kv_get("b24_access_token")
    refresh = store.kv_get("b24_refresh_token")
    if token and refresh:
        return {"state": "authorized"}
    return {"state": "needs_auth"}


# ── API настроек ───────────────────────────────────────────────
_EXPOSED_SETTINGS = [
    "PUBLIC_URL", "B24_DOMAIN", "B24_CLIENT_ID", "B24_LINE_ID", "B24_CONNECTOR_ID",
    "TG_ENABLED", "WA_ENABLED", "MAX_ENABLED",
    "TG_API_ID", "TG_API_HASH",
    "TG_PROXY_HOST", "TG_PROXY_PORT",
    "VLESS_URL",
]
# Видны в UI как ***, обновляются только если пришло не "***"
_EDITABLE_SECRETS = {"B24_CLIENT_SECRET"}
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


# ── Номера телефонов адаптеров (авто-определение) ─────────────
@app.post("/api/adapter_phone")
async def set_adapter_phone(req: Request):
    """Адаптер сообщает свой номер после успешного подключения."""
    body    = await req.json()
    adapter = str(body.get("adapter", ""))
    phone   = str(body.get("phone", ""))
    if adapter and phone:
        store.kv_set(f"{adapter}_phone", phone)
        _adapter_phones[adapter] = phone
        await _broadcast({"type": "phone", "adapter": adapter, "phone": phone})
        print(f"[core] {adapter} phone: {phone}")
    return {"ok": True}


@app.get("/api/ntfy")
async def get_ntfy():
    return {"url": NTFY_URL}


@app.post("/api/ntfy")
async def save_ntfy(req: Request):
    global NTFY_URL
    body = await req.json()
    url = str(body.get("url", "")).strip()
    NTFY_URL = url
    store.kv_set("ntfy_url", url)
    return {"ok": True}


@app.post("/api/ntfy/test")
async def test_ntfy():
    if not NTFY_URL:
        return JSONResponse({"ok": False, "error": "ntfy URL не задан"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=8) as cli:
            r = await cli.post(NTFY_URL, json={
                "message": "Уведомления работают корректно ✓",
                "title": "MaxBridge: тест",
                "priority": "default",
                "tags": ["satellite"],
            })
        if r.status_code >= 300:
            return JSONResponse(
                {"ok": False, "error": f"ntfy вернул {r.status_code}: {r.text[:120]}"},
                status_code=502,
            )
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)


@app.get("/api/phones")
async def get_phones():
    result: dict[str, str] = {}
    for name in ADAPTERS:
        p = store.kv_get(f"{name}_phone")
        if p:
            result[name] = p
    return result


@app.get("/bitrix/app", response_class=HTMLResponse)
async def bitrix_app():
    """Страница для Bitrix24 iframe (PLACEMENT_HANDLER). Без авторизации."""
    states = store.get_adapter_states()
    _NAMES  = {"whatsapp": "WhatsApp", "max": "MAX", "telegram": "Telegram"}
    _DOTS   = {"connected": "#34c759", "needs_auth": "#ff9f0a"}
    _LABELS = {"connected": "Подключён", "needs_auth": "Ожидает авторизации",
               "unavailable": "Недоступен", "unknown": "Неизвестно"}

    rows = []
    for adapter, state_val in states.items():
        dot   = _DOTS.get(state_val, "#8e8e93")
        label = _LABELS.get(state_val, state_val)
        name  = _NAMES.get(adapter, adapter)
        phone = store.kv_get(f"{adapter}_phone") or ""
        phone_span = f" <span style='color:#8e8e93'>· {phone}</span>" if phone else ""
        rows.append(
            f'<div class="row">'
            f'<div class="dot" style="background:{dot}"></div>'
            f'<span class="name">{name}{phone_span}</span>'
            f'<span class="status">{label}</span>'
            f'</div>'
        )

    rows_html = "\n".join(rows) if rows else "<p class='empty'>Адаптеры не настроены</p>"
    html = f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MaxBridge</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:16px;background:#f5f5f7;color:#1d1d1f;font-size:14px}}
h3{{font-size:15px;font-weight:600;margin:0 0 12px}}
.row{{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#fff;border-radius:10px;margin-bottom:8px;box-shadow:0 1px 4px rgba(0,0,0,.06)}}
.dot{{width:8px;height:8px;border-radius:50%;flex-shrink:0}}
.name{{font-weight:500}}
.status{{margin-left:auto;color:#8e8e93;font-size:12px}}
.empty{{color:#8e8e93;font-size:13px}}
.footer{{margin-top:14px;font-size:12px;color:#8e8e93;text-align:center}}
a{{color:#3d5c82;text-decoration:none}}
</style></head>
<body>
<h3>MaxBridge</h3>
{rows_html}
<div class="footer"><a href="/" target="_blank">Открыть панель управления</a></div>
</body></html>"""
    return HTMLResponse(content=html)


# ── Исходящие из CRM (messageservice handler) ─────────────────
@app.post("/bitrix/message")
async def bitrix_message_handler(req: Request):
    """
    Битрикс24 вызывает этот URL когда менеджер отправляет сообщение
    из карточки CRM (кнопка «Сообщение» → выбор MaxBridge WA/MAX).
    Поля формы: code, message_to (телефон), message_body, message_id.
    """
    form    = await req.form()
    code    = str(form.get("code", ""))
    phone   = str(form.get("message_to", "")).strip()
    text    = str(form.get("message_body", "")).strip()
    msg_id  = str(form.get("message_id", ""))

    if not phone or not text:
        return JSONResponse({"status": "error", "error": "empty phone or text"}, status_code=400)

    # Определяем адаптер по коду провайдера
    if "_wa" in code:
        adapter_name = "whatsapp"
        peer_id      = phone  # WA-адаптер сам конвертирует в @s.whatsapp.net
    elif "_max" in code:
        adapter_name = "max"
        # MAX не поддерживает отправку по телефону незнакомому — ищем в chat_map
        peer_id = store.find_peer_by_phone("max", phone)
        if not peer_id:
            return JSONResponse(
                {"status": "error",
                 "error": f"MAX: клиент с номером {phone} ещё не писал нам — не можем инициировать диалог"},
                status_code=400,
            )
    else:
        return JSONResponse({"status": "error", "error": f"unknown provider code: {code}"}, status_code=400)

    adapter_url = ADAPTERS.get(adapter_name)
    if not adapter_url:
        return JSONResponse({"status": "error", "error": f"адаптер {adapter_name} не настроен"}, status_code=503)

    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{adapter_url}/send", json={"peer_id": peer_id, "text": text})

    if r.status_code != 200:
        return JSONResponse({"status": "error", "error": r.text}, status_code=502)

    # Подтверждение доставки (игнорируем ошибки — основная отправка уже прошла)
    if msg_id:
        try:
            bitrix.call("messageservice.message.status.update", {
                "CODE":       code,
                "MESSAGE_ID": msg_id,
                "STATUS":     "delivered",
            })
        except Exception:
            pass

    print(f"[core] msgservice → {adapter_name} phone={phone} text={text[:40]!r}")
    return {"status": "success"}


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
