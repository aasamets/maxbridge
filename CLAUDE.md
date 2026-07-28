# CLAUDE.md — контекст проекта для Claude Code

Этот файл Claude Code читает в начале каждой сессии. Здесь — суть проекта, ключевые решения
и инструкция для запуска с нуля. Подробности архитектуры — в коде.

## Что это за проект

Самописный коннектор: один телефонный номер с мессенджерами (WhatsApp, MAX, Telegram) →
Открытая линия Битрикс24 → отдел продаж. Клиент пишет **на номер** в любом мессенджере,
сообщение попадает в Битрикс24, менеджер отвечает из Битрикса, ответ возвращается клиенту.
Аналог Wazzup, только свой и без абонентки (только VPS + SIM).

## ✅ РАБОЧИЙ КОСТЯК (верифицировано 2026-07-28)

```
[WhatsApp] → Baileys messages.upsert → POST /incoming → imconnector.send.messages → [Открытая линия Битрикс24]
[Битрикс24] → ONIMCONNECTORMESSAGEADD → POST /bitrix/events → адаптер POST /send → [WhatsApp]
[CRM кнопка «Сообщение»] → Битрикс24 messageservice → POST /bitrix/message → адаптер POST /send → [WA/MAX]
```

## Ключевые архитектурные решения (не пересматривать без обсуждения)

- **Ядро messenger-агностично.** Вся логика Битрикса — в `core/`. Каждый мессенджер — отдельный
  адаптер с единым контрактом: `GET /status`, `GET /qr`, `POST /send`, `POST /logout`, `POST /reconnect`;
  входящие шлются на `core POST /incoming`. Новый мессенджер = новый адаптер, ядро не трогаем.
- **Маршрутизацию делает Открытая линия, а не код.** Ядро лишь передаёт телефон клиента.
- **imconnector требует OAuth-приложения, не вебхука.** Токены освежаются из событий + refresh.
- **«Номерной» режим — серая зона.** Userbot против правил мессенджеров, но это осознанный выбор.
  Человеческий темп, без рассылок, один номер — один аккаунт.
- **Персистентность критична.** Файлы сессий и SQLite (`data/`) нельзя терять — только named volumes.
- **Номер телефона — автоопределяется, не хардкодится.** При подключении адаптер сообщает
  свой номер в `POST /api/adapter_phone` → core сохраняет в SQLite kv. Показывается в UI.

## Технические особенности (зафиксировано)

### WhatsApp @lid JID
Новый формат JID: `15857119940815@lid` вместо `+79...@s.whatsapp.net`.
Номер телефона из @lid **не извлекается** — используем lid как peer_id, phone=null.
`isJidUser()` из Baileys @lid не принимает — явная проверка `jid.endsWith('@lid')`.

### Baileys fetchProps patch
WA-серверы не отвечают на `fetchProps` IQ. Baileys зависал на 60 сек («зомби-состояние»).
Фикс: sed-патч в `wa_adapter/Dockerfile` — `fetchProps().catch(...)`.
**Не удалять** — без патча QR появляется, но сообщения не приходят.

### fetchLatestBaileysVersion timeout
`fetchLatestBaileysVersion()` делает прямой HTTP-запрос к WA-серверам. На VPS с блокировкой WA
висит навсегда. Фикс: 8-секундный таймаут через `Promise.race()` в `wa_adapter/index.js`.

### WA через SOCKS5 (xray)
VPS-провайдеры блокируют прямые подключения к WhatsApp. WA идёт через xray SOCKS5 (`xray:1080`).
Xray нужен даже когда Telegram отключён. `install.sh` спрашивает VLESS URL при включении WA или TG.
`xray/config.json` монтируется как bind mount → патчится `install.sh` при первой установке.

### Коннекторы Битрикс24
После первичного OAuth: `docker compose exec core python3 install_connector.py`.
Регистрирует `maxbridge_wa`, `maxbridge_max` (и `maxbridge_tg`) через:
- `imconnector.*` — для Открытой линии (входящие + ответы)
- `messageservice.sender.add` — для кнопки «Сообщение» в карточке CRM (исходящие)
Без этого: `IMCONNECTOR_NO_CORRECT_PROVIDER`. Повторный запуск безопасен.
Иконки — base64 SVG. PLACEMENT_HANDLER → `/bitrix/app` (публичная iframe-страница статуса).

### messageservice scope
Для регистрации CRM-отправщиков нужен скоуп `messageservice` в локальном приложении Битрикс24.
Текущие скоупы: `imconnector, imopenlines, crm, im, messageservice`.
После добавления скоупа — переавторизовать через веб-морду и запустить `install_connector.py`.

### ntfy push-уведомления
Настраивается из веб-морды (раздел «Уведомления»). URL сохраняется в SQLite kv,
при старте core читает kv поверх env. Алерты при недоступности/восстановлении адаптеров.

### Публичные пути (не убирать из белого списка)
`/login`, `/incoming`, `/bitrix/events`, `/bitrix/install`, `/bitrix/oauth`,
`/bitrix/app`, `/api/adapter_phone`, `/bitrix/message`

## Порядок ввода каналов

1. **WhatsApp** (Baileys, Node) — через xray SOCKS5. QR в веб-морде. **Работает.**
2. **MAX** (pymax, Python) — напрямую. QR в веб-морде → «Устройства → Привязать». **Работает.**
3. **Telegram — последним:** нужен прокси (VLESS-Reality) + `TG_API_ID`/`TG_API_HASH` от my.telegram.org.

## Структура

```
core/main.py            FastAPI: /incoming, /bitrix/events, /bitrix/message, /api/*, /bitrix/app, веб-морда
core/bitrix.py          Битрикс24: OAuth + imconnector + messageservice + авто-refresh токенов
core/store.py           SQLite: chat_map, seen_msg, kv (токены, телефоны, ntfy), adapter_state
core/static/            веб-морда (index.html, app.js, style.css)
adapters/max_adapter.py pymax WebClient — QR, supervisor, авто-определение телефона
adapters/telegram_adapter.py  Telethon — прокси xray:1080, QR-login
wa_adapter/index.js     Baileys — multifile auth, @lid JID, socks5, авто-определение телефона
wa_adapter/Dockerfile   npm install + sed-патч fetchProps
install_connector.py    imconnector + messageservice.sender регистрация (после OAuth)
install.sh              интерактивный установщик (Docker, git clone, .env, сборка, запуск)
Caddyfile.template      обратный прокси: HTTPS + Let's Encrypt
.env.example            шаблон (реальный .env — только на сервере)
```

## Запуск с нуля

1. Восстановить снапшот `infra-base` (Ubuntu 26.04, ufw 22/80/443, git, python3.12, nodejs)
2. `curl -fsSL https://raw.githubusercontent.com/aasamets/maxbridge/main/install.sh -o /tmp/install.sh && bash /tmp/install.sh`
   - ⚠️ `bash <(curl ...)` не работает на Ubuntu. Только двухшаговый вариант.
   - Установщик спрашивает: домен, Битрикс24 реквизиты, какие мессенджеры, VLESS URL (если WA или TG), ntfy URL
3. Открыть `https://ДОМЕН` → войти → кнопка «Авторизовать в Битрикс24» (OAuth, один раз)
4. `docker compose exec core python3 install_connector.py` — зарегистрировать коннекторы
5. В каждой карточке мессенджера — отсканировать QR

## Инфраструктура

- VPS: Ubuntu 26.04, 1 vCPU / 1 ГБ RAM + 2 ГБ swap / 10 ГБ SSD.
- Telegram: VLESS-Reality прокси (ссылка — только в `.env` на сервере, не в Git).
- Битрикс24: облако, локальное приложение (scope: `imconnector, imopenlines, crm, im, messageservice`).
- **SSH: только по паролю** (снапшот не сохраняет authorized_keys).

## Безопасность

- Адаптеры и ядро — только внутри Docker-сети. Наружу — только Caddy (443/80).
- Веб-морда: FastAPI session auth (cookie `mb_session`, 8ч TTL).
- `/incoming`, `/api/adapter_phone`, `/bitrix/message` — публичные (только внутренний/B24 трафик).
- xray SOCKS5 `0.0.0.0:1080` — только внутри Docker-сети, наружу не пробрасывается.
- **Никогда не коммитить:** `.env`, `*.session`, VLESS-ссылку, токены, пароли.

## План следующей итерации

### messageservice — разграничение мессенджеров в выпадайке CRM

Симптом: в карточке лида в кнопке «Сообщение» выпадает «MaxBridge» без указания на мессенджер.
Когда подключены WA + MAX + TG — появится три одинаковых «MaxBridge», менеджер не поймёт, куда пишет.

Исследовать:
- Что именно отображает Битрикс — поле `NAME` из `messageservice.sender.add` или поле `CODE`?
- Достаточно ли переименовать `NAME` в `"WhatsApp"` / `"MAX"` / `"Telegram"` (без «MaxBridge») —
  или нужно явно добавить бренд для узнаваемости?
- Проверить, обновляется ли NAME через `messageservice.sender.update` без повторной OAuth.

Вариант по умолчанию: `NAME = "WhatsApp (MaxBridge)"`, `"MAX (MaxBridge)"`, `"Telegram (MaxBridge)"` —
понятно менеджеру, сохраняет брендинг. Можно менять без переавторизации через `install_connector.py`.

## Рабочий процесс с Claude Code

- **Код — только по явной команде** («пиши», «пиши код», «реализуй»).
- Итерации ≈1 час, 3–4ч/нед: формируем задачи → уточнения → план одним блоком → команда.
- Коммиты: conventional commits (`feat:`, `fix:`, `chore:`, `docs:`).
- Деплой: локально → GitHub push → сервер `git pull && docker compose up -d --build`.
- Для чистой установки: снапшот → `install.sh` → OAuth → `install_connector.py` → QR.

## Команды (сервер)

```bash
cd /opt/maxbridge
docker compose ps                        # статус
docker compose logs -f core              # логи ядра
docker compose logs -f wa                # логи WA
docker compose restart core              # перезапуск без пересборки
docker compose up -d --build             # пересборка
docker compose exec core python3 install_connector.py  # (ре)регистрация коннекторов
```
