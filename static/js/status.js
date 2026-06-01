/**
 * status.js — 即時狀態頁面邏輯
 * 優先使用 WebSocket (Socket.IO)，若連線失敗則每 2 秒輪詢 REST API。
 */

const POLL_INTERVAL = 2000;
let usePolling = false;

// ── DOM 元素 ─────────────────────────────────────────────
const scoreEl  = document.getElementById('score');
const levelEl  = document.getElementById('level');
const livesEl  = document.getElementById('lives');
const badgeEl  = document.getElementById('status-badge');
const updateEl = document.getElementById('last-update');
const canvas   = document.getElementById('gameCanvas');
const ctx      = canvas.getContext('2d');

// ── Canvas 尺寸（對應後端邏輯座標 160×120，放大 2×）────
const SCALE  = 2;
const W = 160 * SCALE;
const H = 120 * SCALE;
canvas.width  = W;
canvas.height = H;

// ── 狀態標籤映射 ─────────────────────────────────────────
const STATUS_LABEL = {
  waiting:  '等待中',
  playing:  '遊戲中',
  paused:   '暫停中',
  gameover: '遊戲結束',
};

// ── 更新 UI ──────────────────────────────────────────────
function updateUI(data) {
  scoreEl.textContent = data.score.toLocaleString();
  levelEl.textContent = 'Lv.' + data.level;
  livesEl.textContent = '❤'.repeat(data.lives) || '💀';

  const label = STATUS_LABEL[data.status] || data.status;
  badgeEl.textContent = label;
  badgeEl.className   = 'value badge ' + data.status;

  updateEl.textContent = new Date().toLocaleTimeString();

  drawCanvas(data);
}

// ── Canvas 繪製（字元風格）───────────────────────────────
function drawCanvas(data) {
  ctx.clearRect(0, 0, W, H);

  // 背景
  ctx.fillStyle = '#000010';
  ctx.fillRect(0, 0, W, H);

  // 星星背景（固定種子，每幀重繪）
  ctx.fillStyle = 'rgba(255,255,255,0.5)';
  for (let i = 0; i < 40; i++) {
    const sx = ((i * 73 + 17) % 160) * SCALE;
    const sy = ((i * 137 + 31) % 120) * SCALE;
    ctx.fillRect(sx, sy, 1, 1);
  }

  if (!data.player) return;

  // 敵機
  ctx.font      = `${14 * SCALE / 2}px monospace`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  (data.enemies || []).forEach(e => {
    ctx.fillStyle = '#ff4444';
    ctx.fillText('✈', e.x * SCALE, e.y * SCALE);
  });

  // 子彈
  (data.bullets || []).forEach(b => {
    ctx.fillStyle = b.owner === 'player' ? '#00e5ff' : '#ff8800';
    ctx.fillRect(b.x * SCALE - 1, b.y * SCALE - 4, 2, 8);
  });

  // 玩家
  ctx.fillStyle = '#00ff88';
  ctx.fillText('🚀', data.player.x * SCALE, data.player.y * SCALE);

  // HUD
  ctx.fillStyle = 'rgba(0,229,255,0.8)';
  ctx.font = '10px monospace';
  ctx.textAlign = 'left';
  ctx.fillText(`SCORE: ${data.score}`, 4, 12);
  ctx.fillText(`LV: ${data.level}`, 4, 24);
}

// ── WebSocket 連線 ───────────────────────────────────────
function connectSocket() {
  try {
    const socket = io('/game');
    socket.on('connect', () => { usePolling = false; });
    socket.on('game_state', updateUI);
    socket.on('connect_error', () => { if (!usePolling) startPolling(); });
  } catch (e) {
    startPolling();
  }
}

// ── REST 輪詢（fallback）────────────────────────────────
function startPolling() {
  usePolling = true;
  setInterval(async () => {
    try {
      const res  = await fetch('/api/status');
      const data = await res.json();
      updateUI(data);
    } catch (e) { /* ignore */ }
  }, POLL_INTERVAL);
}

connectSocket();
