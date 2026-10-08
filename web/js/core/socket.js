/**
 * core/socket.js - the WebSocket client (SPEC 6, 7.1, 8.5).
 *
 * Responsibilities: request/response correlation (ids, 15 s timeout, <= 60 in flight), strict
 * in-order dispatch of server events, `ev.ready` handling (state becomes 'ready' only after the
 * store consumed it), the section 8.5 close-code table with full-jitter back-off and a cap of 12
 * attempts per minute, the /api/me probe when a connection fails before `ev.ready`, half-open
 * detection (visibility / online / pageshow / clock jumps) and a 20 s ping.
 *
 * Exported API (docs/ui-core-api.md section 6):
 *   socket (singleton SocketClient): start, stop, reconnect, request, send, on, probe, nudge,
 *     state, isReady, latency, info
 *   SocketError, isTransientError, PROTOCOL_VERSION, SocketClient (for tests),
 *   backoffSeconds, closePolicy
 *
 * States: idle | connecting | open | ready | waiting | unauthorized | password_change | kicked |
 *         toomany | outdated | stopped
 * Events: 'state' (state, prev), 'open', 'close' (code), 'ev.<name>' (d)
 */

import { Emitter, randomBetween } from './util.js';
import { api } from './api.js';

export const PROTOCOL_VERSION = 1;

const REQUEST_TIMEOUT_MS = 15000;
const MAX_IN_FLIGHT = 60;
const PING_INTERVAL_MS = 20000;
const PROBE_TIMEOUT_MS = 2500;
const MAX_ATTEMPTS_PER_MIN = 12;
const CLOCK_JUMP_MS = 30000;
const NUDGE_MIN_GAP_MS = 3000;
const MAX_HELD_EVENTS = 400;
const READY_TIMEOUT_MS = 30000;
const MAX_FRAME_CHARS = 250000;
const WS_OPEN = 1;

const TRANSIENT_CODES = new Set(['offline', 'connection_lost', 'timeout', 'rate_limited', 'server_busy', 'server_error']);

/**
 * Error of a WebSocket request. Local codes: 'offline', 'connection_lost', 'timeout',
 * 'cancelled'; every other code comes from the server (SPEC 7.1.1).
 */
export class SocketError extends Error {
  /**
   * @param {string} code
   * @param {string} [message]
   * @param {{reason?: string, retry_after?: number, chat_id?: number, message_id?: number}} [extra]
   */
  constructor(code, message, extra = {}) {
    super(message || code);
    this.name = 'SocketError';
    this.code = code;
    if (extra.reason !== undefined) this.reason = extra.reason;
    if (extra.retry_after !== undefined) this.retry_after = extra.retry_after;
    if (extra.chat_id !== undefined) this.chat_id = extra.chat_id;
    if (extra.message_id !== undefined) this.message_id = extra.message_id;
  }
}

/**
 * Outbox taxonomy of SPEC 7.1.1: errors after which a mutation may simply be retried.
 * @param {any} err
 * @returns {boolean}
 */
export function isTransientError(err) {
  return Boolean(err && TRANSIENT_CODES.has(err.code));
}

/**
 * Minimum wait before reconnecting after a close code (SPEC 8.5 table).
 * @param {number} code WebSocket close code
 * @returns {{action: 'retry'|'stop-kicked'|'stop-toomany', minWaitS: number}}
 */
export function closePolicy(code) {
  if (code === 4001) return { action: 'stop-kicked', minWaitS: 0 };
  if (code === 4003) return { action: 'stop-toomany', minWaitS: 0 };
  if (code === 4008) return { action: 'retry', minWaitS: 30 };
  if (code === 1013) return { action: 'retry', minWaitS: 5 };
  if (code === 1002 || code === 1003 || code === 1007 || code === 1008 || code === 1009) return { action: 'retry', minWaitS: 10 };
  return { action: 'retry', minWaitS: 0 };
}

/**
 * Full-jitter back-off: random(0, min(10, 0.5 * 2^n)) seconds, n = consecutive failures.
 * @param {number} failures
 * @param {() => number} [rand=Math.random]
 * @returns {number}
 */
export function backoffSeconds(failures, rand = Math.random) {
  return rand() * Math.min(10, 0.5 * 2 ** Math.max(0, failures));
}

/**
 * The WebSocket client. A single instance (`socket`) is used by the app; tests build their own
 * with injected dependencies.
 */
export class SocketClient extends Emitter {
  /**
   * @param {{WebSocketImpl?: any, url?: () => string, getMe?: (o: object) => Promise<any>,
   *          now?: () => number, random?: () => number, readyTimeoutMs?: number}} [deps]
   */
  constructor(deps = {}) {
    super();
    this._deps = {
      WebSocketImpl: deps.WebSocketImpl || null,
      url: deps.url || defaultUrl,
      getMe: deps.getMe || ((o) => api.getMe(o)),
      now: deps.now || (() => Date.now()),
      random: deps.random || Math.random,
      readyTimeoutMs: deps.readyTimeoutMs || READY_TIMEOUT_MS,
    };
    /** @type {string} */
    this.state = 'idle';
    /** @type {number|null} */
    this.latency = null;
    this.info = { kickReason: /** @type {string|null} */ (null), nextRetryAt: /** @type {number|null} */ (null), attempt: 0, error: /** @type {string|null} */ (null) };

    /** @type {any} */
    this._ws = null;
    this._started = false;
    this._stopped = false;
    this._opened = false;
    this._gotReady = false;
    this._failures = 0;
    this._token = 0;
    this._seq = 0;
    this._timer = /** @type {any} */ (null);
    this._readyTimer = /** @type {any} */ (null);
    this._ticker = /** @type {any} */ (null);
    this._lastAttempt = 0;
    this._hardWaitUntil = 0;
    this._lastTick = 0;
    this._lastPing = 0;
    this._pingInFlight = false;
    this._probing = false;
    /** @type {number[]} */
    this._attempts = [];
    /** @type {Map<string, any>} */
    this._pending = new Map();
    /** @type {any[]} */
    this._queue = [];
    /** @type {any[]} */
    this._held = [];
    this._globalsInstalled = false;
  }

  /** @returns {boolean} true once ev.ready was processed on the current socket */
  get isReady() {
    return this.state === 'ready';
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Public control                                                                           */
  /* ---------------------------------------------------------------------------------------- */

  /** Connect and keep connected (idempotent). */
  start() {
    if (this._started && !this._stopped) return;
    this._started = true;
    this._stopped = false;
    this._failures = 0;
    this._hardWaitUntil = 0;
    this._attempts = [];
    this.info = { kickReason: null, nextRetryAt: null, attempt: 0, error: null };
    this._installGlobals();
    this._startTicker();
    this._connect();
  }

  /** Close permanently (logout, kicked): no reconnect until start() is called again. */
  stop() {
    this._stopped = true;
    this._started = false;
    this._token += 1;
    this._clearTimer();
    this._clearReadyTimer();
    this._stopTicker();
    const ws = this._ws;
    this._ws = null;
    if (ws) {
      detach(ws);
      try {
        ws.close(1000);
      } catch (_) {
        /* ignore */
      }
    }
    this._failAll(new SocketError('connection_lost', 'Connection closed'));
    this._opened = false;
    this._gotReady = false;
    this._held = [];
    this._setState('stopped');
  }

  /** Reset the back-off and connect right now (4003 "Use this window", manual retry). */
  reconnect() {
    this._stopped = false;
    this._started = true;
    this._failures = 0;
    this._hardWaitUntil = 0;
    this._attempts = [];
    this._token += 1;
    this._clearTimer();
    this._clearReadyTimer();
    this._installGlobals();
    this._startTicker();
    const ws = this._ws;
    if (ws) {
      this._ws = null;
      detach(ws);
      try {
        ws.close();
      } catch (_) {
        /* ignore */
      }
      this._failAll(new SocketError('connection_lost', 'Reconnecting'));
    }
    this._opened = false;
    this._gotReady = false;
    this._connect();
  }

  /**
   * Send a request and wait for its `res`.
   * @param {string} type one of the SPEC 7.4 request names
   * @param {object} [d]
   * @param {{timeout?: number}} [opts]
   * @returns {Promise<any>}
   */
  request(type, d = {}, opts = {}) {
    if (!this._canSend()) return Promise.reject(new SocketError('offline', 'Not connected'));
    return new Promise((resolve, reject) => {
      const job = { type, d, timeout: opts.timeout || REQUEST_TIMEOUT_MS, resolve, reject, id: '', timer: null, ws: this._ws };
      if (this._pending.size >= MAX_IN_FLIGHT) this._queue.push(job);
      else this._transmit(job);
    });
  }

  /**
   * Fire-and-forget request without an id (no `res`), e.g. `typing`.
   * @param {string} type
   * @param {object} [d]
   * @returns {boolean} false when the socket is not open
   */
  send(type, d = {}) {
    if (!this._canSend()) return false;
    try {
      this._ws.send(JSON.stringify({ t: type, d }));
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * Liveness check: ping and expect the answer within 2.5 s, else drop the socket and reconnect
   * immediately (half-open sockets after phone lock / laptop sleep, SPEC 8.5). Runs only while
   * the connection is live (ev.ready received); before that the 30 s ready timeout applies.
   * @param {string} [reason]
   */
  probe(reason = 'probe') {
    if (this._probing || this.state !== 'ready' || !this._canSend()) return;
    const ws = this._ws;
    this._probing = true;
    this.request('ping', {}, { timeout: PROBE_TIMEOUT_MS })
      .catch((err) => {
        if (err && err.code === 'timeout' && this._ws === ws) this._forceClose(reason);
      })
      .finally(() => {
        this._probing = false;
      });
  }

  /**
   * Reconnect immediately when waiting for a back-off and the last attempt was > 3 s ago
   * (`online`, `visibilitychange`, SPEC 8.5).
   */
  nudge() {
    if (!this._started || this._stopped || this.state !== 'waiting' || this._timer === null) return;
    const now = this._deps.now();
    if (now - this._lastAttempt <= NUDGE_MIN_GAP_MS || now < this._hardWaitUntil) return;
    this._clearTimer();
    this._connect();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Connecting                                                                               */
  /* ---------------------------------------------------------------------------------------- */

  /** Open a new WebSocket, honouring the 12-attempts-per-minute cap. */
  _connect() {
    if (this._stopped) return;
    this._clearTimer();
    const now = this._deps.now();
    this._attempts = this._attempts.filter((t) => now - t < 60000);
    if (this._attempts.length >= MAX_ATTEMPTS_PER_MIN) {
      const wait = Math.max(500, this._attempts[0] + 60000 - now);
      this._scheduleIn(wait);
      return;
    }
    this._attempts.push(now);
    this._lastAttempt = now;
    this.info.attempt += 1;
    this.info.nextRetryAt = null;
    this._setState('connecting');
    const Impl = this._deps.WebSocketImpl || globalThis.WebSocket;
    let ws;
    try {
      ws = new Impl(this._deps.url());
    } catch (err) {
      this.info.error = err && err.message ? String(err.message) : 'WebSocket construction failed';
      this._ws = null;
      this._failures += 1;
      this._scheduleIn(Math.max(1, backoffSeconds(this._failures, this._deps.random)) * 1000);
      return;
    }
    this._ws = ws;
    this._opened = false;
    this._gotReady = false;
    this._held = [];
    // Connecting or pending (no ev.ready yet) for 30 s: close and retry with the normal back-off.
    this._clearReadyTimer();
    this._readyTimer = setTimeout(() => {
      this._readyTimer = null;
      if (this._ws === ws && !this._gotReady) this._forceClose('ready-timeout', false);
    }, this._deps.readyTimeoutMs);
    ws.onopen = () => {
      if (ws !== this._ws) return;
      this._opened = true;
      this._lastPing = this._deps.now();
      this._setState('open');
      this.emit('open');
    };
    ws.onmessage = (e) => {
      if (ws === this._ws) this._onFrame(e.data);
    };
    ws.onerror = () => {
      /* the close event follows; nothing useful is exposed here */
    };
    ws.onclose = (e) => this._onClose(ws, e && typeof e.code === 'number' ? e.code : 1006, false);
  }

  /**
   * Drop a socket we consider dead and treat it as closed (1006).
   * @param {string} reason
   * @param {boolean} [immediate=true] retry without delay (half-open recovery); false = normal back-off
   */
  _forceClose(reason, immediate = true) {
    const ws = this._ws;
    if (!ws) return;
    detach(ws);
    try {
      ws.close();
    } catch (_) {
      /* ignore */
    }
    this.info.error = reason;
    this._onClose(ws, 1006, immediate);
  }

  /**
   * Handle the end of a socket.
   * @param {any} ws
   * @param {number} code
   * @param {boolean} immediate retry without delay (half-open recovery)
   */
  _onClose(ws, code, immediate) {
    if (ws !== this._ws) return;
    this._ws = null;
    this._clearReadyTimer();
    const gotReady = this._gotReady;
    const opened = this._opened;
    this._opened = false;
    this._gotReady = false;
    this._held = [];
    this._pingInFlight = false;
    this._probing = false;
    this._failAll(new SocketError('connection_lost', 'The connection was lost'));
    this.emit('close', code);
    if (this._stopped) return;
    const policy = closePolicy(code);
    if (policy.action === 'stop-kicked') {
      this._started = false;
      this._clearTimer();
      this._setState('kicked');
      return;
    }
    if (policy.action === 'stop-toomany') {
      this._started = false;
      this._clearTimer();
      this._setState('toomany');
      return;
    }
    this._failures = gotReady ? 0 : this._failures + 1;
    let delayS;
    if (immediate) delayS = 0;
    else if (gotReady && (code === 1001 || code === 1006)) delayS = randomBetween(0, 5);
    else delayS = backoffSeconds(this._failures, this._deps.random);
    delayS += policy.minWaitS;
    this._hardWaitUntil = policy.minWaitS > 0 ? this._deps.now() + policy.minWaitS * 1000 : 0;
    this._setState('waiting');
    if (!gotReady && !immediate) this._afterEarlyFailure(delayS);
    else this._scheduleIn(delayS * 1000);
  }

  /**
   * Failure before ev.ready: a rejected handshake (401/403/429) is invisible to JS, so ask
   * GET /api/me what is wrong (SPEC 8.5).
   * @param {number} delayS
   */
  async _afterEarlyFailure(delayS) {
    const token = ++this._token;
    const startedAt = this._deps.now();
    let verdict = 'retry';
    try {
      const r = await this._deps.getMe({ timeout: 3000, silent401: true });
      if (r && r.me && r.me.must_change_password) verdict = 'password_change';
    } catch (err) {
      const e = /** @type {any} */ (err);
      if (e && (e.code === 'unauthorized' || e.status === 401)) verdict = 'unauthorized';
      else if (e && e.code === 'password_change_required') verdict = 'password_change';
    }
    if (token !== this._token || this._stopped) return;
    if (verdict === 'unauthorized') {
      this._started = false;
      this._setState('unauthorized');
      return;
    }
    if (verdict === 'password_change') {
      this._started = false;
      this._setState('password_change');
      return;
    }
    const remaining = Math.max(0, delayS * 1000 - (this._deps.now() - startedAt));
    this._scheduleIn(remaining);
  }

  /** @param {number} ms */
  _scheduleIn(ms) {
    this._clearTimer();
    this._setState('waiting');
    this.info.nextRetryAt = this._deps.now() + ms;
    this._timer = setTimeout(() => {
      this._timer = null;
      this._connect();
    }, ms);
  }

  _clearTimer() {
    if (this._timer !== null) clearTimeout(this._timer);
    this._timer = null;
  }

  _clearReadyTimer() {
    if (this._readyTimer !== null) clearTimeout(this._readyTimer);
    this._readyTimer = null;
  }

  /**
   * @param {string} state
   */
  _setState(state) {
    if (state === this.state) return;
    const prev = this.state;
    this.state = state;
    this.emit('state', state, prev);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Frames                                                                                   */
  /* ---------------------------------------------------------------------------------------- */

  /** @returns {boolean} */
  _canSend() {
    return Boolean(this._ws) && this._ws.readyState === WS_OPEN && (this.state === 'open' || this.state === 'ready');
  }

  /**
   * @param {any} data raw frame text
   */
  _onFrame(data) {
    if (typeof data !== 'string') return;
    let msg;
    try {
      msg = JSON.parse(data);
    } catch (_) {
      return;
    }
    if (!msg || typeof msg !== 'object' || typeof msg.t !== 'string') return;
    if (msg.t === 'res') {
      this._onRes(msg);
      return;
    }
    if (msg.t === 'ev.ready') {
      this._onReady(msg.d && typeof msg.d === 'object' ? msg.d : {});
      return;
    }
    if (msg.t.startsWith('ev.')) {
      if (!this._gotReady) {
        // The server never sends an event before ev.ready; hold defensively and keep order.
        if (this._held.length < MAX_HELD_EVENTS) this._held.push(msg);
        return;
      }
      this._dispatch(msg);
    }
  }

  /**
   * @param {any} msg a `res` frame
   */
  _onRes(msg) {
    const job = typeof msg.id === 'string' ? this._pending.get(msg.id) : undefined;
    if (!job) return;
    clearTimeout(job.timer);
    this._pending.delete(msg.id);
    this._pump();
    if (msg.ok) {
      job.resolve(msg.d && typeof msg.d === 'object' ? msg.d : {});
    } else {
      const e = msg.err && typeof msg.err === 'object' ? msg.err : {};
      job.reject(new SocketError(typeof e.code === 'string' ? e.code : 'server_error', typeof e.msg === 'string' ? e.msg : 'Request failed', {
        reason: typeof e.reason === 'string' ? e.reason : undefined,
        retry_after: typeof e.retry_after === 'number' ? e.retry_after : undefined,
        chat_id: typeof e.chat_id === 'number' ? e.chat_id : undefined,
        message_id: typeof e.message_id === 'number' ? e.message_id : undefined,
      }));
    }
  }

  /**
   * ev.ready: dispatch it first (the store replaces its state), then flip to 'ready', then flush
   * any events that were held.
   * @param {any} d
   */
  _onReady(d) {
    if (typeof d.protocol === 'number' && d.protocol > PROTOCOL_VERSION) {
      this._stopped = true;
      this._started = false;
      this._clearTimer();
      this._clearReadyTimer();
      const ws = this._ws;
      this._ws = null;
      if (ws) {
        detach(ws);
        try {
          ws.close(1000);
        } catch (_) {
          /* ignore */
        }
      }
      this._failAll(new SocketError('connection_lost', 'Protocol mismatch'));
      this._setState('outdated');
      return;
    }
    this._clearReadyTimer();
    this._gotReady = true;
    this._failures = 0;
    this.info.attempt = 0;
    this.info.error = null;
    this._lastPing = this._deps.now();
    this.emit('ev.ready', d);
    this._setState('ready');
    const held = this._held;
    this._held = [];
    for (const msg of held) this._dispatch(msg);
  }

  /**
   * @param {any} msg an event frame
   */
  _dispatch(msg) {
    const d = msg.d && typeof msg.d === 'object' ? msg.d : {};
    if (msg.t === 'ev.kicked') this.info.kickReason = typeof d.reason === 'string' ? d.reason : null;
    this.emit(msg.t, d);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Requests                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Put one request on the wire and arm its timeout.
   * @param {any} job
   */
  _transmit(job) {
    if (job.ws !== this._ws || !this._canSend()) {
      job.reject(new SocketError('connection_lost', 'The connection was lost'));
      return;
    }
    this._seq += 1;
    const id = 'r' + this._seq.toString(36);
    let frame;
    try {
      frame = JSON.stringify({ t: job.type, id, d: job.d });
    } catch (_) {
      job.reject(new SocketError('bad_request', 'The request could not be encoded'));
      return;
    }
    if (frame.length > MAX_FRAME_CHARS) {
      job.reject(new SocketError('too_large', 'The request is too large'));
      return;
    }
    try {
      this._ws.send(frame);
    } catch (_) {
      job.reject(new SocketError('connection_lost', 'The connection was lost'));
      return;
    }
    job.id = id;
    job.timer = setTimeout(() => {
      this._pending.delete(id);
      this._pump();
      job.reject(new SocketError('timeout', 'The server did not answer in time'));
    }, job.timeout);
    this._pending.set(id, job);
  }

  /** Send queued requests while there is room. */
  _pump() {
    while (this._queue.length > 0 && this._pending.size < MAX_IN_FLIGHT) {
      this._transmit(this._queue.shift());
    }
  }

  /**
   * Reject everything that is waiting for an answer.
   * @param {SocketError} err
   */
  _failAll(err) {
    const pending = Array.from(this._pending.values());
    this._pending.clear();
    const queued = this._queue;
    this._queue = [];
    for (const job of pending) {
      clearTimeout(job.timer);
      job.reject(err);
    }
    for (const job of queued) job.reject(err);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Liveness                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  _startTicker() {
    if (this._ticker !== null) return;
    this._lastTick = this._deps.now();
    this._ticker = setInterval(() => this._tick(), 1000);
  }

  _stopTicker() {
    if (this._ticker !== null) clearInterval(this._ticker);
    this._ticker = null;
  }

  /** 1 s tick: clock-jump detection and the 20 s ping (timestamp based, SPEC 8.5). */
  _tick() {
    const now = this._deps.now();
    const jump = now - this._lastTick;
    this._lastTick = now;
    if (!this._canSend()) return;
    if (jump > CLOCK_JUMP_MS) {
      this.probe('clock-jump');
      return;
    }
    if (this.state === 'ready' && !this._pingInFlight && now - this._lastPing >= PING_INTERVAL_MS) {
      this._lastPing = now;
      this._pingInFlight = true;
      const ws = this._ws;
      const t0 = now;
      this.request('ping', {})
        .then(() => {
          this.latency = Math.max(0, this._deps.now() - t0);
        })
        .catch((err) => {
          if (err && err.code === 'timeout' && this._ws === ws) this._forceClose('ping-timeout');
        })
        .finally(() => {
          this._pingInFlight = false;
        });
    }
  }

  /** Page-level wake-up hooks (installed once). */
  _installGlobals() {
    if (this._globalsInstalled || typeof window === 'undefined') return;
    this._globalsInstalled = true;
    const wake = () => {
      if (!this._started || this._stopped) return;
      this.nudge();
      this.probe('wake');
    };
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') wake();
    });
    window.addEventListener('online', wake);
    window.addEventListener('pageshow', wake);
  }
}

/**
 * Remove every handler of a socket we are abandoning.
 * @param {any} ws
 */
function detach(ws) {
  ws.onopen = null;
  ws.onmessage = null;
  ws.onerror = null;
  ws.onclose = null;
}

/**
 * ws:// or wss:// on the same host, chosen from the page protocol (SPEC 5.7).
 * @returns {string}
 */
function defaultUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws`;
}

export const socket = new SocketClient();
