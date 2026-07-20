#!/usr/bin/env bash
# MaxBridge — Коннектор мессенджеров → Битрикс24
# Установщик v0.2
# Запуск: bash <(curl -fsSL https://raw.githubusercontent.com/aasamets/maxbridge/main/install.sh)

set -uo pipefail
REPO="https://github.com/aasamets/maxbridge.git"
INSTALL_DIR="/opt/maxbridge"
BUILD_LOG="/tmp/maxbridge_build.log"

# ── Цвета ─────────────────────────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' B='\033[0;34m' C='\033[0;36m'
N='\033[0m' BOLD='\033[1m'

hr()   { echo -e "${B}────────────────────────────────────────────────────────${N}"; }
ok()   { printf "  ${G}✔${N} %s\n" "$*"; }
warn() { printf "  ${Y}⚠${N} %s\n" "$*"; }
fail() { printf "  ${R}✖${N} %s\n" "$*"; }
info() { printf "  ${C}→${N} %s\n" "$*"; }
step() { printf "\n${B}▸ %s${N}\n" "$*"; }

# ── Спиннер ───────────────────────────────────────────────────────────────────
_SP_PID=""; _SP_MSG=""

spinner_start() {
  _SP_MSG="$1"
  (
    local f='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
    while true; do
      printf "\r  ${C}${f:$i:1}${N} %s" "$_SP_MSG"
      i=$(( (i+1) % 10 ))
      sleep 0.1
    done
  ) &
  _SP_PID=$!
  disown "$_SP_PID" 2>/dev/null || true
}

spinner_stop() {
  [[ -z "${_SP_PID:-}" ]] && return
  kill "$_SP_PID" 2>/dev/null; wait "$_SP_PID" 2>/dev/null || true
  _SP_PID=""
  if [[ "${1:-ok}" == "ok" ]]; then
    printf "\r  ${G}✔${N} %s\n" "${2:-$_SP_MSG}"
  else
    printf "\r  ${R}✖${N} %s\n" "${2:-$_SP_MSG}"
  fi
}

# Убедиться что спиннер остановится при любом выходе
trap 'spinner_stop fail "Прервано" 2>/dev/null; exit 1' INT TERM

# ── Ввод значений ─────────────────────────────────────────────────────────────
_VAL=""
read_val() {
  local prompt="$1" default="${2:-}"
  if [[ -n "$default" ]]; then
    printf "  ${C}?${N} %s [%s]: " "$prompt" "$default"
  else
    printf "  ${C}?${N} %s: " "$prompt"
  fi
  IFS= read -r _VAL </dev/tty
  _VAL="${_VAL:-$default}"
}

_SECRET=""
read_secret() {
  local prompt="$1"
  printf "  ${C}?${N} %s: " "$prompt"
  IFS= read -rs _SECRET </dev/tty; echo
  local len=${#_SECRET}
  if [[ $len -gt 0 ]]; then
    printf "    ${G}●${N} получено %d символов\n" "$len"
  else
    printf "    ${Y}⚠${N}  поле пустое\n"
  fi
}

read_yn() {
  local prompt="$1" default="${2:-y}" ans
  printf "  ${C}?${N} %s (y/n) [%s]: " "$prompt" "$default"
  IFS= read -r ans </dev/tty
  ans="${ans:-$default}"
  [[ "${ans,,}" == "y" ]]
}

read_long() {
  # Для длинных строк (VLESS-ссылка): IFS= read -r и показать превью
  local prompt="$1"
  printf "  ${C}?${N} %s\n  ${C}→${N} " "$prompt"
  IFS= read -r _VAL </dev/tty
  local len=${#_VAL}
  if [[ $len -gt 0 ]]; then
    printf "    ${G}●${N} %d символов: %.50s…\n" "$len" "$_VAL"
  fi
}

# ── Сборка одного образа с прогрессом ─────────────────────────────────────────
build_service() {
  local svc="$1" label="${2:-$1}"
  spinner_start "Сборка образа $label..."
  if docker compose build "$svc" >> "$BUILD_LOG" 2>&1; then
    spinner_stop ok "$label — образ готов"
    return 0
  else
    spinner_stop fail "$label — ошибка сборки"
    warn "Подробности: cat $BUILD_LOG"
    return 1
  fi
}

# ── Шапка ─────────────────────────────────────────────────────────────────────
clear
hr
echo -e "${BOLD}  MaxBridge — Установка v0.1.0${N}"
echo -e "  Коннектор мессенджеров → Битрикс24"
hr
echo "" > "$BUILD_LOG"

# ── Проверка прав ──────────────────────────────────────────────────────────────
step "Проверка окружения"
[[ $EUID -ne 0 ]] && { fail "Запустите скрипт от root"; exit 1; }
[[ "$(uname -s)" != "Linux" ]] && { fail "Требуется Linux"; exit 1; }
ok "root, Linux $(lsb_release -rs 2>/dev/null || uname -r)"

# ── Docker ────────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
  step "Установка Docker"
  spinner_start "Загрузка и установка Docker..."
  curl -fsSL https://get.docker.com | sh >> "$BUILD_LOG" 2>&1
  systemctl enable --now docker >> "$BUILD_LOG" 2>&1
  spinner_stop ok "Docker установлен"
else
  ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
fi

if ! docker compose version &>/dev/null; then
  spinner_start "Установка Docker Compose plugin..."
  apt-get install -y docker-compose-plugin >> "$BUILD_LOG" 2>&1 || true
  spinner_stop ok "Docker Compose установлен"
else
  ok "Docker Compose $(docker compose version --short 2>/dev/null || echo 'OK')"
fi

# ── Код ───────────────────────────────────────────────────────────────────────
step "Код приложения"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  spinner_start "Обновление репозитория..."
  git -C "$INSTALL_DIR" pull --quiet >> "$BUILD_LOG" 2>&1
  spinner_stop ok "Репозиторий обновлён ($INSTALL_DIR)"
else
  spinner_start "Клонирование репозитория..."
  git clone --quiet "$REPO" "$INSTALL_DIR" >> "$BUILD_LOG" 2>&1
  spinner_stop ok "Репозиторий клонирован ($INSTALL_DIR)"
fi
cd "$INSTALL_DIR"

# ══════════════════════════════════════════════════════════════════════════════
hr
echo -e "\n${BOLD}  Конфигурация${N}"
echo -e "  Заполните параметры. Enter = оставить значение по умолчанию."
hr

# ── Секция 1: Общие настройки ─────────────────────────────────────────────────
while true; do
  step "Общие настройки"
  read_val "Домен сервера (без https://)" "connector.company.ru"
  PUBLIC_DOMAIN="$_VAL"
  PUBLIC_URL="https://${PUBLIC_DOMAIN}"

  echo
  echo -e "  ${BOLD}Проверьте:${N}"
  echo -e "  Домен:  $PUBLIC_DOMAIN"
  echo -e "  URL:    $PUBLIC_URL"
  echo
  printf "  Всё верно? [Enter — продолжить, R — повторить]: "
  IFS= read -r _ans </dev/tty
  [[ "${_ans,,}" != "r" ]] && break
done
ok "Домен: $PUBLIC_DOMAIN"

# ── Секция 2: Битрикс24 ───────────────────────────────────────────────────────
while true; do
  step "Битрикс24"
  echo
  echo -e "  ${Y}Где найти данные:${N}"
  echo -e "  Битрикс24 → Разработчикам → Другое → Локальные приложения → MaxBridge"
  echo -e "  Код и ключ приложения — на странице приложения."
  echo -e "  Номер линии: CRM → Контакт-центр → Открытые линии → цифра в URL /edit/${C}N${N}/"
  echo

  read_val "Домен портала Битрикс24" "yourcompany.bitrix24.ru"
  B24_DOMAIN="$_VAL"

  read_val "Код приложения (client_id)" ""
  B24_CLIENT_ID="$_VAL"

  read_secret "Ключ приложения (client_secret)"
  B24_CLIENT_SECRET="$_SECRET"

  read_val "Номер Открытой линии" "2"
  B24_LINE_ID="$_VAL"

  echo
  echo -e "  ${BOLD}Проверьте:${N}"
  echo -e "  Домен:       $B24_DOMAIN"
  echo -e "  Client ID:   $B24_CLIENT_ID"
  echo -e "  Secret:      ${G}●${N} (${#B24_CLIENT_SECRET} символов)"
  echo -e "  Линия:       #$B24_LINE_ID"
  echo
  printf "  Всё верно? [Enter — продолжить, R — повторить]: "
  IFS= read -r _ans </dev/tty
  [[ "${_ans,,}" != "r" ]] && break
done
B24_CONNECTOR_ID="maxbridge"
ok "Битрикс24: $B24_DOMAIN, линия #$B24_LINE_ID"

# ── Секция 3: Каналы ──────────────────────────────────────────────────────────
step "Подключаемые каналы"
echo -e "  ${Y}Каналы подключаются ПОСЛЕ установки через веб-интерфейс.${N}"
echo -e "  Здесь выбираем какие адаптеры запускать."
echo

WA_ENABLED=false; MAX_ENABLED=false; TG_ENABLED=false
TG_API_ID=""; TG_API_HASH=""; VLESS_URL=""
GREENAPI_ID_INSTANCE=""; GREENAPI_TOKEN=""

if read_yn "Включить WhatsApp?"; then WA_ENABLED=true; ok "WhatsApp: включён"; fi
if read_yn "Включить MAX?";      then MAX_ENABLED=true; ok "MAX: включён"; fi
if read_yn "Включить Telegram?"; then TG_ENABLED=true;  ok "Telegram: включён"; fi

# ── Секция 3б: MAX (если включён) ────────────────────────────────────────────
if [[ "$MAX_ENABLED" == "true" ]]; then
  while true; do
    step "MAX — GREEN-API настройки"
    echo
    echo -e "  ${Y}MAX подключается через сервис GREEN-API (green-api.com/max).${N}"
    echo -e "  Шаги:"
    echo -e "   1. Зарегистрируйтесь на ${C}green-api.com/max${N}"
    echo -e "   2. Создайте инстанс, отсканируйте QR в приложении MAX"
    echo -e "   3. Скопируйте ${C}idInstance${N} и ${C}apiTokenInstance${N}"
    echo -e "  (Если ключей пока нет — оставьте поля пустыми, заполните в настройках позже.)"
    echo

    read_val "idInstance (число, например: 1101234567)" ""
    GREENAPI_ID_INSTANCE="$_VAL"

    read_secret "apiTokenInstance"
    GREENAPI_TOKEN="$_SECRET"

    echo
    echo -e "  ${BOLD}Проверьте:${N}"
    echo -e "  idInstance: ${GREENAPI_ID_INSTANCE:-${Y}пусто${N}}"
    echo -e "  Token:      ${G}●${N} (${#GREENAPI_TOKEN} символов)"
    echo
    printf "  Всё верно? [Enter — продолжить, R — повторить]: "
    IFS= read -r _ans </dev/tty
    [[ "${_ans,,}" != "r" ]] && break
  done
fi

# ── Секция 4: Telegram (если включён) ────────────────────────────────────────
if [[ "$TG_ENABLED" == "true" ]]; then
  while true; do
    step "Telegram — настройки"
    echo
    echo -e "  ${Y}Нужен USERBOT-доступ (не бот-токен от @BotFather).${N}"
    echo -e "  Это позволяет принимать сообщения на номер телефона."
    echo
    echo -e "  Как получить api_id и api_hash:"
    echo -e "   1. Зайдите на ${C}my.telegram.org${N} — тем же номером что у аккаунта"
    echo -e "   2. ${C}API development tools${N} → Create application (название любое)"
    echo -e "   3. Скопируйте ${C}App api_id${N} (число) и ${C}App api_hash${N} (32 символа)"
    echo

    read_val "App api_id (число, например: 1234567)" ""
    TG_API_ID="$_VAL"

    read_val "App api_hash (32 символа)" ""
    TG_API_HASH="$_VAL"

    echo
    echo -e "  ${Y}VLESS-прокси:${N} обязателен в РФ, Telegram заблокирован."
    echo -e "  Формат: vless://UUID@HOST:PORT?security=reality&..."
    read_long "Вставьте VLESS-ссылку целиком и нажмите Enter"
    VLESS_URL="$_VAL"

    echo
    echo -e "  ${BOLD}Проверьте:${N}"
    echo -e "  api_id:   ${TG_API_ID:-${Y}пусто${N}}"
    echo -e "  api_hash: ${TG_API_HASH:0:8}... (${#TG_API_HASH} символов)"
    echo -e "  VLESS:    ${VLESS_URL:0:50}… (${#VLESS_URL} символов)"
    echo
    printf "  Всё верно? [Enter — продолжить, R — повторить]: "
    IFS= read -r _ans </dev/tty
    [[ "${_ans,,}" != "r" ]] && break
  done
fi

# ── Пароль — генерируется автоматически ──────────────────────────────────────
step "Доступ к веб-интерфейсу"
_USERNAMES=(
  atomic-babushka wild-samovar cosmic-napoleon furious-valenki
  brave-matryoshka lazy-borscht electric-sputnik chaotic-balalaika
  mighty-pelmeni speedy-kolobok golden-sarafan ancient-buran
  funky-medvedev spicy-okroshka turbo-vatrushka silent-kremlin
  daring-ushanka radical-kefir stellar-blini nuclear-kalinka
  rapid-borshchevik floating-galosh supreme-kvass jazzy-bubliki
  toxic-lapti colossal-samovar rogue-vodyanoy phantom-kikimora
  blazing-domovoi epic-zefir mystic-tushonka clever-sibiryak
  bold-shapka grumpy-samovar rogue-bublik sleepy-valenok
  hyper-ogurec astro-pelmen turbo-blincik neon-kalachik
)
ADMIN_USER="${_USERNAMES[RANDOM % ${#_USERNAMES[@]}]}"
ADMIN_PASS=$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20)
ok "Логин: $ADMIN_USER"
ok "Пароль сгенерирован автоматически (будет показан в конце)"

# ── Запись .env ───────────────────────────────────────────────────────────────
step "Запись конфигурации"
cat > "$INSTALL_DIR/.env" <<EOF
PUBLIC_URL=${PUBLIC_URL}
PUBLIC_DOMAIN=${PUBLIC_DOMAIN}

B24_DOMAIN=${B24_DOMAIN}
B24_CLIENT_ID=${B24_CLIENT_ID}
B24_CLIENT_SECRET=${B24_CLIENT_SECRET}
B24_LINE_ID=${B24_LINE_ID}
B24_CONNECTOR_ID=${B24_CONNECTOR_ID}
B24_APPLICATION_TOKEN=

ADAPTERS=whatsapp=http://wa:9003,max=http://max:9002,telegram=http://telegram:9001

TG_ENABLED=${TG_ENABLED}
TG_API_ID=${TG_API_ID}
TG_API_HASH=${TG_API_HASH}
TG_SESSION_FILE=/sessions/telegram.session
TG_PROXY_HOST=xray
TG_PROXY_PORT=1080
VLESS_URL=${VLESS_URL}

WA_ENABLED=${WA_ENABLED}
WA_SESSION_DIR=/sessions/wa
WA_PROXY_HOST=
WA_PROXY_PORT=1080

MAX_ENABLED=${MAX_ENABLED}
GREENAPI_ID_INSTANCE=${GREENAPI_ID_INSTANCE}
GREENAPI_TOKEN=${GREENAPI_TOKEN}
GREENAPI_WEBHOOK_URL=${PUBLIC_URL}/adapters/max/webhook

ADMIN_USER=${ADMIN_USER}
ADMIN_PASS=${ADMIN_PASS}
EOF
ok ".env записан"

# ── Caddyfile из шаблона ──────────────────────────────────────────────────────
spinner_start "Генерация Caddyfile..."
sed \
  -e "s|__DOMAIN__|${PUBLIC_DOMAIN}|g" \
  "${INSTALL_DIR}/Caddyfile.template" > "${INSTALL_DIR}/Caddyfile"
spinner_stop ok "Caddyfile сгенерирован"

# ── Xray конфиг ───────────────────────────────────────────────────────────────
if [[ "$TG_ENABLED" == "true" && -n "${VLESS_URL:-}" ]]; then
  spinner_start "Настройка Xray (Telegram proxy)..."
  python3 - <<PYEOF 2>>"$BUILD_LOG" && spinner_stop ok "Xray настроен" || spinner_stop fail "Xray — ошибка парсинга VLESS"
import re, json, sys
url = r"""${VLESS_URL}"""
m = re.match(r'vless://([^@]+)@([^:]+):(\d+)\?(.*?)(?:#.*)?$', url.strip())
if not m: sys.exit(1)
uid, host, port, qs = m.group(1), m.group(2), int(m.group(3)), m.group(4)
params = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
cfg_path = '${INSTALL_DIR}/xray/config.json'
with open(cfg_path) as f: cfg = json.load(f)
cfg['outbounds'][0]['settings']['vnext'][0].update(address=host, port=port)
cfg['outbounds'][0]['settings']['vnext'][0]['users'][0]['id'] = uid
rs = cfg['outbounds'][0]['streamSettings']['realitySettings']
rs.update(serverName=params.get('sni','github.com'), fingerprint=params.get('fp','firefox'),
          publicKey=params.get('pbk',''), shortId=params.get('sid',''))
with open(cfg_path, 'w') as f: json.dump(cfg, f, indent=2)
PYEOF
fi

# ── Сборка образов (последовательно) ──────────────────────────────────────────
step "Сборка Docker-образов"
echo -e "  ${Y}Собираем по одному — это займёт несколько минут.${N}"
echo

build_service core     "Ядро (core)"
[[ "$TG_ENABLED"  == "true" ]] && build_service telegram "Telegram"
[[ "$MAX_ENABLED" == "true" ]] && build_service max      "MAX"
[[ "$WA_ENABLED"  == "true" ]] && build_service wa       "WhatsApp"
build_service caddy    "Caddy (прокси)" || true  # caddy: готовый образ, не строим

# ── Запуск контейнеров ────────────────────────────────────────────────────────
step "Запуск сервисов"
spinner_start "Запуск контейнеров..."
docker compose up -d >> "$BUILD_LOG" 2>&1
spinner_stop ok "Контейнеры запущены"

spinner_start "Ожидание инициализации сервисов..."
sleep 12
spinner_stop ok "Готово"

# ── Диагностика ───────────────────────────────────────────────────────────────
hr
printf "\n${BOLD}  Диагностика${N}\n\n"

DIAG_FAILED=false

diag() {
  local label="$1"; shift
  if eval "$*" >> "$BUILD_LOG" 2>&1; then
    ok "$label"
  else
    fail "$label"
    DIAG_FAILED=true
  fi
}

diag "Docker daemon"     "docker info"
diag "Контейнер: core"   "docker compose ps core   2>/dev/null | grep -qiE 'running|up'"
diag "Контейнер: caddy"  "docker compose ps caddy  2>/dev/null | grep -qiE 'running|up'"
[[ "$WA_ENABLED"  == "true" ]] && diag "Контейнер: wa"       "docker compose ps wa       2>/dev/null | grep -qiE 'running|up'"
[[ "$MAX_ENABLED" == "true" ]] && diag "Контейнер: max"      "docker compose ps max      2>/dev/null | grep -qiE 'running|up'"
[[ "$TG_ENABLED"  == "true" ]] && diag "Контейнер: telegram" "docker compose ps telegram 2>/dev/null | grep -qiE 'running|up'"
[[ "$TG_ENABLED"  == "true" ]] && diag "Контейнер: xray"    "docker compose ps xray    2>/dev/null | grep -qiE 'running|up'"

if host "$PUBLIC_DOMAIN" >> "$BUILD_LOG" 2>&1 || nslookup "$PUBLIC_DOMAIN" >> "$BUILD_LOG" 2>&1; then
  ok "DNS: $PUBLIC_DOMAIN резолвится"
else
  warn "DNS: $PUBLIC_DOMAIN не резолвится (добавьте A-запись)"
fi

if curl -fsS --max-time 10 "http://localhost:8000/api/status" >> "$BUILD_LOG" 2>&1; then
  ok "API ядра: /api/status отвечает"
else
  warn "API ядра не отвечает — проверьте: docker compose logs core"
  DIAG_FAILED=true
fi

if curl -fsS --max-time 15 "${PUBLIC_URL}/api/status" >> "$BUILD_LOG" 2>&1; then
  ok "HTTPS: $PUBLIC_URL доступен"
else
  warn "HTTPS пока не доступен (нормально если DNS свежий)"
fi

# ── Финал ─────────────────────────────────────────────────────────────────────
echo
hr
if [[ "$DIAG_FAILED" == "false" ]]; then
  echo -e "${G}${BOLD}  Установка завершена успешно! ✓${N}"
else
  echo -e "${Y}${BOLD}  Установка завершена с предупреждениями.${N}"
  echo -e "  Детали: ${C}cat $BUILD_LOG${N}"
fi
hr
printf "\n"
printf "  ${BOLD}Доступ к веб-интерфейсу:${N}\n"
printf "\n"
printf "  Адрес:  ${C}%s${N}\n" "$PUBLIC_URL"
printf "  Логин:  ${BOLD}%s${N}\n" "$ADMIN_USER"
printf "  Пароль: ${BOLD}%s${N}\n" "$ADMIN_PASS"
printf "\n"
printf "  ${Y}Сохраните пароль — он больше не будет показан!${N}\n"
printf "\n"
hr
printf "\n"
printf "  ${BOLD}Следующие шаги:${N}\n"
printf "  1. Откройте %s и войдите\n" "$PUBLIC_URL"
printf "  2. Нажмите «Авторизовать приложение» (Битрикс24 OAuth, один раз)\n"
printf "  3. В каждой карточке нажмите «Подключить» — поднесите телефон\n"
printf "  4. Битрикс → Контакт-центр → привяжите коннекторы к линии #%s\n" "$B24_LINE_ID"
printf "\n"
printf "  Логи: ${C}docker compose -f %s/docker-compose.yml logs -f${N}\n" "$INSTALL_DIR"
printf "\n"
hr
