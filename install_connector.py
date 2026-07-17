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

import os
from dotenv import load_dotenv
from core import store, bitrix

load_dotenv()
store.init()

LINE_ID    = int(os.environ["B24_LINE_ID"])
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")

_ICON = "data:image/svg+xml;charset=US-ASCII,<svg xmlns='http://www.w3.org/2000/svg'/>"

_CONNECTORS = [
    {
        "env":   "WA_ENABLED",
        "id":    "wa",
        "name":  "WhatsApp",
        "color": "#25d366",
    },
    {
        "env":   "MAX_ENABLED",
        "id":    "max",
        "name":  "MAX",
        "color": "#0077ff",
    },
    {
        "env":   "TG_ENABLED",
        "id":    "tg",
        "name":  "Telegram",
        "color": "#2aabee",
    },
]


def register_one(suffix: str, name: str, color: str) -> None:
    connector_id = f"{os.environ.get('B24_CONNECTOR_ID', 'maxbridge')}_{suffix}"
    print(f"  Регистрация {connector_id} ({name})…")

    bitrix.call("imconnector.register", {
        "ID":   connector_id,
        "NAME": f"MaxBridge {name}",
        "ICON": {
            "DATA_IMAGE": _ICON, "COLOR": color,
            "SIZE": "100%", "POSITION": "center",
        },
        "ICON_DISABLED": {
            "DATA_IMAGE": _ICON, "COLOR": "#99adb3",
            "SIZE": "100%", "POSITION": "center",
        },
        "PLACEMENT_HANDLER": f"{PUBLIC_URL}/",
    })

    bitrix.call("event.bind", {
        "event":   "OnImConnectorMessageAdd",
        "handler": f"{PUBLIC_URL}/bitrix/events",
    })

    bitrix.call("imconnector.activate", {
        "CONNECTOR": connector_id,
        "LINE":      LINE_ID,
        "ACTIVE":    1,
    })

    try:
        bitrix.call("imconnector.connector.data.set", {
            "CONNECTOR": connector_id,
            "LINE":      LINE_ID,
            "DATA":      {"id": connector_id, "name": f"MaxBridge {name}", "url": PUBLIC_URL},
        })
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
        register_one(c["id"], c["name"], c["color"])

    print("\nГотово.")
    print("Следующий шаг: CRM → Контакт-центр → привяжи коннекторы к своей линии.")
    print("Настрой очередь операторов и направление ответственному в настройках линии.")


if __name__ == "__main__":
    main()
