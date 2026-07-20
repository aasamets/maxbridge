# CLAUDE.md — контекст проекта для Claude Code

Этот файл Claude Code читает в начале каждой сессии. Здесь — суть проекта, решения и
правила, чтобы не переобъяснять их каждый раз. Подробности — в `PROJECT_PLAN.md`.

## Что это за проект

Самописный коннектор: один телефонный номер с тремя мессенджерами (MAX, Telegram, WhatsApp) →
Открытая линия Битрикс24 → отдел продаж. Клиент пишет **на номер** в любом мессенджере,
сообщение попадает в Битрикс, менеджер отвечает из Битрикса, ответ возвращается клиенту.
Аналог Wazzup, только свой и без абонентки (только VPS + SIM).

## Ключевые архитектурные решения (не пересматривать без обсуждения)

- **Ядро messenger-агностично.** Вся логика Битрикса — в `core/`. Каждый мессенджер — отдельный
  адаптер с единым контрактом: `GET /status`, `GET /qr`, `POST /login`, `POST /code`,
  `POST /password`, `POST /send`; входящие адаптер шлёт на `core POST /incoming`.
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

## Порядок ввода каналов

1. **WhatsApp** (Baileys, Node) — напрямую, без прокси (не заблокирован в РФ). QR в веб-морде.
2. **MAX** (GREEN-API, Python) — через managed-шлюз green-api.com/max. Авторизация: QR в кабинете
   GREEN-API, затем `GREENAPI_ID_INSTANCE` + `GREENAPI_TOKEN` в `.env`. ~690 ₽/мес после бесплатного теста.
3. **Telegram — последним:** заблокирован в РФ, нужен прокси. Трафик адаптера идёт через
   локальный Xray-клиент (SOCKS5 `xray:1080` в Docker) → VLESS-Reality → заграничный сервер.
   Нужны `TG_API_ID` / `TG_API_HASH` от my.telegram.org (получить с российского IP через hosts-правило).

## Структура

```
core/main.py            FastAPI: /incoming, /bitrix/events, веб-морда (статус, QR, вход)
core/bitrix.py          клиент Битрикс24 (OAuth + imconnector)
core/store.py           SQLite: маппинг чатов, дедуп, токены
adapters/telegram_adapter.py  Telethon — прокси xray:1080, qr_login, ждёт TG_API_ID/HASH
adapters/max_adapter.py       GREEN-API клиент (max-api-client-python), polling
adapters/wa_adapter/ (Node)   Baileys, напрямую без прокси
install_connector.py    разовая регистрация коннектора + event.bind
Caddyfile / nginx.conf  обратный прокси: HTTPS + FastAPI session auth
.env.example            шаблон конфигурации (реальный .env — только на сервере)
```

## Инфраструктура (зафиксировано)

- Production VPS: домен и IP — в `.env` на сервере, не в Git.
  Ubuntu 26.04, 1 vCPU / 1 ГБ + 2 ГБ swap / 10 ГБ.
- Telegram-прокси: VLESS-Reality (ссылка — в `.env`, не в Git).
- Битрикс24: облако; коннектор Открытых линий через локальное приложение (scope
  `imconnector, imopenlines, crm, im`).

## Безопасность

- Ядро и адаптеры слушают только внутри Docker-сети; наружу — только Caddy (443/80).
- Веб-морда под FastAPI session auth (cookie `mb_session`, 8ч TTL). `/bitrix/events` и `/adapters/max/webhook` открыты; events проверяются `application_token`.
- Xray слушает SOCKS5 на `0.0.0.0:1080` внутри Docker-сети (чтобы wa/telegram могли достучаться по имени сервиса); наружу порт не пробрасывается.
- `ufw`: открыты 22/80/443. SSH — по ключу, без пароля/root.

## WhatsApp и прокси (фолбэк)

WA по умолчанию ходит напрямую (`WA_PROXY_HOST=` пусто). Если VPS-провайдер блокирует WA
на сетевом уровне (диагноз: `nc -z web.whatsapp.com 443` возвращает TIMEOUT), **быстрый фикс:**

```bash
# В .env на сервере добавить:
WA_PROXY_HOST=xray
# Затем пересоздать контейнер (не restart):
docker compose up -d wa
```

WA пойдёт через тот же VLESS-прокси что и Telegram. Минус: иностранный IP для WA — выше
риск бана аккаунта. Альтернатива — поднять отдельный SOCKS5 на домашней машине
(microsocks / 3proxy + порт-форвард на роутере) и указать его как `WA_PROXY_HOST`.

**Подтверждено (2026-07-20):** WA через VLESS-Reality + SOCKS5 xray работает корректно.
Baileys успешно проходит Noise Protocol handshake, генерирует QR-код. Ошибка 408
(connectionLost) — нормальное поведение: WhatsApp закрывает WebSocket через ~75 сек если
никто не отсканировал QR; адаптер переподключается и генерирует новый QR автоматически.
Диагностика через прокси из wa-контейнера: `wss://web.whatsapp.com/ws/chat` открывается
за ~450 мс (через xray → VLESS → 31.13.x.x).

**Диагностика WA:** если QR не появляется >5 минут:
```bash
# Из wa-контейнера: проверить что xray доступен
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
- **Локальный тест WA:** `cd wa_adapter && SESSION_DIR=/tmp/wa-sessions CORE_URL=http://localhost:8000 PORT=9003 node index.js` — проверить QR без сервера.

## Деплой

**Порядок всегда:** локально → GitHub (`origin`) → сервер. GitHub — источник правды.
GitHub: `git@github.com:aasamets/maxbridge.git` (SSH-ключ настроен).

```
git commit -am "feat: ..."
git push origin main                            # 1. бэкап + история
ssh root@YOUR_SERVER "cd /opt/maxbridge && git pull && docker compose up -d --build"
```

**Стратегия до стабильной сборки — чистые установки:**

До стабильной сборки каждая итерация деплоится на чистый сервер:
1. Восстановить снапшот `infra-base` в панели провайдера
2. На сервере: `curl -fsSL https://raw.githubusercontent.com/aasamets/maxbridge/main/install.sh -o /tmp/install.sh && bash /tmp/install.sh`
   ⚠️ `bash <(curl ...)` не работает на Ubuntu (нет `/dev/fd`). Только двухшаговый вариант.
3. Установщик интерактивно запросит токены Битрикс24 и всё настроит
4. После установки — автодиагностика, вывод ссылки и сгенерированного пароля веб-морды

**Снапшот `infra-base`:** SSH-ключ в `/root/.ssh/authorized_keys`, ufw (22/80/443), swap 2 ГБ,
python3.12-venv, git, nodejs, npm, caddy. Сервисы работают от root.

**Не содержит:** код, `.env`, `data/`, `*.session`.

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
