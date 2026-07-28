# MaxBridge

Самохостинговый коннектор: один телефонный номер с тремя мессенджерами
(**WhatsApp**, **MAX**, **Telegram**) → Открытая линия **Битрикс24** → отдел продаж.

Клиент пишет **на номер** в любом мессенджере — сообщение падает в Битрикс,
менеджер отвечает там же, ответ уходит клиенту обратно.

Аналог Wazzup/Ebox — только свой, без абонентки. Только VPS + SIM.

---

## Как это работает

```
[WhatsApp · MAX · Telegram]
         │  (userbot-сессии на одном номере)
         ▼
    [core — FastAPI]  ←→  [Битрикс24 Открытая линия]
         │                      ↕ imconnector + messageservice API
    [SQLite: чаты, токены]  [Операторы / продавцы]
         │
    [Caddy HTTPS]  ←  веб-морда: статус, QR, вход, подсказки
```

**Два направления:**
- Клиент → менеджер: входящее через `imconnector.send.messages`
- Менеджер → клиент (первым): через `messageservice` прямо из карточки лида/контакта CRM

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

Установщик интерактивно спросит домен, данные Битрикс24, VLESS-ссылку (для WA/TG),
ntfy-топик для мониторинга. Соберёт Docker-образы, запустит сервисы, покажет пароль веб-морды.

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
   - Права: `imconnector, imopenlines, crm, im, messageservice`
   - URL обработчика: `https://ВАШ-ДОМЕН/bitrix/events`
2. Скопируй Client ID и Client Secret → Настройки в веб-морде → Сохранить
3. Нажми **Авторизовать приложение** → разреши в Битриксе (один раз)
4. Зарегистрируй коннекторы (один раз после OAuth):
   ```bash
   docker compose exec core python3 install_connector.py
   ```
5. В Битрикс24: Контакт-центр → Открытые линии → привяжи коннекторы MaxBridge

После OAuth токены сохраняются в SQLite и обновляются автоматически.

---

## Написать клиенту первым из Битрикс24

Работает из карточки **лида**, **контакта** или **сделки**:

1. Открой карточку → вкладка **«Сообщение»**
2. Первый дропдаун — выбери **MaxBridge** (наш провайдер)
3. Второй дропдаун под полем текста — выбери канал: **WhatsApp** или **MAX**
4. Введи номер телефона клиента (в формате `+7...`)
5. Напиши текст → **Отправить**

Сообщение дойдёт клиенту напрямую в WhatsApp / MAX, минуя Открытую линию.
Ответ клиента создаст новый чат в Открытой линии.

> Битрикс24 сам разделяет WA и MAX через внутренний дропдаун каналов — отдельная
> настройка не нужна. `CODE` каждого провайдера (`maxbridge_wa`, `maxbridge_max`)
> распознаётся автоматически.

---

## Мониторинг (ntfy)

В веб-морде, в разделе **Уведомления**, вставь URL ntfy-темы —
и при падении или восстановлении любого адаптера на телефон придёт push.

Как настроить:
1. Установи приложение **ntfy** (iOS / Android)
2. На [ntfy.sh](https://ntfy.sh) придумай уникальную тему, например `maxbridge-x7k2qw`
3. В приложении ntfy подпишись на эту тему
4. В веб-морде MaxBridge вставь `https://ntfy.sh/maxbridge-x7k2qw` → нажми **Тест**

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
  Установщик спрашивает VLESS-ссылку при включении WA — рекомендуется задать.
- **Telegram** — заблокирован; трафик адаптера идёт через xray VLESS-Reality.
  Нужна **VLESS-ссылка** (`vless://...`) от зарубежного сервера с Reality.
- **MAX** — российский мессенджер, подключается напрямую без прокси.

> `my.telegram.org` открывай с российского IP напрямую (без VPN).

---

## Текущее состояние

| Канал | Статус |
|---|---|
| WhatsApp | ✅ Работает: приём, ответ, CRM-инициация по номеру |
| MAX | ✅ Готов: pymax WebClient, QR-авторизация через веб-морду |
| Telegram | 🔧 Адаптер готов, нужны `api_id` / `api_hash` |
| Битрикс24 Открытая линия | ✅ OAuth + imconnector + auto-refresh токенов |
| Битрикс24 CRM-сообщения | ✅ messageservice: кнопка «Сообщение» в карточке лида |
| Веб-морда | ✅ Статус (WS), QR, настройки, push-уведомления (ntfy) |

---

## Стек

| Компонент | Технология |
|---|---|
| Ядро | Python 3.12, FastAPI, SQLite |
| WhatsApp | Node.js 20, [@whiskeysockets/baileys](https://github.com/WhiskeySockets/Baileys) |
| MAX | Python 3.12, pymax WebClient |
| Telegram | Python 3.12, [Telethon](https://github.com/LonamiWebs/Telethon) |
| Прокси | [Xray-core](https://github.com/XTLS/Xray-core) VLESS-Reality → SOCKS5 |
| HTTPS | [Caddy](https://caddyserver.com) (авто Let's Encrypt) |
| Контейнеры | Docker Compose (6 сервисов: core, wa, max, telegram, caddy, xray) |

---

## Структура проекта

```
core/
  main.py              FastAPI: /incoming, /bitrix/events, /bitrix/message, веб-морда, /api/*
  bitrix.py            Клиент Битрикс24: OAuth, imconnector, messageservice, авто-refresh
  store.py             SQLite: chat_map, seen_msg, kv (токены, ntfy URL), adapter_state
  static/              Веб-морда (index.html, style.css, app.js)

adapters/
  telegram_adapter.py  Telethon: QR/phone login, proxy xray:1080
  max_adapter.py       pymax WebClient: QR-авторизация, supervisor-паттерн

wa_adapter/
  index.js             Baileys: multifile auth, @lid JID, proxy socks5
  Dockerfile           npm install + sed-патч Baileys (fetchProps graceful skip)

xray/
  config.json          Шаблон с плейсхолдерами — патчится install.sh из VLESS_URL

install.sh             Интерактивный установщик (домен, Битрикс, VLESS, ntfy)
install_connector.py   Регистрация imconnector + messageservice.sender в Битрикс24
docker-compose.yml     Оркестрация, shared volumes (sessions, data)
Caddyfile.template     HTTPS reverse proxy шаблон
.env.example           Пример переменных (без секретов)
```

---

## Диагностика

### WA подключён, но сообщения не приходят в Битрикс

```bash
docker compose ps
docker compose logs -f wa
docker compose logs -f core
```

### WA не показывает QR / не подключается

```bash
# Проверить прокси из контейнера wa
docker compose exec wa node -e "
const {SocksProxyAgent} = require('socks-proxy-agent');
const agent = new SocksProxyAgent('socks5://xray:1080');
require('https').get('https://web.whatsapp.com/', {agent}, r => console.log('HTTP', r.statusCode))
  .on('error', e => console.log('ERR', e.message));"
```

Если нет прокси — задать VLESS-ссылку в Настройках веб-морды и пересобрать xray.

### Коннекторы не зарегистрированы (IMCONNECTOR_NO_CORRECT_PROVIDER)

```bash
docker compose exec core python3 install_connector.py
```

Запускать один раз после первичного OAuth. Повторный запуск безопасен.

### Telegram не подключается

```bash
docker compose ps xray          # xray должен быть Up
docker compose logs telegram    # смотреть ошибки Telethon
```

Нужны `TG_API_ID` и `TG_API_HASH` в Настройках веб-морды + VLESS-ссылка.

---

## Команды на сервере

```bash
cd /opt/maxbridge

docker compose ps                        # статус всех сервисов
docker compose logs -f                   # все логи
docker compose logs -f wa                # логи одного сервиса
docker compose up -d --build             # пересборка всего
docker compose up -d --build core        # пересборка только ядра
docker compose exec core python3 install_connector.py  # (ре)регистрация коннекторов
```

---

## Предупреждения

- **Серая зона.** Вход под обычным аккаунтом нарушает правила WhatsApp, Telegram и MAX.
  Аккаунт могут заблокировать. Разумный темп, без рассылок, один номер — один аккаунт.
- **Сессии.** При потере файлов сессий (`/sessions/`) потребуется переавторизация
  через веб-морду. В Docker — только named volumes.
- **Персистентность.** Не удалять volumes `maxbridge_data` и `maxbridge_sessions` —
  там OAuth-токены Битрикса и сессии мессенджеров.

---

by [@aasamets_a](https://t.me/aasamets_a)
