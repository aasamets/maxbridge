"""
Разовая регистрация трёх коннекторов в Битрикс24.
Запускать ОДИН РАЗ после первичной OAuth-авторизации.

  python install_connector.py

Регистрирует коннекторы только для включённых адаптеров:
  maxbridge_wa   — WhatsApp
  maxbridge_max  — MAX
  maxbridge_tg   — Telegram

После выполнения зайди в Битрикс:
  CRM → Контакт-центр → привяжи появившиеся коннекторы к линии.
Настрой линию: очередь операторов (только продавцы) + направление ответственному.
"""

import base64
import os
from dotenv import load_dotenv
from core import store, bitrix

load_dotenv()
store.init()

LINE_ID    = int(os.environ["B24_LINE_ID"])
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")


def _svg(path_d: str) -> str:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           f'<path fill="#fff" d="{path_d}"/></svg>')
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# Иконки мессенджеров (упрощённые силуэты)
_ICON_WA = _svg(
    "M16 3C9.4 3 4 8.4 4 15c0 2.4.7 4.6 1.8 6.5L4 29l7.7-2c1.8.9 3.8 1.4 6 1.4 "
    "6.6 0 12-5.4 12-12S22.6 3 16 3zm6 17.4c-.3.8-1.6 1.5-2.2 1.6-.6.1-1.1.3-3.7-.8"
    "-3.1-1.3-5.1-4.5-5.3-4.7-.2-.2-1.2-1.6-1.2-3.1s.8-2.2 1.1-2.5c.3-.3.6-.4.8-.4h"
    ".6c.2 0 .4.1.6.6l.9 2.2c.1.3.2.6 0 1s-.3.6-.6.9c-.3.3-.5.6-.2 1.2.3.5 1.4 2.3 "
    "3 3.7 2.1 1.8 3.8 2.4 4.3 2.6.5.3.8.2 1.1-.1.3-.4 1.3-1.6 1.6-2.1.3-.5.7-.4 "
    "1.2-.2.5.2 3.1 1.5 3.6 1.7.5.3.8.4 1 .6.1.2.1 1.2-.2 2z"
)
_ICON_TG = _svg(
    "M27.6 5.3L3.2 14.6c-1.7.7-1.7 1.7-.3 2.1l6.2 1.9 14.4-9.1c.7-.4 1.3-.2.8.3"
    "L11.4 19.8l-.4 6.5c.6 0 .8-.3 1.1-.5l2.7-2.6 5.5 4.1c1 .6 1.7.3 2-.9l3.6-17"
    "c.3-1.5-.6-2.2-1.8-1.5z"
)
_ICON_MAX = _svg(
    "M4 27V7l5.5 7L16 7l6.5 7L28 7v20h-3V15l-3.5 4.5L16 13l-5.5 6.5L7 15v12z"
)
_ICON_FALLBACK = "data:image/svg+xml;charset=US-ASCII,<svg xmlns='http://www.w3.org/2000/svg'/>"

_CONNECTORS = [
    {
        "env":   "WA_ENABLED",
        "id":    "wa",
        "name":  "WhatsApp",
        "color": "#25d366",
        "icon":  _ICON_WA,
    },
    {
        "env":   "MAX_ENABLED",
        "id":    "max",
        "name":  "MAX",
        "color": "#0077ff",
        "icon":  _ICON_MAX,
    },
    {
        "env":   "TG_ENABLED",
        "id":    "tg",
        "name":  "Telegram",
        "color": "#2aabee",
        "icon":  _ICON_TG,
    },
]


def _connector_url(suffix: str) -> str:
    """URL для кнопки в виджете Битрикс24 — глубокая ссылка на мессенджер."""
    if suffix == "wa":
        phone = store.kv_get("whatsapp_phone") or os.environ.get("WA_PHONE", "")
        if phone:
            clean = phone.lstrip("+")
            return f"https://wa.me/{clean}"
    if suffix == "max":
        phone = store.kv_get("max_phone") or os.environ.get("MAX_PHONE", "")
        if phone:
            clean = phone.lstrip("+")
            return f"https://vk.me/+{clean}"
    if suffix == "tg":
        # Telegram userbot не имеет публичного username — просто ссылку не даём
        return PUBLIC_URL
    return PUBLIC_URL


def register_one(suffix: str, name: str, color: str, icon: str) -> None:
    connector_id = f"{os.environ.get('B24_CONNECTOR_ID', 'maxbridge')}_{suffix}"
    print(f"  Регистрация {connector_id} ({name})…")

    bitrix.call("imconnector.register", {
        "ID":   connector_id,
        "NAME": f"MaxBridge {name}",
        "ICON": {
            "DATA_IMAGE": icon, "COLOR": color,
            "SIZE": "100%", "POSITION": "center",
        },
        "ICON_DISABLED": {
            "DATA_IMAGE": icon, "COLOR": "#99adb3",
            "SIZE": "100%", "POSITION": "center",
        },
        "PLACEMENT_HANDLER": f"{PUBLIC_URL}/bitrix/app",
    })

    try:
        bitrix.call("event.bind", {
            "event":   "OnImConnectorMessageAdd",
            "handler": f"{PUBLIC_URL}/bitrix/events",
        })
    except RuntimeError as e:
        if "already" in str(e).lower():
            print(f"    event.bind: уже привязан (ok)")
        else:
            raise

    bitrix.call("imconnector.activate", {
        "CONNECTOR": connector_id,
        "LINE":      LINE_ID,
        "ACTIVE":    1,
    })

    url = _connector_url(suffix)
    try:
        bitrix.call("imconnector.connector.data.set", {
            "CONNECTOR": connector_id,
            "LINE":      LINE_ID,
            "DATA":      {"id": connector_id, "name": f"MaxBridge {name}", "url": url},
        })
        if url != PUBLIC_URL:
            print(f"    Ссылка виджета: {url}")
    except Exception as e:
        print(f"    data.set предупреждение: {e}")

    print(f"  ✔ {name} зарегистрирован")


def main() -> None:
    enabled = [c for c in _CONNECTORS
               if os.environ.get(c["env"], "true").lower() == "true"]

    if not enabled:
        print("Ни один адаптер не включён. Проверь .env (WA_ENABLED / MAX_ENABLED / TG_ENABLED).")
        return

    print(f"Регистрация {len(enabled)} коннектора(ов) на линии #{LINE_ID}…\n")
    for c in enabled:
        register_one(c["id"], c["name"], c["color"], c["icon"])

    print("\nГотово.")
    print("Следующий шаг: CRM → Контакт-центр → привяжи коннекторы к своей линии.")
    print("Настрой очередь операторов и направление ответственному в настройках линии.")


if __name__ == "__main__":
    main()
