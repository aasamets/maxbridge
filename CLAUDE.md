# CLAUDE.md — контекст проекта для Claude Code

Этот файл Claude Code читает в начале каждой сессии. Здесь — суть проекта, ключевые решения
и инструкция для запуска с нуля. Подробности архитектуры — в коде.

## Что это за проект

Самописный коннектор: один телефонный номер с мессенджерами (WhatsApp, MAX, Telegram) →
Открытая линия Битрикс24 → отдел продаж. Клиент пишет **на номер** в любом мессенджере,
сообщение попадает в Битрикс24, менеджер отвечает из Битрикса, ответ возвращается клиенту.
Аналог Wazzup, только свой и без абонентки (только VPS + SIM).

## ✅ РАБОЧИЙ КОСТЯК (верифицировано 2026-07-29, полный двусторонний WA + MAX)

```
[WhatsApp] → Baileys messages.upsert → POST /incoming → imconnector.send.messages → [Открытая линия Битрикс24]
[Битрикс24] → ONIMCONNECTORMESSAGEADD → POST /bitrix/events → адаптер POST /send → [WhatsApp]
[CRM кнопка «Сообщение»] → Битрикс24 messageservice → POST /bitrix/message → адаптер POST /send → [WA/MAX]

[MAX] → pymax on_message → POST /incoming → imconnector.send.messages → [Открытая линия Битрикс24]
[Битрикс24] → ONIMCONNECTORMESSAGEADD → POST /bitrix/events → MAX /send → [MAX]
[CRM кнопка «Сообщение»] → POST /bitrix/message → MAX /send → search_by_phone → get_chat_id → [MAX]
```

**Верифицировано вручную (2026-07-29):**
- Клиент → WA → Битрикс24 Открытая линия → менеджер ✅
- Менеджер → Битрикс24 → WA → клиент (ответ в линии) ✅
- Менеджер инициирует первым: карточка → «Сообщение» → MaxBridge → WhatsApp → клиент ✅
- Клиент → MAX → Битрикс24 Открытая линия → менеджер ✅
- Менеджер → Битрикс24 → MAX → клиент ✅
- Менеджер инициирует первым через MAX из карточки CRM ✅
- WA @lid JID корректно резолвится в номер телефона (не создаёт дубль-лид) ✅

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

### Лог сообщений (messages)
Все входящие и исходящие пишутся в SQLite таблицу `messages` (adapter, direction in/out, phone, text, created_at).
Доступны через `GET /api/messages?adapter=whatsapp|max|telegram&limit=200`.
В веб-морде — секция «Лог сообщений» с вкладками по мессенджерам.
Логируются три точки: `/incoming` (клиент → Битрикс), `/bitrix/events` (ответ из Open Line),
`/bitrix/message` (CRM-кнопка «Сообщение»).

### Коннекторы Битрикса — брендинг
NAME в `imconnector.register` и `messageservice.sender.add` — без префикса «MaxBridge»:
`"WhatsApp"`, `"MAX"`, `"Telegram"`. Так отображается в интерфейсе Битрикса.
Иконки: одноцветный SVG-глиф (белый) + COLOR-фон. SIZE=80%, POSITION=center.
Глиф WA — трубка viewBox 24×24. Глиф MAX — официальный SVG (только path, без rect-градиентов).

### Потеря входящих MAX при рестарте адаптера
pymax WebClient при реконнекте **не воспроизводит** пропущенные сообщения.
Сообщение, пришедшее пока контейнер был оффлайн, теряется безвозвратно.
Мониторить через ntfy-уведомления — alert при падении адаптера.

### WhatsApp @lid JID
WA постепенно мигрирует пользователей с `@s.whatsapp.net` на `@lid` (Account ID).
Тот же человек может слать сообщения под разными JID → без обработки создаётся дубль-лид.

**Фикс:** `msg.key.senderPn` содержит реальный `@s.whatsapp.net` JID даже когда
`remoteJid` = `@lid`. Читаем оттуда номер телефона напрямую.
Резерв: `contacts.upsert` событие строит маппинг `_lidToPhone` (работает если WA
прислал контакт-апдейт до сообщения).

`isJidUser()` из Baileys @lid не принимает — явная проверка `jid.endsWith('@lid')`.
Подробности — `wa_adapter/index.js`, обработчик `messages.upsert`.

### Baileys fetchProps patch
WA-серверы не отвечают на `fetchProps` IQ. Baileys зависал на 60 сек («зомби-состояние»).
Фикс: sed-патч в `wa_adapter/Dockerfile` — `fetchProps().catch(...)`.
**Не удалять** — без патча QR появляется, но сообщения не приходят.

### fetchLatestBaileysVersion timeout
`fetchLatestBaileysVersion()` делает прямой HTTP-запрос к WA-серверам. На VPS с блокировкой WA
висит навсегда. Фикс: 8-секундный таймаут через `Promise.race()` в `wa_adapter/index.js`.

### WA reconnect backoff
WA даёт код 405 при слишком частых реконнектах (rate-limiting).
Фикс: экспоненциальный backoff `5s → 7.5s → 11s → ... → 60s`, сбрасывается при
успешном подключении. Коды 408 (fetchProps timeout) и 428 (connection replaced) — штатные,
WA всегда переподключается. Код 405 тоже не фатален, просто означает «подожди».

### MAX chat_id=0 при холодном старте
При запуске pymax клиент стартует с `chats=0` — история чатов ещё не загружена.
`get_chat_id(me_id, user_id)` возвращает 0, сообщение уходит в никуда.
Фикс: `POST /send` возвращает 503 если `chat_id == 0`, с явным текстом ошибки.
Решение для менеджера: повторить через 10–15 секунд после старта контейнера.

### WA через SOCKS5 (xray)
VPS-провайдеры блокируют прямые подключения к WhatsApp. WA идёт через xray SOCKS5 (`xray:1080`).
Xray нужен даже когда Telegram отключён. `install.sh` спрашивает VLESS URL при включении WA или TG.
`xray/config.json` монтируется как bind mount → патчится `install.sh` при первой установке.

### Коннекторы Битрикс24
После первичного OAuth: `docker compose exec core python3 install_connector.py`.
Регистрирует `maxbridge_wa`, `maxbridge_max` (и `maxbridge_tg`) через:
- `imconnector.register` — NAME, ICON. Повторный вызов с тем же ID обновляет название и иконку,
  **не** удаляет привязку к Открытой линии и **не** удаляет историю диалогов.
- `imconnector.activate` — привязывает к LINE_ID (идемпотентно).
- `messageservice.sender.add` — для кнопки «Сообщение» в карточке CRM (исходящие).
Без этого: `IMCONNECTOR_NO_CORRECT_PROVIDER`. Повторный запуск безопасен.
Иконки — base64 SVG-глиф (белый) поверх COLOR-фона. PLACEMENT_HANDLER → `/bitrix/app`.

### messageservice scope
Для регистрации CRM-отправщиков нужен скоуп `messageservice` в локальном приложении Битрикс24.
Текущие скоупы: `imconnector, imopenlines, crm, im, messageservice`.
После добавления скоупа — переавторизовать через веб-морду и запустить `install_connector.py`.

### Где хранится переписка с клиентом
Переписка через imconnector живёт в **Открытой линии** (Контакт-центр Битрикс24),
а не в карточке контакта/лида. Это стандартная модель imconnector — так работают все
коннекторы (Wazzup, Ebox и др.).

Чтобы чат Open Line был виден в карточке контакта/лида:
- Битрикс24 → Контакт-центр → Открытые линии → линия → Настройки
- Включить **«Проверять клиента по базе CRM»** (это и есть "Привязывать к CRM")
- Включить **«Автоматически создать новый лид»** если клиент не найден
- Битрикс матчит по номеру телефона — важен точный формат (`+79...`)

После настройки: входящие сообщения привязываются к контакту/лиду по телефону,
чат отображается в ленте активности карточки.

**Решение пробовалось и отвергнуто:** `crm.activity.add` при каждом исходящем —
создаёт «Встречу» (TYPE_ID=1) в ленте, что засоряет CRM. Open Line — правильное место.

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

После подключения каналов — настроить Open Line в Битрикс24:
- Контакт-центр → Открытые линии → линия → Настройки → включить «Проверять клиента по базе CRM»
- Привязать MaxBridge WA/MAX коннекторы к этой линии (если не привязаны автоматически)

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

### ~~messageservice — разграничение мессенджеров в выпадайке CRM~~

**Закрыто (2026-07-28):** Битрикс24 сам решает задачу через второй дропдаун.
Первый дропдаун — провайдер («MaxBridge»), второй — канал («WhatsApp» / «MAX»).

### ~~История исходящих в карточке CRM~~

**Закрыто (2026-07-29):** `crm.activity.add` создаёт «Встречу» (TYPE_ID=1 = Meeting),
что засоряет CRM. Переписка живёт в Open Line — это правильная модель.
Если нужна история исходящих, смотреть в Контакт-центре, а не в карточке.

### ~~Лог сообщений в веб-морде~~

**Закрыто (2026-08-05):** Реализован MVP — таблица `messages` в SQLite,
логирование всех входящих и исходящих, секция в веб-морде с вкладками по мессенджерам.

### Подпись менеджера в исходящих сообщениях (WhatsApp / MAX / TG)

Запрос: клиент в WhatsApp видит обезличенный аккаунт — у конкурентов (Wazzup) имя менеджера
подставляется в начало сообщения автоматически, создаёт эмоциональную связь.

Решение: изменить имя отправителя в WA невозможно (Baileys = один профиль),
но можно **автоматически добавлять подпись оператора в текст**:

```
Иван Петров:

Добрый день! Готов ответить на ваши вопросы.
```

Детали реализации:
- `ONIMCONNECTORMESSAGEADD` (ответ из Открытой линии): изучить какое поле несёт имя оператора
  (предположительно `data[MESSAGES][idx][user][name]` или похожее — проверить в логах).
- `messageservice` (CRM-кнопка «Сообщение»): имя оператора в запросе не приходит,
  нужно вызвать `user.current` через REST API Битрикс24 перед отправкой.
- Настройка в веб-морде: чекбокс «Добавлять имя менеджера к сообщениям» (по умолчанию выкл).
- Шаблон подписи: `"{name}:\n\n{text}"` — можно вынести в настройки.

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
