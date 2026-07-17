'use strict';

/**
 * WhatsApp адаптер на Baileys (без Chromium, только WebSocket).
 *
 * Контракт:
 *   GET  /status  → {state: "connected|needs_auth|unavailable"}
 *   GET  /qr      → PNG QR-код (пока не подключён)
 *   POST /send    → {peer_id: "79990000000", text: "..."}
 *   POST /logout  → сбросить сессию
 *
 * Входящие шлём в ядро: POST {CORE_URL}/incoming
 */

const express = require('express');
const axios   = require('axios');
const QRCode  = require('qrcode');
const pino    = require('pino');
const path    = require('path');
const fs      = require('fs');

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  isJidUser,
} = require('@whiskeysockets/baileys');
const { SocksProxyAgent } = require('socks-proxy-agent');

const CORE_URL      = (process.env.CORE_URL    || 'http://core:8000').replace(/\/$/, '');
const ADAPTER_NAME  = process.env.ADAPTER_NAME || 'whatsapp';
const SESSION_DIR   = process.env.SESSION_DIR  || '/sessions/wa';
const PORT          = parseInt(process.env.PORT || '9003', 10);
const WA_PROXY_HOST = process.env.WA_PROXY_HOST || '';
const WA_PROXY_PORT = process.env.WA_PROXY_PORT || '1080';

const agent = WA_PROXY_HOST
  ? new SocksProxyAgent(`socks5://${WA_PROXY_HOST}:${WA_PROXY_PORT}`)
  : undefined;

if (agent) console.log(`[WA] Используем прокси ${WA_PROXY_HOST}:${WA_PROXY_PORT}`);

const logger = pino({ level: 'warn' });
const app    = express();
app.use(express.json());

// ── Состояние ─────────────────────────────────────────────────────────────────

let sock        = null;
let currentQR   = null;  // raw QR string от Baileys
let state       = 'needs_auth';

// ── Baileys ───────────────────────────────────────────────────────────────────

async function connectWA() {
  fs.mkdirSync(SESSION_DIR, { recursive: true });

  const { state: authState, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  let version;
  try {
    ({ version } = await fetchLatestBaileysVersion());
  } catch (e) {
    console.warn('[WA] fetchLatestBaileysVersion недоступен, используем встроенную версию');
  }

  sock = makeWASocket({
    version,
    auth:           authState,
    logger,
    printQRInTerminal: false,
    browser:        ['MaxBridge', 'Chrome', '120.0.0'],
    syncFullHistory: false,
    agent,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', ({ connection, qr, lastDisconnect }) => {
    if (qr) {
      currentQR = qr;
      state     = 'needs_auth';
    }

    if (connection === 'open') {
      currentQR = null;
      state     = 'connected';
      console.log('[WA] Подключён');
    }

    if (connection === 'close') {
      const code      = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = code === DisconnectReason.loggedOut;
      // При любом дисконнекте показываем needs_auth (QR-виджет), не 'unavailable'.
      // 'unavailable' оставляем только для критических сбоев (нет ответа адаптера).
      state = 'needs_auth';
      console.log(`[WA] Отключён (${code}), повтор=${!loggedOut}`);
      if (loggedOut) {
        // Сессия сброшена на стороне WhatsApp — чистим локальные файлы
        fs.rmSync(SESSION_DIR, { recursive: true, force: true });
      } else {
        setTimeout(connectWA, 5000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return;
    for (const msg of messages) {
      if (!msg.message || msg.key.fromMe) continue;
      if (!isJidUser(msg.key.remoteJid))  continue;  // только личные чаты

      const jid      = msg.key.remoteJid;                  // "79990000000@s.whatsapp.net"
      const phone    = '+' + jid.replace('@s.whatsapp.net', '');
      const text     = extractText(msg.message);
      const pushName = msg.pushName || null;

      try {
        await axios.post(`${CORE_URL}/incoming`, {
          adapter:  ADAPTER_NAME,
          peer_id:  phone,       // используем номер как peer_id для CRM-маппинга
          msg_id:   msg.key.id,
          text,
          name:     pushName,
          phone,
        });
      } catch (e) {
        console.error('[WA] Ошибка отправки в core:', e.message);
      }
    }
  });
}

function extractText(message) {
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    '[медиафайл]'
  );
}

// ── HTTP API ──────────────────────────────────────────────────────────────────

app.get('/status', (req, res) => {
  res.json({ state });
});

app.get('/qr', async (req, res) => {
  if (state === 'connected') {
    return res.json({ state: 'connected' });
  }
  if (!currentQR) {
    return res.status(202).json({ state, hint: 'QR ещё не готов — подождите несколько секунд' });
  }
  try {
    const png = await QRCode.toBuffer(currentQR, { type: 'png', width: 300, margin: 2 });
    res.set('Content-Type', 'image/png').send(png);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/send', async (req, res) => {
  const { peer_id, text } = req.body;
  if (state !== 'connected' || !sock) {
    return res.status(503).json({ error: 'not connected' });
  }
  try {
    // peer_id может быть "+79990000000" или уже "79990000000@s.whatsapp.net"
    const jid = peer_id.includes('@') ? peer_id : peer_id.replace(/^\+/, '') + '@s.whatsapp.net';
    await sock.sendMessage(jid, { text });
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.post('/logout', async (req, res) => {
  try {
    if (sock) await sock.logout();
  } catch (_) {}
  fs.rmSync(SESSION_DIR, { recursive: true, force: true });
  state = 'needs_auth';
  setTimeout(connectWA, 1000);
  res.json({ ok: true });
});

// /login и /code — WhatsApp не использует SMS-код, QR достаточно
app.post('/login',    (req, res) => res.json({ ok: true, state }));
app.post('/code',     (req, res) => res.json({ ok: true, state }));
app.post('/password', (req, res) => res.json({ ok: true, state }));

// ── Старт ─────────────────────────────────────────────────────────────────────

app.listen(PORT, () => console.log(`[WA] Адаптер запущен на порту ${PORT}`));

function startWA() {
  connectWA().catch(e => {
    console.error('[WA] Ошибка запуска, повтор через 10с:', e.message);
    setTimeout(startWA, 10000);
  });
}
startWA();
