'use strict';

// ── Константы ────────────────────────────────────────────────────────────────

const ADAPTERS = ['whatsapp', 'max', 'telegram'];

const STATE_LABELS = {
  connected:    'Подключён',
  needs_auth:   'Не подключён',
  needs_code:   'Ожидание кода',
  needs_password: 'Нужен пароль',
  unavailable:  'Недоступен',
  unknown:      'Загрузка...',
};

const STATE_DOT = {
  connected:    'green',
  needs_auth:   '',
  needs_code:   'yellow',
  needs_password: 'yellow',
  unavailable:  'red',
  unknown:      '',
};

// ── Retry state ───────────────────────────────────────────────────────────────

const _lastState    = {};  // { adapter: state }
const _pendingRetry = {};  // { adapter: { action, phone, startedAt } }
const _retryTimers  = {};

function _scheduleRetryRender(adapter) {
  clearTimeout(_retryTimers[adapter]);
  _retryTimers[adapter] = setTimeout(() => {
    const st = _lastState[adapter];
    if (st && st !== 'connected') updateCard(adapter, st);
  }, 60000);
}

function _setRetry(adapter, action, phone) {
  _pendingRetry[adapter] = { action, phone, startedAt: Date.now() };
  _scheduleRetryRender(adapter);
}

function _clearRetry(adapter) {
  clearTimeout(_retryTimers[adapter]);
  delete _pendingRetry[adapter];
}

function _retryBlock(adapter) {
  const r = _pendingRetry[adapter];
  if (!r || Date.now() - r.startedAt < 60000) return '';
  return `<p class="msg-info" style="margin-top:8px">Не пришёл код? <button class="btn btn-secondary btn-sm" onclick="retryAction('${adapter}')">Запросить повторно</button></p>`;
}

async function retryAction(adapter) {
  const r = _pendingRetry[adapter];
  if (!r) return;
  r.startedAt = Date.now();
  _scheduleRetryRender(adapter);
  if (r.action === 'phone') {
    const res = await post(`/adapters/${adapter}/login`, { phone: r.phone });
    toast(res.ok ? 'Код запрошен повторно' : 'Ошибка: ' + (res.error || 'неизвестная'));
  }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'status') {
      ADAPTERS.forEach(name => {
        const state = msg.adapters[name] || 'unknown';
        updateCard(name, state);
      });
    }
    if (msg.type === 'proxy') {
      updateProxyCard(msg.status);
    }
  };

  ws.onclose = () => setTimeout(connectWS, 3000);
}

// Docker Compose service name per adapter (для подсказки в логах)
const SERVICE_NAME = { whatsapp: 'wa', max: 'max', telegram: 'telegram' };

// ── Статус карточки ───────────────────────────────────────────────────────────

function updateCard(name, state) {
  const prev = _lastState[name];
  _lastState[name] = state;
  if (state === 'connected') _clearRetry(name);

  const dot   = document.getElementById(`dot-${name}`);
  const label = document.getElementById(`label-${name}`);
  const body  = document.getElementById(`body-${name}`);
  if (!dot) return;

  dot.className = 'status-dot ' + (STATE_DOT[state] || '');
  label.textContent = STATE_LABELS[state] || state;

  // Перерисовываем body только при смене состояния — иначе сбрасываются поля ввода
  if (state !== prev) {
    body.innerHTML = renderBody(name, state);
  }
}

function renderBody(name, state) {
  if (state === 'connected') {
    return `<div class="connected-state">
      <span class="connected-check">✓</span>
      <span class="connected-text">Мессенджер подключён и принимает сообщения</span>
      <button class="btn btn-secondary btn-sm" onclick="disconnectAdapter('${name}')">Отключить</button>
    </div>`;
  }

  if (state === 'unavailable') {
    const svc = SERVICE_NAME[name] || name;
    return `<p class="msg-error">Адаптер не отвечает. Проверьте логи:<br><code>docker compose -f /opt/maxbridge/docker-compose.yml logs ${svc}</code></p>`;
  }

  // WhatsApp — QR
  if (name === 'whatsapp') {
    if (state === 'needs_auth') {
      return `<div>
        <p class="msg-info">Отсканируйте QR-код в приложении WhatsApp:<br>
        Три точки → Связанные устройства → Привязать устройство</p>
        <div class="qr-wrap">
          <img src="/adapters/whatsapp/qr?t=${Date.now()}" alt="QR" onerror="this.parentElement.innerHTML='<p class=msg-error>QR недоступен — запустите адаптер</p>'">
        </div>
        <button class="btn btn-secondary btn-sm" onclick="refreshQR('whatsapp')">Обновить QR</button>
      </div>`;
    }
  }

  // MAX — телефон + код
  if (name === 'max') {
    if (state === 'needs_auth') {
      return `<div class="auth-form">
        <p class="msg-info">Введите номер телефона аккаунта MAX</p>
        <input class="input" id="max-phone" type="tel" placeholder="+79990000000" autocomplete="tel">
        <button class="btn btn-primary" onclick="sendPhone('max')">Получить код</button>
      </div>`;
    }
    if (state === 'needs_code') {
      return `<div class="auth-form">
        <p class="msg-info">Введите код из SMS</p>
        <input class="input" id="max-code" type="text" placeholder="12345" inputmode="numeric">
        <button class="btn btn-primary" onclick="sendCode('max')">Войти</button>
        ${_retryBlock(name)}
      </div>`;
    }
  }

  // Telegram — телефон + код
  if (name === 'telegram') {
    if (state === 'needs_auth') {
      return `<div class="auth-form">
        <p class="msg-info">Введите номер телефона аккаунта Telegram</p>
        <input class="input" id="telegram-phone" type="tel" placeholder="+79990000000" autocomplete="tel">
        <button class="btn btn-primary" onclick="sendPhone('telegram')">Получить код</button>
      </div>`;
    }
    if (state === 'needs_code') {
      return `<div class="auth-form">
        <p class="msg-info">Введите код из SMS или Telegram-уведомления</p>
        <input class="input" id="telegram-code" type="text" placeholder="12345" inputmode="numeric">
        <button class="btn btn-primary" onclick="sendCode('telegram')">Войти</button>
        ${_retryBlock(name)}
      </div>`;
    }
    if (state === 'needs_password') {
      return `<div class="auth-form">
        <p class="msg-info">Введите пароль двухфакторной аутентификации</p>
        <input class="input" id="telegram-password" type="password" placeholder="Пароль 2FA">
        <button class="btn btn-primary" onclick="sendPassword('telegram')">Подтвердить</button>
      </div>`;
    }
  }

  return '';
}

// ── Действия с адаптерами ─────────────────────────────────────────────────────

async function sendPhone(adapter) {
  const input = document.getElementById(`${adapter}-phone`);
  if (!input || !input.value.trim()) { toast('Введите номер телефона'); return; }
  const phone = input.value.trim();
  const body  = document.getElementById(`body-${adapter}`);
  body.innerHTML = '<p class="msg-info">Отправляем запрос...</p>';
  const res = await post(`/adapters/${adapter}/login`, { phone });
  if (!res.ok) toast('Ошибка: ' + (res.error || 'неизвестная'));
  else _setRetry(adapter, 'phone', phone);
}

async function sendCode(adapter) {
  const input = document.getElementById(`${adapter}-code`);
  if (!input || !input.value.trim()) { toast('Введите код'); return; }
  const body  = document.getElementById(`body-${adapter}`);
  body.innerHTML = '<p class="msg-info">Проверяем код...</p>';
  const res = await post(`/adapters/${adapter}/code`, { code: input.value.trim() });
  if (!res.ok) toast('Ошибка: ' + (res.error || 'неизвестная'));
}

async function sendPassword(adapter) {
  const input = document.getElementById(`${adapter}-password`);
  if (!input || !input.value.trim()) { toast('Введите пароль'); return; }
  const res = await post(`/adapters/${adapter}/password`, { password: input.value.trim() });
  if (!res.ok) toast('Ошибка: ' + (res.error || 'неизвестная'));
}

async function disconnectAdapter(adapter) {
  if (!confirm(`Отключить ${adapter}? Сессия будет удалена.`)) return;
  const res = await post(`/adapters/${adapter}/logout`, {});
  toast(res.ok ? 'Отключено' : 'Ошибка при отключении');
}

function refreshQR(adapter) {
  const img = document.querySelector(`#body-${adapter} img`);
  if (img) img.src = `/adapters/${adapter}/qr?t=${Date.now()}`;
}

// ── Битрикс24 OAuth ───────────────────────────────────────────────────────────

async function openOAuth() {
  const res = await fetch('/api/oauth_url').then(r => r.json());
  if (res.url) {
    window.open(res.url, '_blank');
    document.getElementById('label-b24').textContent = 'Ожидание авторизации...';
    document.getElementById('dot-b24').className = 'status-dot yellow';
  }
}

// Проверить OAuth на старте (если редирект вернул ?oauth=ok)
if (new URLSearchParams(location.search).get('oauth') === 'ok') {
  history.replaceState({}, '', '/');
  setTimeout(() => {
    document.getElementById('label-b24').textContent = 'Авторизован';
    document.getElementById('dot-b24').className = 'status-dot green';
    toast('Битрикс24 успешно авторизован');
  }, 300);
}

// ── Настройки ─────────────────────────────────────────────────────────────────

let _settings = {};

function toggleSettings() {
  const sec = document.getElementById('settings-section');
  const visible = sec.style.display !== 'none';
  sec.style.display = visible ? 'none' : 'block';
  if (!visible) loadSettings();
}

async function loadSettings() {
  const form = document.getElementById('settings-form');
  form.innerHTML = '<div class="spinner">Загрузка...</div>';
  _settings = await fetch('/api/settings').then(r => r.json());
  form.innerHTML = renderSettingsForm(_settings);
}

function renderSettingsForm(s) {
  const field = (key, label, hint = '', type = 'text') => `
    <div class="setting-group">
      <label for="s-${key}">${label}</label>
      <input class="input" id="s-${key}" name="${key}" type="${type}"
             value="${esc(s[key] || '')}" autocomplete="off">
      ${hint ? `<span class="hint">${hint}</span>` : ''}
    </div>`;

  const secret = (key, label, hint = '') => `
    <div class="setting-group">
      <label for="s-${key}">${label}</label>
      <input class="input" id="s-${key}" name="${key}" type="password"
             value="${esc(s[key] || '')}" placeholder="оставьте *** чтобы не менять" autocomplete="new-password">
      ${hint ? `<span class="hint">${hint}</span>` : ''}
    </div>`;

  return `
    <h3 style="font-size:13px;font-weight:600;color:var(--text-2);margin-bottom:4px">Битрикс24</h3>
    ${field('B24_DOMAIN',       'Домен портала',            'ваш-портал.bitrix24.ru')}
    ${field('B24_CLIENT_ID',    'ID приложения (client_id)', 'local.XXXXX — из страницы локального приложения')}
    ${secret('B24_CLIENT_SECRET','Ключ приложения (client_secret)', '*** = не менять; вставьте новый чтобы обновить')}
    ${field('B24_LINE_ID',      'ID Открытой линии',        'число из URL линии')}

    <h3 style="font-size:13px;font-weight:600;color:var(--text-2);margin:8px 0 4px">Каналы</h3>
    ${field('TG_API_ID',   'Telegram API ID',   'my.telegram.org → API development tools')}
    ${field('TG_API_HASH', 'Telegram API Hash', '')}
    ${field('VLESS_URL',   'VLESS-ссылка (прокси Telegram)', 'vless://...')}

    <h3 style="font-size:13px;font-weight:600;color:var(--text-2);margin:8px 0 4px">Общие</h3>
    ${field('PUBLIC_URL',  'Публичный URL', '')}
  `;
}

async function saveSettings() {
  const form = document.getElementById('settings-form');
  const inputs = form.querySelectorAll('input[name]');
  const data = {};
  inputs.forEach(i => { data[i.name] = i.value; });

  const note = document.getElementById('settings-note');
  note.textContent = 'Сохраняем...';
  note.className = 'settings-note';

  const res = await fetch('/api/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  }).then(r => r.json());

  if (res.ok) {
    note.textContent = '✓ Сохранено. ' + (res.note || '');
    note.className = 'settings-note msg-success';
  } else {
    note.textContent = 'Ошибка: ' + (res.error || 'неизвестная');
    note.className = 'settings-note msg-error';
  }
}

// ── Утилиты ───────────────────────────────────────────────────────────────────

async function post(url, data) {
  const body = new URLSearchParams(data);
  const res  = await fetch(url, { method: 'POST', body });
  return res.json().catch(() => ({ ok: false }));
}

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function toast(msg, duration = 2500) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast'; el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), duration);
}

// ── Прокси-статус ─────────────────────────────────────────────────────────────

function updateProxyCard(s) {
  const dot   = document.getElementById('dot-proxy');
  const label = document.getElementById('label-proxy');
  const body  = document.getElementById('body-proxy');
  if (!dot) return;

  if (s.state === 'disabled') {
    dot.className = 'status-dot';
    label.textContent = 'Не настроен';
    body.innerHTML = '<p class="msg-info">VLESS_URL не задан — прокси отключён (Telegram недоступен)</p>';
    return;
  }
  if (s.state === 'checking') {
    dot.className = 'status-dot yellow';
    label.textContent = 'Проверка...';
    body.innerHTML = '';
    return;
  }
  if (s.ok) {
    dot.className = 'status-dot green';
    label.textContent = s.latency_ms ? `Работает · ${s.latency_ms} мс` : 'Работает';
    body.innerHTML = '<p class="msg-success">Прокси доступен — заблокированные ресурсы проходят</p>';
  } else {
    dot.className = 'status-dot red';
    label.textContent = 'Недоступен';
    const hint = s.error ? `<br><code style="font-size:12px">${esc(s.error)}</code>` : '';
    body.innerHTML = `<p class="msg-error">Прокси не работает${hint}</p>`;
  }
}

// ── Инициализация ─────────────────────────────────────────────────────────────

connectWS();
