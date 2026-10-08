/**
 * core/api.js - REST helpers (SPEC 4.2). Every call sends `X-Requested-With: desktalk`, JSON bodies
 * carry `Content-Type: application/json`, errors are mapped to ApiError with the section 4.2 codes.
 *
 * Exported API (docs/ui-core-api.md section 5):
 *   ApiError, api.request, api.getInfo, api.getMe, api.login, api.register, api.logout,
 *   api.changePassword, api.listSessions, api.revokeSession, api.onUnauthorized, api.reportUnauthorized,
 *   createSearcher (the msg.search client of SPEC 9.6), serialSearch
 */

import { Emitter, errorText } from './util.js';
import { socket } from './socket.js'; // used lazily by createSearcher only (socket.js imports this module too)

const DEFAULT_TIMEOUT_MS = 15000;

/** Fallback error codes by HTTP status for responses that are not the standard JSON error. */
const STATUS_CODES = {
  400: 'bad_request', 401: 'unauthorized', 403: 'forbidden', 404: 'not_found', 405: 'method_not_allowed',
  408: 'request_timeout', 409: 'conflict', 411: 'length_required', 413: 'too_large', 415: 'unsupported_media_type',
  416: 'range_not_satisfiable', 421: 'host_not_allowed', 429: 'rate_limited', 431: 'header_fields_too_large', 500: 'server_error',
  501: 'not_implemented', 503: 'unavailable', 505: 'version_not_supported', 507: 'insufficient_storage',
};

/**
 * Error of a REST call. `status` 0 means the request never got an answer
 * (`code` 'network' or 'timeout').
 */
export class ApiError extends Error {
  /**
   * @param {number} status HTTP status (0 = no response)
   * @param {string} code machine-readable code (SPEC 4.2)
   * @param {string} message human text
   * @param {{retryAfter?: number|null, reason?: string|null}} [extra]
   */
  constructor(status, code, message, extra = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.retryAfter = extra.retryAfter ?? null;
    this.reason = extra.reason ?? null;
  }
}

const events = new Emitter();

/**
 * Tell the app that the session is gone (an `unauthorized` answer). main.js listens through
 * `api.onUnauthorized`; the upload module calls this for XHR 401s.
 * @param {ApiError} err
 */
function reportUnauthorized(err) {
  events.emit('unauthorized', err);
}

/**
 * Parse a Retry-After header (seconds form only).
 * @param {Response} res
 * @returns {number|null}
 */
function headerRetryAfter(res) {
  const v = res.headers.get('Retry-After');
  const n = v === null ? NaN : Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Perform a REST call.
 * @param {string} method
 * @param {string} path absolute path such as "/api/login"
 * @param {object|null} [body] JSON body (POSTs always send one, `{}` when omitted)
 * @param {{timeout?: number, signal?: AbortSignal, silent401?: boolean}} [opts]
 *        silent401: do not notify `onUnauthorized` listeners (used by probes)
 * @returns {Promise<any>} parsed JSON, or null for 204
 */
async function request(method, path, body, opts = {}) {
  const { timeout = DEFAULT_TIMEOUT_MS, signal, silent401 = false } = opts;
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    ctl.abort();
  }, timeout);
  const onAbort = () => ctl.abort();
  if (signal) {
    if (signal.aborted) ctl.abort();
    else signal.addEventListener('abort', onAbort, { once: true });
  }
  /** @type {Record<string, string>} */
  const headers = { 'X-Requested-With': 'desktalk', Accept: 'application/json' };
  /** @type {RequestInit} */
  const init = { method, headers, credentials: 'same-origin', cache: 'no-store', signal: ctl.signal };
  if (method !== 'GET' && method !== 'HEAD') {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body === undefined || body === null ? {} : body);
  }
  try {
    let res;
    try {
      res = await fetch(path, init);
    } catch (_) {
      throw new ApiError(0, timedOut ? 'timeout' : 'network', timedOut ? 'The server did not answer in time.' : "Can't reach the server.");
    }
    if (res.status === 204) return null;
    let text = '';
    try {
      text = await res.text();
    } catch (_) {
      throw new ApiError(0, timedOut ? 'timeout' : 'network', "Can't reach the server.");
    }
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = null;
      }
    }
    if (res.ok) return data === null ? {} : data;
    const e = data && typeof data === 'object' && data.error && typeof data.error === 'object' ? data.error : null;
    const code = (e && typeof e.code === 'string' && e.code) || /** @type {any} */ (STATUS_CODES)[res.status] || 'server_error';
    const message = (e && typeof e.msg === 'string' && e.msg) || `Request failed (${res.status})`;
    const retryAfter = e && typeof e.retry_after === 'number' ? e.retry_after : headerRetryAfter(res);
    const err = new ApiError(res.status, code, message, { retryAfter, reason: e && typeof e.reason === 'string' ? e.reason : null });
    if (code === 'unauthorized' && !silent401) reportUnauthorized(err);
    throw err;
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onAbort);
  }
}

export const api = {
  request,
  reportUnauthorized,

  /**
   * Public server info (SPEC 4.2).
   * @returns {Promise<{name: string, registration_open: boolean, needs_setup: boolean, tls: boolean}>}
   */
  getInfo() {
    return request('GET', '/api/info');
  },

  /**
   * The signed-in user. 401 rejects with code 'unauthorized'.
   * @param {{timeout?: number, silent401?: boolean}} [opts]
   * @returns {Promise<{me: any}>}
   */
  getMe(opts) {
    return request('GET', '/api/me', null, opts);
  },

  /**
   * @param {string} username
   * @param {string} password
   * @returns {Promise<{me: any}>}
   */
  login(username, password) {
    return request('POST', '/api/login', { username, password });
  },

  /**
   * @param {{username: string, display_name: string, password: string, setup_code?: string, join_code?: string}} fields
   * @returns {Promise<{me: any}>}
   */
  register(fields) {
    return request('POST', '/api/register', fields);
  },

  /**
   * Sign out; never throws (the cookie is cleared server-side even when this fails).
   * @returns {Promise<null>}
   */
  async logout() {
    try {
      await request('POST', '/api/logout', {}, { silent401: true, timeout: 5000 });
    } catch (_) {
      /* the session may already be gone */
    }
    return null;
  },

  /**
   * @param {string} oldPassword
   * @param {string} newPassword
   * @returns {Promise<null>}
   */
  changePassword(oldPassword, newPassword) {
    return request('POST', '/api/password', { old_password: oldPassword, new_password: newPassword });
  },

  /**
   * @returns {Promise<{sessions: Array<{id: string, created_at: number, last_used_at: number, ip: string, user_agent: string, current: boolean}>}>}
   */
  listSessions() {
    return request('GET', '/api/sessions');
  },

  /**
   * @param {{id: string}|{all_others: true}} target
   * @returns {Promise<null>}
   */
  revokeSession(target) {
    return request('POST', '/api/sessions/revoke', target);
  },

  /**
   * Listen for expired/revoked sessions detected by any REST call.
   * @param {(err: ApiError) => void} fn
   * @returns {() => void} remover
   */
  onUnauthorized(fn) {
    return events.on('unauthorized', fn);
  },
};

/* ------------------------------------------------------------------------------------------ */
/* Search client (SPEC 9.6 "Search client rules")                                             */
/* ------------------------------------------------------------------------------------------ */

/** @type {Promise<any>} */
let searchLane = Promise.resolve();

/**
 * Run `fn` after every earlier lane call finished: the server allows ONE `msg.search`/`msg.shared`
 * in flight per user (a concurrent one is answered `rate_limited`, SPEC 2.3). Searchers use it;
 * views that call `msg.shared` directly should wrap the request the same way.
 * @template T
 * @param {() => Promise<T>} fn
 * @returns {Promise<T>}
 */
export function serialSearch(fn) {
  const run = searchLane.then(fn, fn);
  searchLane = run.then(() => undefined, () => undefined);
  return run;
}

const SEARCH_DEBOUNCE_MS = 400;
const SEARCH_MIN_CHARS = 2;
const SEARCH_TOTAL_CAP = 1000;

/**
 * The one implementation of the msg.search client rules: 400 ms debounce, queries shorter than 2
 * characters are never sent, sequence numbers make stale responses harmless, `rate_limited` waits
 * `retry_after` silently (the previous results stay), and at most ONE msg.search is in flight (a
 * newer query waits for the response of the running one).
 *
 * `state` is `{ q, results:[{message, chat_id}], hasMore, total, totalCapped, loading, error }`;
 * `total` exists only for chat-scoped searches (first page), `totalCapped` is true at 1000 ("1000+").
 * Events: `on('update', state)`.
 */
export class Searcher extends Emitter {
  /**
   * @param {{chat_id?: number|null, limit?: number, debounceMs?: number, socket?: any}} [opts]
   */
  constructor(opts = {}) {
    super();
    this._chatId = opts.chat_id || null;
    this._limit = opts.limit || (this._chatId ? 50 : 30);
    this._debounceMs = opts.debounceMs === undefined ? SEARCH_DEBOUNCE_MS : opts.debounceMs;
    this._socket = opts.socket || socket;
    this._seq = 0;
    this._timer = null;
    this._wait = null;
    this._inflight = false;
    /** @type {null|{seq: number, kind: 'first'|'more'}} */
    this._pending = null;
    this._destroyed = false;
    this.state = this._emptyState('');
  }

  /**
   * @param {string} q
   * @returns {{q: string, results: any[], hasMore: boolean, total: number|null, totalCapped: boolean, loading: boolean, error: any}}
   */
  _emptyState(q) {
    return { q, results: [], hasMore: false, total: null, totalCapped: false, loading: false, error: null };
  }

  _emit() {
    if (!this._destroyed) this.emit('update', this.state);
  }

  _clearTimers() {
    if (this._timer) clearTimeout(this._timer);
    if (this._wait) clearTimeout(this._wait);
    this._timer = null;
    this._wait = null;
  }

  /**
   * Set the query text (call on every keystroke). Short queries clear the results at once.
   * @param {string} q
   */
  setQuery(q) {
    if (this._destroyed) return;
    const text = String(q == null ? '' : q);
    this._seq += 1;
    this._pending = null;
    this._clearTimers();
    if (text.trim().length < SEARCH_MIN_CHARS) {
      this.state = this._emptyState(text);
      this._emit();
      return;
    }
    this.state = { ...this.state, q: text, error: null };
    this._timer = setTimeout(() => {
      this._timer = null;
      this._start(this._seq, 'first');
    }, this._debounceMs);
    this._emit();
  }

  /** Load the next page (`before_id` = the oldest result's message id). */
  loadMore() {
    const st = this.state;
    if (this._destroyed || !st.hasMore || st.loading || st.results.length === 0) return;
    this._start(this._seq, 'more');
  }

  /** Stop everything; no further events. */
  destroy() {
    this._destroyed = true;
    this._seq += 1;
    this._clearTimers();
    this._pending = null;
  }

  /**
   * @param {number} seq
   * @param {'first'|'more'} kind
   */
  _start(seq, kind) {
    if (this._destroyed || seq !== this._seq) return;
    if (this._inflight) {
      this._pending = { seq, kind };
      this.state = { ...this.state, loading: true };
      this._emit();
      return;
    }
    this._send(seq, kind);
  }

  /**
   * @param {number} seq
   * @param {'first'|'more'} kind
   */
  _send(seq, kind) {
    this._inflight = true;
    this.state = { ...this.state, loading: true, error: null };
    this._emit();
    /** @type {Record<string, any>} */
    const d = { q: this.state.q.trim(), limit: this._limit };
    if (this._chatId) d.chat_id = this._chatId;
    if (kind === 'more') {
      const last = this.state.results[this.state.results.length - 1];
      if (last) d.before_id = last.message.id;
    }
    serialSearch(() => this._socket.request('msg.search', d)).then(
      (res) => {
        this._inflight = false;
        if (seq === this._seq && !this._destroyed) this._apply(res, kind);
        this._next();
      },
      (err) => {
        this._inflight = false;
        if (seq !== this._seq || this._destroyed) {
          this._next();
          return;
        }
        if (err && (err.code === 'rate_limited' || err.code === 'server_busy')) {
          // wait silently, keep the previous results, then repeat the same request
          const ms = Math.max(0.5, Number(err.retry_after) || 1) * 1000;
          this._wait = setTimeout(() => {
            this._wait = null;
            this._start(seq, kind);
          }, ms);
          return;
        }
        this.state = { ...this.state, loading: false, error: { code: err && err.code, message: errorText(err, 'Search failed.') } };
        this._emit();
        this._next();
      },
    );
  }

  /** Run the newest follow-up request that was waiting for the response. */
  _next() {
    const p = this._pending;
    this._pending = null;
    if (p && p.seq === this._seq) this._send(p.seq, p.kind);
  }

  /**
   * @param {any} res msg.search response
   * @param {'first'|'more'} kind
   */
  _apply(res, kind) {
    const incoming = Array.isArray(res && res.results) ? res.results : [];
    let results;
    if (kind === 'more') {
      const seen = new Set(this.state.results.map((r) => r.message.id));
      results = this.state.results.concat(incoming.filter((r) => !seen.has(r.message.id)));
    } else {
      results = incoming;
    }
    const total = kind === 'first' && typeof res.total === 'number' ? res.total : this.state.total;
    this.state = {
      ...this.state,
      results,
      hasMore: Boolean(res && res.has_more),
      total,
      totalCapped: typeof total === 'number' && total >= SEARCH_TOTAL_CAP,
      loading: false,
      error: null,
    };
    this._emit();
  }
}

/**
 * Create a search client. Chat-scoped (`chat_id`) searches report `total` and page by 50, global
 * searches page by 30.
 * @param {{chat_id?: number|null, limit?: number}} [opts]
 * @returns {Searcher}
 */
export function createSearcher(opts = {}) {
  return new Searcher(opts);
}
