// agentkai WhatsApp sidecar — EXPERIMENTAL.
//
// This is a plain whatsapp-web.js client that exposes the tiny HTTP contract
// agentkai's WhatsAppBridge expects:
//
//   GET  /health                 -> 200 "ok"
//   GET  /events?since=<unix ts> -> {"events":[{"id","from","name","text","ts"}]}
//   POST /send {"to":"+15551234567","text":"..."} -> {"ok":true}
//
// Run:  npm install && node server.js
// Then scan the QR printed in the terminal with WhatsApp (Linked devices).
//
// WARNING: automating a personal WhatsApp account via the web client can get
// the number rate-limited or banned. Use a spare number, keep volumes low.

const express = require('express');
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');

const PORT = process.env.SIDECAR_PORT || 18791;
const events = [];          // in-memory inbox (sidecar restarts lose it)
const MAX_EVENTS = 500;

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: './.wwebjs_auth' }),
  puppeteer: { headless: true, args: ['--no-sandbox'] },
});

client.on('qr', (qr) => {
  console.log('Scan this QR with WhatsApp > Linked devices:');
  qrcode.generate(qr, { small: true });
});

client.on('ready', () => console.log('whatsapp sidecar ready'));

client.on('message', async (msg) => {
  if (msg.fromMe) return;
  const contact = await msg.getContact().catch(() => null);
  events.push({
    id: msg.id._serialized,
    from: '+' + (msg.from || '').replace(/@.*/, '').replace(/^\+/, ''),
    name: contact ? (contact.pushname || contact.name) : null,
    text: msg.body || '',
    ts: msg.timestamp || Math.floor(Date.now() / 1000),
  });
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
});

const app = express();
app.use(express.json({ limit: '256kb' }));

app.get('/health', (_req, res) => res.send('ok'));

app.get('/events', (req, res) => {
  const since = Number(req.query.since || 0);
  res.json({ events: events.filter((e) => e.ts > since) });
});

app.post('/send', async (req, res) => {
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).json({ ok: false, error: 'to+text required' });
  const chatId = String(to).replace(/^\+/, '').replace(/\D/g, '') + '@c.us';
  try {
    await client.sendMessage(chatId, String(text));
    res.json({ ok: true });
  } catch (err) {
    res.status(502).json({ ok: false, error: String(err && err.message || err) });
  }
});

app.listen(PORT, '127.0.0.1', () =>
  console.log(`whatsapp sidecar listening on 127.0.0.1:${PORT}`));

client.initialize();
