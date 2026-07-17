"""
Тонкий клиент Битрикс24 для коннектора Открытых линий.

Авторизация через локальное приложение (OAuth). Методы imconnector.*
работают только в контексте приложения — обычный вебхук не подойдёт.

Токены обновляются двумя путями:
  1. Каждое событие от Битрикса приносит свежий access_token — сохраняем.
  2. Если протух — обновляем по refresh_token через oauth.bitrix24.tech.

Три коннектора: {B24_CONNECTOR_ID}_wa / _max / _tg.
Регистрируются только для подключённых адаптеров (вызывает install_connector.py).
"""

import os
import httpx
from . import store

_OAUTH = "https://oauth.bitrix24.tech/oauth/token/"

# Маппинг: имя адаптера → суффикс connector_id
ADAPTER_CONNECTOR_SUFFIX = {
    "whatsapp": "wa",
    "max":      "max",
    "telegram": "tg",
}


def connector_id_for(adapter: str) -> str:
    base = os.environ.get("B24_CONNECTOR_ID", "maxbridge")
    suffix = ADAPTER_CONNECTOR_SUFFIX.get(adapter, adapter)
    return f"{base}_{suffix}"


def _domain() -> str:
    return os.environ["B24_DOMAIN"].strip().rstrip("/")


def _rest_url(method: str) -> str:
    return f"https://{_domain()}/rest/{method}.json"


def save_tokens_from_event(auth: dict) -> None:
    if not auth:
        return
    if auth.get("access_token"):
        store.kv_set("b24_access_token", auth["access_token"])
    if auth.get("refresh_token"):
        store.kv_set("b24_refresh_token", auth["refresh_token"])
    # Автоматически сохраняем application_token если ещё не знаем его
    if auth.get("application_token") and not store.kv_get("b24_application_token"):
        store.kv_set("b24_application_token", auth["application_token"])


def get_application_token() -> str | None:
    return store.kv_get("b24_application_token") or os.environ.get("B24_APPLICATION_TOKEN")


def _current_access_token() -> str | None:
    return store.kv_get("b24_access_token")


def _refresh_token() -> str:
    return store.kv_get("b24_refresh_token") or os.environ.get("B24_REFRESH_TOKEN", "")


def _do_refresh() -> str:
    params = {
        "grant_type":    "refresh_token",
        "client_id":     os.environ["B24_CLIENT_ID"],
        "client_secret": os.environ["B24_CLIENT_SECRET"],
        "refresh_token": _refresh_token(),
    }
    r = httpx.get(_OAUTH, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    store.kv_set("b24_access_token",  data["access_token"])
    store.kv_set("b24_refresh_token", data["refresh_token"])
    return data["access_token"]


def oauth_url() -> str:
    """URL для первичной OAuth-авторизации приложения (открыть в браузере один раз)."""
    domain = _domain()
    client_id = os.environ["B24_CLIENT_ID"]
    redirect = os.environ.get("PUBLIC_URL", "").rstrip("/") + "/bitrix/oauth"
    return (
        f"https://{domain}/oauth/authorize/"
        f"?client_id={client_id}&response_type=code&redirect_uri={redirect}"
    )


def exchange_code(code: str, redirect_uri: str | None = None) -> None:
    """Обменять OAuth-code на токены. redirect_uri должен совпадать с тем, куда Битрикс отправил код."""
    if redirect_uri is None:
        redirect_uri = os.environ.get("PUBLIC_URL", "").rstrip("/") + "/bitrix/oauth"
    params = {
        "grant_type":    "authorization_code",
        "client_id":     os.environ["B24_CLIENT_ID"],
        "client_secret": os.environ["B24_CLIENT_SECRET"],
        "code":          code,
        "redirect_uri":  redirect_uri,
    }
    r = httpx.get(_OAUTH, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    store.kv_set("b24_access_token",  data["access_token"])
    store.kv_set("b24_refresh_token", data["refresh_token"])


def call(method: str, payload: dict) -> dict:
    token = _current_access_token() or _do_refresh()

    def _post(tok: str) -> httpx.Response:
        body = dict(payload, auth=tok)
        return httpx.post(_rest_url(method), json=body, timeout=30)

    resp = _post(token)
    data = resp.json()
    if isinstance(data, dict) and data.get("error") in (
        "expired_token", "invalid_token", "NO_AUTH_FOUND",
    ):
        token = _do_refresh()
        resp = _post(token)
        data = resp.json()

    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(
            f"Bitrix {method}: {data.get('error')} {data.get('error_description')}"
        )
    return data.get("result", data)


def send_incoming_message(
    connector_id: str, line_id: int, external_chat_id: str,
    peer_id: str, text: str, msg_external_id: str,
    peer_name: str | None = None, peer_phone: str | None = None,
    files: list[dict] | None = None,
) -> dict:
    user = {"id": str(peer_id), "name": peer_name or f"Клиент {peer_id}"}
    if peer_phone:
        user["phone"] = peer_phone
        user["skip_phone_validate"] = "Y"

    message = {"id": str(msg_external_id), "text": text, "disable_crm": "N"}
    if files:
        message["files"] = files

    return call("imconnector.send.messages", {
        "CONNECTOR": connector_id,
        "LINE":      line_id,
        "MESSAGES":  [{"user": user, "message": message, "chat": {"id": external_chat_id}}],
    })


def confirm_delivery(connector_id: str, line_id: int,
                     external_chat_id: str, b24_message_id) -> None:
    call("imconnector.send.status.delivery", {
        "CONNECTOR": connector_id,
        "LINE":      line_id,
        "MESSAGES":  [{"chat": {"id": external_chat_id},
                       "message": {"id": [str(b24_message_id)]}}],
    })
