#!/usr/bin/env node
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import process from 'node:process';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { WSClient, generateReqId } from '@wecom/aibot-node-sdk';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const configPath = process.env.STOCK_RUNTIME_CONFIG || path.join(root, 'config/runtime.local.json');
const runtimeDir = path.join(root, 'data/runtime');
const statePath = path.join(runtimeDir, 'wecom-aibot-state.json');
const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
const bot = config.wecom_aibot || {};
if (!bot.bot_id || !bot.secret) throw new Error('wecom_aibot bot_id/secret is not configured');

fs.mkdirSync(runtimeDir, { recursive: true });
let state = {};
try { state = JSON.parse(fs.readFileSync(statePath, 'utf8')); } catch {}
let connected = false;
const conversationQueues = new Map();
let activeMessages = 0;
let shuttingDown = false;
let forcedShutdownTimer = null;

function persistState() {
  const temp = `${statePath}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(state, null, 2) + '\n', { mode: 0o600 });
  fs.renameSync(temp, statePath);
}

function truncateUtf8(value, maxBytes = 19000) {
  let result = String(value || '');
  while (Buffer.byteLength(result, 'utf8') > maxBytes) result = result.slice(0, -256);
  return result;
}

function processMessage(payload) {
  return new Promise((resolve, reject) => {
    const python = spawn(
      path.join(root, '.venv/bin/python'),
      [path.join(root, 'scripts/process_wecom_aibot_message.py'), '--config', configPath],
      { cwd: root, env: process.env },
    );
    let stdout = '';
    let stderr = '';
    python.stdout.on('data', chunk => { stdout += chunk.toString(); });
    python.stderr.on('data', chunk => { stderr += chunk.toString(); });
    python.on('error', reject);
    python.on('close', code => {
      const line = stdout.split(/\r?\n/).findLast(item => item.startsWith('STOCK_AIBOT_RESULT='));
      if (code !== 0 || !line) {
        reject(new Error((stderr || stdout || `processor exited ${code}`).slice(-1500)));
        return;
      }
      try { resolve(JSON.parse(line.slice('STOCK_AIBOT_RESULT='.length))); }
      catch (error) { reject(error); }
    });
    python.stdin.end(JSON.stringify(payload));
  });
}

const wsClient = new WSClient({
  botId: bot.bot_id,
  secret: bot.secret,
  logger: {
    debug: () => {},
    info: message => console.log(`[AiBotSDK] ${message}`),
    warn: message => console.warn(`[AiBotSDK] ${message}`),
    error: message => console.error(`[AiBotSDK] ${message}`),
  },
});
wsClient.on('authenticated', () => {
  connected = true;
  console.log('[wecom-aibot] authenticated');
});
wsClient.on('disconnected', () => { connected = false; });
wsClient.on('error', error => console.error('[wecom-aibot] websocket error:', error?.message || error));

function addMixedContent(mixed, textParts, images, label) {
  for (const item of mixed?.msg_item || []) {
    if (item.msgtype === 'text' && item.text?.content) textParts.push(item.text.content);
    if (item.msgtype === 'image' && item.image?.url) images.push({ ...item.image, label });
  }
}

async function prepareMessage(body) {
  const textParts = [];
  const images = [];
  if (body.msgtype === 'text' && body.text?.content) textParts.push(body.text.content);
  else if (body.msgtype === 'mixed') addMixedContent(body.mixed, textParts, images, '本次消息图片');
  else if (body.msgtype === 'image' && body.image?.url) {
    textParts.push('[用户发送了一张图片]');
    images.push({ ...body.image, label: '本次消息图片' });
  } else if (body.msgtype === 'voice' && body.voice?.content) {
    textParts.push(`[语音转写] ${body.voice.content}`);
  } else if (body.msgtype === 'file') textParts.push('[用户发送了一个文件]');
  else if (body.msgtype === 'video') textParts.push('[用户发送了一段视频]');

  const quote = body.quote;
  if (quote) {
    textParts.push('\n[企业微信引用消息]');
    if (quote.msgtype === 'text' && quote.text?.content) textParts.push(quote.text.content);
    else if (quote.msgtype === 'mixed') addMixedContent(quote.mixed, textParts, images, '引用消息图片');
    else if (quote.msgtype === 'image' && quote.image?.url) {
      textParts.push('[引用了一张图片]');
      images.push({ ...quote.image, label: '引用消息图片' });
    } else if (quote.msgtype === 'voice' && quote.voice?.content) {
      textParts.push(`[引用语音转写] ${quote.voice.content}`);
    } else if (quote.msgtype === 'file') textParts.push('[引用了一个文件]');
  }

  const safeId = String(body.msgid || Date.now()).replace(/[^a-zA-Z0-9_-]/g, '').slice(-80);
  const mediaDir = images.length ? path.join(runtimeDir, 'wecom-aibot-media', safeId) : '';
  const imagePaths = [];
  if (mediaDir) fs.mkdirSync(mediaDir, { recursive: true, mode: 0o700 });
  for (let index = 0; index < images.length; index += 1) {
    const image = images[index];
    try {
      const downloaded = await wsClient.downloadFile(image.url, image.aeskey);
      const original = path.basename(downloaded.filename || `image-${index + 1}.jpg`)
        .replace(/[^a-zA-Z0-9._-]/g, '_');
      const filename = path.extname(original) ? original : `${original}.jpg`;
      const target = path.join(mediaDir, `${index + 1}-${filename}`);
      fs.writeFileSync(target, downloaded.buffer, { mode: 0o600 });
      imagePaths.push(target);
      textParts.push(`[${image.label}已附加给模型：${index + 1}]`);
    } catch (error) {
      textParts.push(`[${image.label}下载失败，无法查看]`);
      console.error('[wecom-aibot] image download failed:', error?.message || error);
    }
  }
  return {
    content: textParts.join('\n').trim() || '[消息没有可解析的文本内容]',
    imagePaths,
    mediaDir,
  };
}

async function handleInboundMessage(frame) {
  activeMessages += 1;
  const body = frame.body || {};
  const senderId = body.from?.userid || '';
  if (body.chattype === 'group' && body.chatid && !state.group_chat_id && bot.capture_first_group_chat !== false) {
    state.group_chat_id = body.chatid;
    state.captured_at = new Date().toISOString();
    state.captured_by = senderId;
    persistState();
    console.log('[wecom-aibot] captured target group chat');
  }
  const streamId = generateReqId('stock');
  let mediaDir = '';
  try {
    await wsClient.replyStream(frame, streamId, '收到，正在处理…', false);
    const prepared = await prepareMessage(body);
    mediaDir = prepared.mediaDir;
    let elapsed = 0;
    const progress = setInterval(() => {
      elapsed += 20;
      wsClient.replyStream(frame, streamId, `正在处理，已等待 ${elapsed} 秒…`, false)
        .catch(error => console.error('[wecom-aibot] progress reply failed:', error?.message || error));
    }, 20000);
    try {
      const chatType = body.chattype || 'single';
      const queueKey = chatType === 'group'
        ? `group:${body.chatid || ''}:${senderId}`
        : `single:${senderId}`;
      const previous = conversationQueues.get(queueKey) || Promise.resolve();
      const queued = previous.catch(() => {}).then(() => processMessage({
          message_id: body.msgid,
          sender_id: senderId,
          content: prepared.content,
          create_time: body.create_time,
          chat_id: body.chatid || '',
          chat_type: chatType,
          image_paths: prepared.imagePaths,
        }));
      conversationQueues.set(queueKey, queued);
      let result;
      try {
        result = await queued;
      } finally {
        if (conversationQueues.get(queueKey) === queued) conversationQueues.delete(queueKey);
      }
      clearInterval(progress);
      await wsClient.replyStream(frame, streamId, truncateUtf8(result.reply || '处理完成。'), true);
    } catch (error) {
      clearInterval(progress);
      throw error;
    }
  } catch (error) {
    console.error('[wecom-aibot] message failed:', error?.message || error);
    try { await wsClient.replyStream(frame, streamId, '消息处理失败，请稍后重试。', true); } catch {}
  } finally {
    if (mediaDir) {
      try { fs.rmSync(mediaDir, { recursive: true, force: true }); } catch {}
    }
    activeMessages -= 1;
    if (shuttingDown && activeMessages === 0) finishShutdown();
  }
}

for (const eventName of [
  'message.text', 'message.image', 'message.mixed',
  'message.voice', 'message.file', 'message.video',
]) {
  wsClient.on(eventName, handleInboundMessage);
}

const bridgeHost = bot.bridge_host || '127.0.0.1';
const bridgePort = Number(bot.bridge_port || 8898);
const bridge = http.createServer((request, response) => {
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  if (request.method === 'GET' && request.url === '/health') {
    response.end(JSON.stringify({
      ok: true,
      connected,
      group_chat_captured: Boolean(bot.group_chat_id || state.group_chat_id),
      active_messages: activeMessages,
      shutting_down: shuttingDown,
    }));
    return;
  }
  if (request.method !== 'POST' || request.url !== '/send') {
    response.statusCode = 404;
    response.end(JSON.stringify({ ok: false, error: 'not found' }));
    return;
  }
  let raw = '';
  request.on('data', chunk => {
    raw += chunk.toString();
    if (raw.length > 100000) request.destroy();
  });
  request.on('end', async () => {
    try {
      const payload = JSON.parse(raw);
      const target = bot.group_chat_id || state.group_chat_id;
      if (!connected) throw new Error('robot websocket is not connected');
      if (!target) throw new Error('target group chat has not been captured; @ the robot in the target group first');
      const proactiveContent = payload.mention_all
        ? `<@all>\n\n${String(payload.content || '')}`
        : payload.content;
      const message = payload.template_card
        ? { msgtype: 'template_card', template_card: payload.template_card }
        : { msgtype: 'markdown', markdown: { content: truncateUtf8(proactiveContent) } };
      await wsClient.sendMessage(target, message);
      response.end(JSON.stringify({ ok: true }));
    } catch (error) {
      response.statusCode = 503;
      response.end(JSON.stringify({ ok: false, error: error?.message || String(error) }));
    }
  });
});

bridge.listen(bridgePort, bridgeHost, () => {
  console.log(`[wecom-aibot] local bridge listening on ${bridgeHost}:${bridgePort}`);
});
wsClient.connect();

function finishShutdown() {
  if (forcedShutdownTimer) clearTimeout(forcedShutdownTimer);
  bridge.close();
  wsClient.disconnect();
  setTimeout(() => process.exit(0), 250).unref();
}
function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  bridge.close();
  if (activeMessages === 0) {
    finishShutdown();
    return;
  }
  console.log(`[wecom-aibot] waiting for ${activeMessages} active message(s) before shutdown`);
  forcedShutdownTimer = setTimeout(finishShutdown, 80000);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
