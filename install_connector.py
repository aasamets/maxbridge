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


def _b64svg(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# WhatsApp: чистый белый глиф трубки в viewBox 24×24, без собственного фона.
# Битрикс кладёт глиф поверх COLOR-фона (#25D366), SIZE=80%, center.
_ICON_WA = _b64svg(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="#fff" d="M12 2C6.48 2 2 6.48 2 12c0 1.85.5 3.58 1.37 5.07L2 22l5.08-1.33'
    'A9.93 9.93 0 0 0 12 22c5.52 0 10-4.48 10-10S17.52 2 12 2zm5.16 13.73c-.22.62-1.3 1.18'
    '-1.79 1.25-.46.07-.87.22-2.9-.61-2.43-1.01-3.99-3.51-4.11-3.67-.12-.16-.97-1.28-.97'
    '-2.45 0-1.16.61-1.73.83-1.97.22-.23.48-.29.64-.29h.46c.15 0 .35.06.54.51l.77 1.82'
    'c.08.19.04.41-.09.61l-.46.67c-.15.22-.31.46-.13.9.18.44 1.1 1.81 2.37 2.93 1.63 1.42'
    ' 3 1.87 3.41 2.08.41.21.65.18.89-.1.24-.28 1.01-1.19 1.28-1.6.27-.41.54-.34.91-.2'
    '.37.14 2.34 1.1 2.74 1.3.4.21.67.31.76.48.1.17.1.97-.12 1.59z"/>'
    '</svg>'
)

# Telegram: белый глиф самолётика в viewBox 24×24.
_ICON_TG = _b64svg(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<path fill="#fff" d="M20.665 3.717l-17.73 6.837c-1.21.486-1.203 1.161-.222 1.462'
    'l4.552 1.42 10.532-6.645c.498-.303.953-.14.579.192L9.116 16.93l-.396 4.602'
    'c.58 0 .833-.25.997-.408l2.396-2.32 4.985 3.682c.92.507 1.58.245 1.81-.852'
    'l3.279-15.44c.329-1.316-.498-1.912-1.522-1.477z"/>'
    '</svg>'
)

# MAX: официальный SVG логотип адаптирован для модели Битрикса.
# Оригинал имеет собственный цветной фон (rect с градиентом) — убираем rect,
# оставляем только белый path (буква O/кружок). Битрикс добавит фон через COLOR.
_ICON_MAX = _b64svg(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">'
    '<path fill="#fff" fill-rule="evenodd" d="M508.211 878.328c-75.007 0-109.864-10.95'
    '-170.453-54.75-38.325 49.275-159.686 87.783-164.979 21.9 0-49.456-10.95-91.248'
    '-23.36-136.873-14.782-56.21-31.572-118.807-31.572-209.508 0-216.626 177.754'
    '-379.597 388.357-379.597 210.785 0 375.947 171.001 375.947 381.604.707 207.346'
    '-166.595 376.118-373.94 377.224m3.103-571.585c-102.564-5.292-182.499 65.7'
    '-200.201 177.024-14.6 92.162 11.315 204.398 33.397 210.238 10.585 2.555'
    ' 37.23-18.98 53.837-35.587a189.8 189.8 0 0 0 92.71 33.032c106.273 5.112'
    ' 197.08-75.794 204.215-181.95 4.154-106.382-77.67-196.486-183.958-202.574Z"'
    ' clip-rule="evenodd"/>'
    '</svg>'
)

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
        "NAME": name,
        "ICON": {
            "DATA_IMAGE": icon, "COLOR": color,
            "SIZE": "80%", "POSITION": "center",
        },
        "ICON_DISABLED": {
            "DATA_IMAGE": icon, "COLOR": "#99adb3",
            "SIZE": "80%", "POSITION": "center",
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
            "DATA":      {"id": connector_id, "name": name, "url": url},
        })
        if url != PUBLIC_URL:
            print(f"    Ссылка виджета: {url}")
    except Exception as e:
        print(f"    data.set предупреждение: {e}")

    print(f"  ✔ {name} зарегистрирован")


def register_message_sender(suffix: str, name: str) -> None:
    """Регистрирует провайдера в messageservice — для кнопки 'Сообщение' в карточке CRM."""
    code = f"{os.environ.get('B24_CONNECTOR_ID', 'maxbridge')}_{suffix}"
    print(f"  MessageService: {code} ({name})…")
    try:
        bitrix.call("messageservice.sender.add", {
            "CODE":    code,
            "TYPE":    "SMS",
            "NAME":    name,
            "HANDLER": f"{PUBLIC_URL}/bitrix/message",
        })
        print(f"  ✔ MessageService {name} зарегистрирован")
    except RuntimeError as e:
        err = str(e).lower()
        if "already" in err or "exist" in err or "duplicate" in err:
            # Уже зарегистрирован — обновляем HANDLER на случай смены домена
            try:
                bitrix.call("messageservice.sender.update", {
                    "CODE":    code,
                    "HANDLER": f"{PUBLIC_URL}/bitrix/message",
                })
                print(f"  ✔ MessageService {name} — HANDLER обновлён")
            except Exception:
                print(f"    уже зарегистрирован (ok)")
        else:
            print(f"  ✖ MessageService {name}: {e}")
            print(f"    Убедись что в локальном приложении Битрикс24 добавлен скоуп 'messageservice'")
            print(f"    и переавторизуй приложение через кнопку в веб-морде.")


def main() -> None:
    enabled = [c for c in _CONNECTORS
               if os.environ.get(c["env"], "true").lower() == "true"]

    if not enabled:
        print("Ни один адаптер не включён. Проверь .env (WA_ENABLED / MAX_ENABLED / TG_ENABLED).")
        return

    print(f"Регистрация {len(enabled)} коннектора(ов) на линии #{LINE_ID}…\n")
    for c in enabled:
        register_one(c["id"], c["name"], c["color"], c["icon"])

    # MessageService — для кнопки «Сообщение» в карточке CRM лида/контакта
    print(f"\nРегистрация MessageService провайдеров…\n")
    for c in enabled:
        if c["id"] in ("wa", "max"):  # TG не поддерживает отправку по телефону
            register_message_sender(c["id"], c["name"])

    print("\nГотово.")
    print("Следующий шаг: CRM → Контакт-центр → привяжи коннекторы к своей линии.")
    print("Настрой очередь операторов и направление ответственному в настройках линии.")


if __name__ == "__main__":
    main()
