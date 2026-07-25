# CLAUDE.md — контекст проекта для Claude Code

Этот файл Claude Code читает в начале каждой сессии. Здесь — суть проекта, решения и
правила, чтобы не переобъяснять их каждый раз. Подробности — в `PROJECT_PLAN.md`.

## Что это за проект

Самописный коннектор: один телефонный номер с тремя мессенджерами (MAX, Telegram, WhatsApp) →
Открытая линия Битрикс24 → отдел продаж. Клиент пишет **на номер** в любом мессенджере,
сообщение попадает в Битрикс, менеджер отвечает из Битрикса, ответ возвращается клиенту.
Аналог Wazzup, только свой и без абонентки (только VPS + SIM).

## ✅ РАБОЧИЙ КОСТЯК (не ломать, верифицировано 2026-07-25)

**WhatsApp → Битрикс24 — работает end-to-end:**

```
[WhatsApp] → Baileys messages.upsert → POST /incoming → imconnector.send.messages → [Открытая линия Битрикс24]
```

Подтверждено: сообщение от клиента (peer_id=`15857119940815@lid`) дошло до Открытой линии Битрикс24.
Логи подтверждения:
```
[WA] → core OK peer=15857119940815@lid "Тест"
core-1  | INFO: "POST /incoming HTTP/1.1" 200 OK
core-1  | INFO: "POST /bitrix/events HTTP/1.1" 200 OK
```

Это минимальный рабочий путь. Ничего в этой цепочке не менять без понимания всех звеньев.

## Ключевые архитектурные решения (не пересматривать без обсуждения)

- **Ядро messenger-агностично.** Вся логика Битрикса — в `core/`. Каждый мессенджер — отдельный
  адаптер с единым контрактом: `GET /status`, `GET /qr`, `POST /login`, `POST /code`,
  `POST /password`, `POST /send`, `POST /logout`, `POST /reconnect`;
  входящие адаптер шлёт на `core POST /incoming`.
  Новый мессенджер = новый адаптер, ядро не трогаем.
- **Маршрутизацию делает Открытая линия, а не код.** «Только продавцы» = очередь операторов
  линии; «ответственному, если контакт в CRM» = настройка линии. Ядро лишь передаёт телефон
  клиента в `imconnector.send.messages`. НЕ писать свой движок распределения.
- **imconnector требует локального приложения (OAuth), не вебхука.** Методы `imconnector.*`
  работают только в контексте приложения Битрикс. Токены освежаются из событий + по refresh.
- **«Номерной» режим — серая зона.** Вход под обычным аккаунтом (userbot) против правил всех
  трёх мессенджеров; номер можно забанить. Человеческий темп, без рассылок, один номер — один
  аккаунт. Это осознанный выбор: бизнесу нужно «пиши на номер», а не «пиши боту».
- **Персистентность критична.** Файлы сессий (`*.session`) и SQLite (`data/`) нельзя терять —
  иначе переавторизация и потеря привязки «диалог ↔ клиент». В Docker — только named volumes.
- **`/incoming` — публичный эндпоинт.** Адаптеры шлют туда сообщения без авторизации.
  Он включён в `_PUBLIC_PATHS` в `core/main.py`. Не убирать из белого списка.

## Известные технические особенности (зафиксировано)

### WhatsApp @lid JID (2026-07-25)
Новый формат JID в WhatsApp: `15857119940815@lid` вместо `+79...@s.whatsapp.net`.
Номер телефона **не извлекается** из @lid — используем lid как peer_id, phone=null.
`isJidUser()` из Baileys @lid не принимает — нужна явная проверка `jid.endsWith('@lid')`.
Код в `wa_adapter/index.js` это обрабатывает.

### Baileys fetchProps patch (2026-07-25)
WhatsApp-серверы перестали отвечать на `fetchProps` IQ-запрос. Baileys падал с таймаутом
через 60 сек при инициализации — WebSocket открыт, но сообщения не доставляются («зомби-состояние»).
Фикс: sed-патч в `wa_adapter/Dockerfile` — `fetchProps().catch(...)` вместо хардового ожидания.
**Не удалять этот патч** — без него WA подключается внешне (QR появляется), но сообщения не приходят.

### Коннекторы Битрикс24 (install_connector.py)
После первичного OAuth нужно запустить `docker compose exec core python3 install_connector.py`.
Регистрирует `maxbridge_wa`, `maxbridge_max`, `maxbridge_tg` в Битрикс24.
Без этого: `IMCONNECTOR_NO_CORRECT_PROVIDER` при входящем сообщении.
Повторный запуск безопасен (дубли event.bind игнорируются).

## Порядок ввода каналов

1. **WhatsApp** (Baileys, Node) — через xray SOCKS5 (VPS-провайдер блокирует WA напрямую).
   QR в веб-морде. **Работает, верифицировано.**
2. **MAX** (pymax, Python) — напрямую, без прокси. QR в веб-морде → «Устройства → Привязать».
   Без сторонних платных шлюзов. Адаптер готов, ждёт QR-сканирования.
3. **Telegram — последним:** заблокирован в РФ, нужен прокси. Трафик адаптера идёт через
   локальный Xray-клиент (SOCKS5 `xray:1080` в Docker) → VLESS-Reality → заграничный сервер.
   Нужны `TG_API_ID` / `TG_API_HASH` от my.telegram.org (получить с российского IP без VPN).

## Структура

```
core/main.py            FastAPI: /incoming, /bitrix/events, /api/*, веб-морда (статус, QR, вход)
core/bitrix.py          клиент Битрикс24 (OAuth + imconnector + авто-refresh токенов)
core/store.py           SQLite: chat_map, seen_msg, kv (токены), adapter_state
core/static/            веб-морда (index.html, app.js, style.css)
adapters/telegram_adapter.py  Telethon — прокси xray:1080, qr_login, ждёт TG_API_ID/HASH
adapters/max_adapter.py       pymax WebClient — QR-авторизация, supervisor-паттерн
wa_adapter/index.js     Baileys — multifile auth, @lid JID, socks5 прокси
wa_adapter/Dockerfile   npm install + sed-патч fetchProps
install_connector.py    разовая регистрация коннекторов + event.bind в Битрикс24
Caddyfile.template      обратный прокси: HTTPS + Let's Encrypt
.env.example            шаблон конфигурации (реальный .env — только на сервере)
```

## Следующий этап: исходящие из Битрикс24 (outbound initiation)

**Задача:** менеджер хочет написать клиенту первым прямо из Битрикс24 в WhatsApp
(или MAX/Telegram), не дожидаясь входящего от клиента.

**Как это устроено в imconnector:**
- `imconnector.chat.sendMessage` позволяет отправить сообщение в конкретный чат.
- Нужен `chat_id` — создаётся при первом входящем от клиента (уже есть в `chat_map`).
- Для «первого сообщения» клиента нет в `chat_map` — нужно создать чат через
  `imopenlines.chat.open` или `imconnector.send.messages` с пустым `chat_id`.

**Вариант реализации:**
1. В Битрикс24 — приложение (локальное или виджет CRM) с кнопкой «Написать в WhatsApp».
2. Кнопка открывает форму: выбор мессенджера + текст первого сообщения.
3. POST на `core/outbound` → core вызывает адаптер `POST /send {peer_id, text}`.
4. Адаптер через Baileys/pymax/Telethon отправляет сообщение.
5. Входящий ответ клиента потом придёт через обычный `messages.upsert` → `/incoming`.

**Нюанс WhatsApp:** для @lid peer_id отправка через `sock.sendMessage(jid, {text})` работает,
если jid уже известен (клиент писал). Для нового контакта нужен номер телефона → `+79...@s.whatsapp.net`.

**Это следующий крупный этап — не начинать пока не стабилен текущий костяк.**

## Инфраструктура (зафиксировано)

- Production VPS: домен и IP — в `.env` на сервере, не в Git.
  Ubuntu 26.04, 1 vCPU / 1 ГБ + 2 ГБ swap / 10 ГБ.
- Telegram-прокси: VLESS-Reality (ссылка — в `.env`, не в Git).
- Битрикс24: облако; коннектор Открытых линий через локальное приложение (scope
  `imconnector, imopenlines, crm, im`).
- **SSH на сервер: по паролю** (снапшот `infra-base` не сохраняет authorized_keys).
  Для автоматизации использовать paramiko с password auth.

## Безопасность

- Ядро и адаптеры слушают только внутри Docker-сети; наружу — только Caddy (443/80).
- Веб-морда под FastAPI session auth (cookie `mb_session`, 8ч TTL).
- Публичные пути (`_PUBLIC_PATHS`): `/login`, `/bitrix/events`, `/bitrix/install`,
  `/bitrix/oauth`, `/incoming`. Публичные префиксы: `/static`, `/adapters/max/webhook`.
  Events проверяются `application_token`.
- Xray слушает SOCKS5 на `0.0.0.0:1080` внутри Docker-сети; наружу порт не пробрасывается.
- `ufw`: открыты 22/80/443.

## WhatsApp и прокси

WA по умолчанию через xray (`WA_PROXY_HOST=xray`). VPS-провайдер блокирует WA напрямую
(диагноз: `nc -z web.whatsapp.com 443` из контейнера = TIMEOUT).

**Подтверждено (2026-07-25):** WA через VLESS-Reality + SOCKS5 xray работает корректно.
Baileys успешно проходит Noise Protocol handshake, генерирует QR-код. Ошибка 408
(connectionLost) — нормальное поведение: WhatsApp закрывает WebSocket через ~75 сек если
никто не отсканировал QR; адаптер переподключается и генерирует новый QR автоматически.

**Диагностика WA:** если QR не появляется >5 минут:
```bash
docker exec maxbridge-wa-1 node -e "
const {SocksProxyAgent} = require('socks-proxy-agent');
const agent = new SocksProxyAgent('socks5://xray:1080');
const https = require('https');
https.get('https://web.whatsapp.com/', {agent}, r => console.log('HTTP', r.statusCode))
  .on('error', e => console.log('ERR', e.message));
"
# Ожидаем: HTTP 200
```

## Рабочий процесс с Claude Code

- **Код — только по явной команде.** Пока не сказано «можно писать код» (или аналог) — только
  диалог, уточнения, планирование. Claude выступает как команда разработки + архитектуры + QA.
- **Формат итерации (≈1 час, ≈3–4 часа в неделю):**
  1. Пользователь спрашивает «на чём мы закончили» → отвечаю: что сделано по коду в прошлый раз
  2. Формируем пул задач на итерацию
  3. Claude задаёт уточняющие вопросы
  4. На «как ты меня понял» — выдаю полный план итерации одним блоком
  5. Пользователь соглашается → по команде пишу код
- **Тестируемость.** Каждый модуль/функция пишется так, чтобы можно было проверить изолированно:
  моки внешних зависимостей, smoke-тесты на `/status`, явные интерфейсы между слоями.
- **НИКОГДА не коммитить секреты.** `.env`, токены, `*.session`, VLESS-ссылку — только в `.env`
  на сервере. `.gitignore` настроен; проверять перед каждым коммитом.
- Коммиты — conventional commits (`feat:`, `fix:`, `chore:`, `docs:`). Версии — семвер-теги.
- Стек: Python 3.12 (ядро, Telegram, MAX), Node/Baileys (WhatsApp). Прокси — Caddy. Docker Compose.
- **Локальный тест WA:** `cd wa_adapter && SESSION_DIR=/tmp/wa-sessions CORE_URL=http://localhost:8000 PORT=9003 node index.js`

## Деплой

**Порядок всегда:** локально → GitHub (`origin`) → сервер. GitHub — источник правды.
GitHub: `git@github.com:aasamets/maxbridge.git` (SSH-ключ настроен).

```
git commit -am "feat: ..."
git push origin main
ssh root@YOUR_SERVER "cd /opt/maxbridge && git pull && docker compose up -d --build"
```

**Стратегия до стабильной сборки — чистые установки:**

1. Восстановить снапшот `infra-base` в панели провайдера
2. На сервере: `curl -fsSL https://raw.githubusercontent.com/aasamets/maxbridge/main/install.sh -o /tmp/install.sh && bash /tmp/install.sh`
   ⚠️ `bash <(curl ...)` не работает на Ubuntu (нет `/dev/fd`). Только двухшаговый вариант.
3. Установщик интерактивно запросит токены Битрикс24 и всё настроит
4. После установки — автодиагностика, вывод ссылки и сгенерированного пароля веб-морды

**Снапшот `infra-base`:** ufw (22/80/443), swap 2 ГБ, python3.12-venv, git, nodejs, npm, caddy.
**Не содержит:** SSH authorized_keys (снапшот не сохраняет), код, `.env`, `data/`, `*.session`.
**SSH после снапшота:** только по паролю. paramiko с `password=` для автоматизации.

## Команды (сервер)

```bash
cd /opt/maxbridge

docker compose ps                        # статус всех сервисов
docker compose logs -f                   # все логи в реальном времени
docker compose logs -f core              # логи одного сервиса
docker compose restart core              # перезапуск без пересборки
docker compose up -d --build             # пересборка + перезапуск
docker compose exec core bash            # shell внутри контейнера

free -h                                  # память
```
