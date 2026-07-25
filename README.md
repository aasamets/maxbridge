# MaxBridge

Самохостинговый коннектор: один телефонный номер с тремя мессенджерами
(**MAX**, **Telegram**, **WhatsApp**) → Открытая линия **Битрикс24** → отдел продаж.

Клиент пишет **на номер** в любом мессенджере — сообщение падает в Битрикс,
менеджер отвечает там же, ответ уходит клиенту обратно. Телефон лежит «в шкафу».

Аналог Wazzup/Ebox — только свой, без абонентки. Только VPS + SIM.

---

## Как это работает

```
[WhatsApp · MAX · Telegram]
         │  (userbot-сессии на одном номере)
         ▼
    [core — FastAPI]  ←→  [Битрикс24 Открытая линия]
         │                      ↕ imconnector API
    [SQLite: чаты, токены]  [Операторы / продавцы]
         │
    [Caddy HTTPS]  ←  веб-морда: статус, QR, вход, подсказки
```

Маршрутизацию («только продавцы», «на ответственного если клиент в CRM»)
делает **сама Открытая линия** — код этим не занимается.

### Контракт адаптеров

Каждый мессенджер — отдельный сервис с единым HTTP-интерфейсом:

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/status` | `{"state": "connected\|needs_auth\|unavailable"}` |
| GET | `/qr` | PNG QR-кода (пока не авторизован) |
| POST | `/send` | `{peer_id, text}` — отправить сообщение |
| POST | `/login` | Начать авторизацию по номеру (Telegram) |
| POST | `/code` | Ввести SMS-код (Telegram) |
| POST | `/password` | Ввести пароль 2FA (Telegram) |
| POST | `/logout` | Разорвать сессию |
| POST | `/reconnect` | Переподключиться без сброса сессии |

Входящие сообщения адаптер шлёт в ядро: `POST /incoming`.

---

## Быстрый старт

> Требуется VPS с белым IP, DNS A-запись на него и аккаунт Битрикс24.

```bash
curl -fsSL https://raw.githubusercontent.com/aasamets/maxbridge/main/install.sh \
  -o /tmp/install.sh && bash /tmp/install.sh
```

Установщик интерактивно спросит домен, данные Битрикс24, VLESS-ссылку.
Соберёт Docker-образы, запустит сервисы, покажет пароль веб-морды.

После установки — открой веб-морду и подключи каналы по одному.

---

## Подключение каналов

### WhatsApp

1. В карточке **WhatsApp** нажми «Отсканировать QR» — появится QR-код
2. В приложении WhatsApp: три точки → Связанные устройства → Привязать устройство
3. Наведи камеру — статус сменится на «Подключён»

Сессия держится несколько недель. QR обновляется автоматически каждые 30 сек.

> **Технически:** Baileys (WebSocket Noise Protocol), через xray SOCKS5 → VLESS-Reality
> если провайдер VPS блокирует WA. Сессия — multifile auth state в `/sessions/wa`.

### MAX

1. В карточке **MAX** нажми «Показать QR»
2. В приложении MAX: Профиль → Устройства → Привязать устройство → Сканировать QR
3. Статус сменится на «Подключён»

Сессия сохраняется на сервере — при перезапуске переавторизация не нужна.

> **Технически:** pymax `WebClient` (WebSocket к MAX-серверам напрямую),
> сессия в SQLite `/sessions/max/session.db`. Без сторонних шлюзов.

### Telegram

1. Получи `api_id` и `api_hash` на [my.telegram.org](https://my.telegram.org)
   *(с российского IP напрямую, без прокси — иначе не откроется)*
2. Добавь в Настройки → Сохранить → перезапусти контейнер `telegram`
3. В карточке Telegram введи номер телефона → код из SMS/уведомления → (2FA пароль если есть)

> **Технически:** Telethon, трафик через xray SOCKS5:1080 → VLESS-Reality.
> Сессия — `/sessions/telegram.session`.

### Битрикс24

1. В Битрикс24: **Приложения → Разработчикам → Другое → Локальное приложение**
   - Тип: Серверное приложение
   - Права: `imconnector, imopenlines, crm, im`
   - URL обработчика: `https://ВАШ-ДОМЕН/bitrix/events`
2. Скопируй Client ID и Client Secret → Настройки в веб-морде → Сохранить
3. Нажми **Авторизовать приложение** → разреши в Битриксе (один раз)
4. Зарегистрируй коннекторы (один раз после OAuth):
   ```bash
   docker compose exec core python3 install_connector.py
   ```
5. В Битрикс24: Контакт-центр → Открытые линии → привяжи коннекторы MaxBridge

После OAuth токены сохраняются в SQLite и обновляются автоматически — кнопка
«Авторизовать» нужна только при первичной настройке или смене приложения.

---

## Требования

| Параметр | Минимум |
|---|---|
| ОС | Ubuntu 22.04+ / Debian 12+ |
| CPU | 1 vCPU |
| RAM | 1 ГБ + **2 ГБ swap** (обязательно) |
| Диск | 10 ГБ |
| Docker | 24+ с Compose plugin |
| Домен | A-запись → IP сервера (для HTTPS и OAuth Битрикс) |

**Протестировано на:** Ubuntu 26.04 LTS, 1 vCPU, 1 ГБ RAM + 2 ГБ swap, 10 ГБ SSD.

---

## Особенности в РФ

- **WhatsApp** — не заблокирован, но часть VPS-провайдеров блокируют трафик.
  Если WA не подключается: `WA_PROXY_HOST=xray` в `.env` → `docker compose up -d wa`.
- **Telegram** — заблокирован; трафик адаптера идёт через xray VLESS-Reality.
  Нужна **VLESS-ссылка** (`vless://...`) от зарубежного сервера с Reality.
- **MAX** — российский мессенджер, подключается напрямую без прокси.

> `my.telegram.org` открывай с российского IP напрямую (без VPN).

---

## Текущее состояние

| Канал | Статус |
|---|---|
| WhatsApp | ✅ Работает: приём и отправка, QR-авторизация, @lid JID поддерживается |
| MAX | ✅ Готов: pymax WebClient, QR-авторизация через веб-морду |
| Telegram | 🔧 Адаптер готов, нужны `api_id` / `api_hash` |
| Битрикс24 | ✅ OAuth + imconnector + auto-refresh токенов |
| Веб-морда | ✅ Статус каналов (WS), QR, Битрикс статус авто-определяется |

---

## Стек

| Компонент | Технология |
|---|---|
| Ядро | Python 3.12, FastAPI, SQLite (aiosqlite) |
| WhatsApp | Node.js 20, [@whiskeysockets/baileys](https://github.com/WhiskeySockets/Baileys) 6.7.x |
| MAX | Python 3.12, [pymax / maxapi-python](https://pypi.org/project/maxapi-python/) 2.3.1 |
| Telegram | Python 3.12, [Telethon](https://github.com/LonamiWebs/Telethon) |
| Прокси | [Xray-core](https://github.com/XTLS/Xray-core) VLESS-Reality → SOCKS5 |
| HTTPS | [Caddy](https://caddyserver.com) (авто Let's Encrypt) |
| Контейнеры | Docker Compose (6 сервисов: core, wa, max, telegram, caddy, xray) |

---

## Структура проекта

```
core/
  main.py              FastAPI: /incoming, /bitrix/events, веб-морда, /api/*
  bitrix.py            Клиент Битрикс24: OAuth, imconnector, авто-refresh токенов
  store.py             SQLite: chat_map, seen_msg, kv (токены), adapter_state
  static/              Веб-морда (index.html, style.css, app.js)

adapters/
  telegram_adapter.py  Telethon: QR/phone login, proxy xray:1080
  max_adapter.py       pymax WebClient: QR-авторизация, supervisor-паттерн

wa_adapter/
  index.js             Baileys: multifile auth, @lid JID, proxy socks5
  Dockerfile           npm install + sed-патч Baileys (fetchProps graceful skip)

xray/
  config.json          Генерируется install.sh из VLESS_URL

install.sh             Интерактивный установщик (домен, Битрикс, VLESS)
install_connector.py   Разовая регистрация коннекторов в Битрикс24
docker-compose.yml     Оркестрация, shared volumes (sessions, data)
Caddyfile.template     HTTPS reverse proxy шаблон
.env.example           Пример переменных (без секретов)
```

---

## Диагностика

### WA подключён, но сообщения не приходят в Битрикс

```bash
# Проверить статус всех сервисов
docker compose ps

# Логи WA в реальном времени
docker compose logs -f wa

# Проверить /incoming вручную
docker compose exec core python3 -c "
import httpx, asyncio
async def t():
    r = await httpx.AsyncClient().post('http://localhost:8000/incoming', json={
        'adapter':'whatsapp','peer_id':'+79990000000',
        'msg_id':'test','text':'тест','phone':'+79990000000'})
    print(r.status_code, r.text)
asyncio.run(t())"
```

### WA не показывает QR / не подключается

```bash
# Проверить прокси из контейнера
docker compose exec wa node -e "
const {SocksProxyAgent} = require('socks-proxy-agent');
const agent = new SocksProxyAgent('socks5://xray:1080');
require('https').get('https://web.whatsapp.com/', {agent}, r => console.log('HTTP', r.statusCode))
  .on('error', e => console.log('ERR', e.message));"
```

Если нет прокси — добавить `WA_PROXY_HOST=xray` в `.env` и `docker compose up -d wa`.

### Коннекторы не зарегистрированы (IMCONNECTOR_NO_CORRECT_PROVIDER)

```bash
docker compose exec core python3 install_connector.py
```

Запускать один раз после первичного OAuth Битрикса. Повторный запуск безопасен.

### Telegram не подключается

```bash
docker compose ps xray              # xray должен быть Up
docker compose logs telegram        # смотреть ошибки Telethon
# TG_API_ID и TG_API_HASH нужно добавить в Настройки веб-морды
```

### Посмотреть токены Битрикс24 в DB

```bash
docker compose exec core python3 -c "
from core import store; store.init()
print('access:', store.kv_get('b24_access_token')[:20] if store.kv_get('b24_access_token') else None)
print('refresh:', store.kv_get('b24_refresh_token')[:20] if store.kv_get('b24_refresh_token') else None)"
```

---

## Команды на сервере

```bash
cd /opt/maxbridge

docker compose ps                        # статус всех сервисов
docker compose logs -f                   # все логи
docker compose logs -f wa                # логи одного сервиса
docker compose up -d --build             # пересборка всего
docker compose up -d --build core wa     # пересборка отдельных сервисов
docker compose exec core bash            # shell внутри контейнера
```

---

## Предупреждения

- **Серая зона.** Вход под обычным аккаунтом нарушает правила WhatsApp, Telegram и MAX.
  Аккаунт могут заблокировать. Разумный темп, без рассылок, один номер — один аккаунт.
- **Сессии.** При потере файлов сессий (`/sessions/`) потребуется переавторизация
  через веб-морду. В Docker — только named volumes, никаких bind-mount в /tmp.
- **Персистентность.** Не удалять volumes `maxbridge_data` и `maxbridge_sessions` —
  там OAuth-токены Битрикса и сессии мессенджеров.

---

by [@aasamets_a](https://t.me/aasamets_a)
