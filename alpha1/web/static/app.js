/**
 * Alpha1 Live Trading Dashboard — WebSocket client + DOM update logic.
 *
 * Connects to ws://<host>/ws, receives JSON events from the server, and
 * updates the dashboard tables in real-time without page reloads.
 *
 * Event types received from server:
 *   snapshot      — initial state on connect: positions, orders, equity
 *   order_placed  — new bracket order submitted
 *   order_filled  — limit order executed; position opened
 *   order_cancelled / order_expired
 *   breakeven_set — stop moved to entry
 *   position_closed — SL / TP / session-end exit
 *   bar_update    — periodic heartbeat on each 5M bar
 */
'use strict';

// ---------------------------------------------------------------------------
// WebSocket connection + auto-reconnect
// ---------------------------------------------------------------------------

const WS_URL = `ws://${location.host}/ws`;
const RECONNECT_DELAY_MS = 3000;
let ws = null;

function connectWebSocket() {
  setConnectionStatus('connecting');
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setConnectionStatus('connected');
    // Keep-alive ping every 25 s (server receives_text() consumes it)
    setInterval(() => { if (ws.readyState === WebSocket.OPEN) ws.send('ping'); }, 25000);
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    handleEvent(msg);
  };

  ws.onclose = () => {
    setConnectionStatus('disconnected');
    setTimeout(connectWebSocket, RECONNECT_DELAY_MS);
  };

  ws.onerror = () => {
    setConnectionStatus('error');
    ws.close();
  };
}

function setConnectionStatus(state) {
  const dot   = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  if (!dot || !label) return;

  const states = {
    connecting:   { color: 'bg-yellow-500', text: 'connecting…' },
    connected:    { color: 'bg-green-500',  text: 'connected'   },
    disconnected: { color: 'bg-gray-600',   text: 'reconnecting…' },
    error:        { color: 'bg-red-500',    text: 'error'        },
  };
  const s = states[state] || states.connecting;
  dot.className = `dot-pulse ${s.color}`;
  label.textContent = s.text;
}

// ---------------------------------------------------------------------------
// Event handler
// ---------------------------------------------------------------------------

function handleEvent(msg) {
  const { type, data, time } = msg;

  switch (type) {
    case 'snapshot':
      renderPositions(data.positions || []);
      renderOrders(data.orders || []);
      updateEquity(data.equity);
      break;

    case 'order_placed':
    case 'order_cancelled':
    case 'order_expired':
    case 'order_filled':
    case 'breakeven_set':
    case 'position_closed':
    case 'bar_update':
      // Refresh from REST on any meaningful event
      refreshDashboard();
      break;
  }

  addEventFeedItem(type, data, time);
  updateEquityFromApi();
}

// ---------------------------------------------------------------------------
// Dashboard refresh (REST polling fallback + event-driven refresh)
// ---------------------------------------------------------------------------

async function refreshDashboard() {
  try {
    const [posRes, ordRes] = await Promise.all([
      fetch('/api/positions'),
      fetch('/api/orders'),
    ]);
    if (posRes.ok) renderPositions(await posRes.json());
    if (ordRes.ok) renderOrders(await ordRes.json());
  } catch (e) {
    console.warn('Dashboard refresh failed:', e);
  }
}

async function updateEquityFromApi() {
  try {
    const res = await fetch('/api/equity');
    if (res.ok) {
      const { equity } = await res.json();
      updateEquity(equity);
    }
  } catch { /* ignore */ }
}

function updateEquity(value) {
  const el = document.getElementById('equity-val');
  const nav = document.getElementById('nav-equity');
  if (!value && value !== 0) return;
  const formatted = '$' + Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (el) el.textContent = formatted;
  if (nav) nav.textContent = formatted;
}

// ---------------------------------------------------------------------------
// Positions table
// ---------------------------------------------------------------------------

function renderPositions(positions) {
  const tbody = document.getElementById('positions-tbody');
  if (!tbody) return;

  const countEl = document.getElementById('pos-count');
  if (countEl) countEl.textContent = positions.length;

  if (positions.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="px-3 py-4 text-center text-gray-600">No open positions</td></tr>';
    return;
  }

  tbody.innerHTML = positions.map(p => `
    <tr class="border-b border-gray-800/50">
      <td class="px-3 py-2 font-bold text-white">${esc(p.instrument)}</td>
      <td class="px-3 py-2">
        <span class="badge ${p.direction === 'LONG' ? 'badge-long' : 'badge-short'}">${esc(p.direction)}</span>
      </td>
      <td class="px-3 py-2 text-right">${fmt(p.entry_price)}</td>
      <td class="px-3 py-2 text-right text-red-400">${fmt(p.stop_price)}</td>
      <td class="px-3 py-2 text-right text-green-400">${fmt(p.target_price)}</td>
      <td class="px-3 py-2 text-right">${p.size}</td>
      <td class="px-3 py-2 text-right text-gray-400">${shortTs(p.entry_time)}</td>
    </tr>
  `).join('');
}

// ---------------------------------------------------------------------------
// Orders table
// ---------------------------------------------------------------------------

function renderOrders(orders) {
  const tbody = document.getElementById('orders-tbody');
  if (!tbody) return;

  const countEl = document.getElementById('order-count');
  if (countEl) countEl.textContent = orders.length;

  if (orders.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="px-3 py-4 text-center text-gray-600">No pending orders</td></tr>';
    return;
  }

  tbody.innerHTML = orders.map(o => `
    <tr class="border-b border-gray-800/50">
      <td class="px-3 py-2 font-bold text-white">${esc(o.instrument)}</td>
      <td class="px-3 py-2">
        <span class="badge ${o.direction === 'LONG' ? 'badge-long' : 'badge-short'}">${esc(o.direction)}</span>
      </td>
      <td class="px-3 py-2 text-right">${fmt(o.limit_price)}</td>
      <td class="px-3 py-2 text-right text-red-400">${fmt(o.stop_price)}</td>
      <td class="px-3 py-2 text-right text-green-400">${fmt(o.target_price)}</td>
      <td class="px-3 py-2 text-right text-amber-400">${fmt(o.cancel_price)}</td>
      <td class="px-3 py-2 text-gray-400">${esc(o.status)}</td>
      <td class="px-3 py-2 text-gray-400">${shortTs(o.placed_at)}</td>
    </tr>
  `).join('');
}

// ---------------------------------------------------------------------------
// Live event feed
// ---------------------------------------------------------------------------

const EVENT_COLORS = {
  order_placed:   'text-blue-400',
  order_filled:   'text-green-400',
  order_cancelled:'text-amber-400',
  order_expired:  'text-gray-500',
  breakeven_set:  'text-cyan-400',
  position_closed:'text-purple-400',
  bar_update:     'text-gray-600',
  snapshot:       'text-gray-500',
};

function addEventFeedItem(type, data, time) {
  const container = document.getElementById('live-events');
  if (!container) return;

  // Throttle bar_update noise
  if (type === 'bar_update') return;

  const color = EVENT_COLORS[type] || 'text-gray-400';
  const symbol = data.symbol || '';
  const reason = data.reason ? ` (${data.reason})` : '';
  const price  = data.fill_price || data.exit_price || data.entry || '';
  const priceStr = price ? ` @ ${fmt(price)}` : '';

  const div = document.createElement('div');
  div.className = `${color} leading-snug`;
  div.innerHTML = `<span class="text-gray-600">${shortTs(time)}</span> <span class="font-bold">${esc(type)}</span>${symbol ? ' ' + esc(symbol) : ''}${priceStr}${reason}`;

  // Prepend to show newest first
  container.insertBefore(div, container.firstChild);

  // Keep feed bounded to 100 items
  while (container.children.length > 100) {
    container.removeChild(container.lastChild);
  }

  // Show feed panel if hidden
  const feed = document.getElementById('event-feed');
  if (feed && feed.classList.contains('opacity-0')) {
    feed.classList.remove('opacity-0', 'pointer-events-none');
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmt(v) {
  if (v == null || v === '') return '—';
  const n = Number(v);
  if (isNaN(n)) return esc(v);
  // Use 5 decimals for values < 1000 (forex/metals), 2 for larger (futures)
  return n >= 1000 ? n.toFixed(2) : n.toFixed(5);
}

function shortTs(ts) {
  if (!ts) return '—';
  // Show HH:MM:SS from ISO timestamp
  const m = String(ts).match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : String(ts).slice(0, 16);
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  connectWebSocket();
  refreshDashboard();
  updateEquityFromApi();
});

// Expose for template script blocks
window.refreshDashboard = refreshDashboard;
