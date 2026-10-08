/**
 * core/util.js - small shared helpers: event emitter, ids, code-point maths, clock skew,
 * date/size formatting, environment detection, clipboard and the namespaced storage layer.
 *
 * Exported API (see docs/ui-core-api.md section 3):
 *   Emitter, newClientId, randomHex, uuid, cpLength, cpSlice, clamp, debounce, throttle, sleep,
 *   randomBetween, fold, pluralize, findMentions, runPaced, setSkew, getSkew, serverNow, formatTime, formatDayLabel,
 *   formatListTime, formatDateTime, formatLastSeen, formatDuration, formatBytes, formatCount,
 *   errorText, isLoopbackHost, isInsecureRemote, isTouchDevice, isIOS, prefersReducedMotion,
 *   supportsDesktopNotifications, supportsRecording, copyToClipboard, storage.
 */

/* ------------------------------------------------------------------------------------------ */
/* Emitter                                                                                    */
/* ------------------------------------------------------------------------------------------ */

/**
 * Minimal synchronous event emitter. A throwing listener never prevents the others from running.
 */
export class Emitter {
  constructor() {
    /** @type {Map<string, Set<Function>>} */
    this._listeners = new Map();
  }

  /**
   * Subscribe to an event.
   * @param {string} name
   * @param {Function} fn
   * @returns {() => void} remover
   */
  on(name, fn) {
    let set = this._listeners.get(name);
    if (!set) {
      set = new Set();
      this._listeners.set(name, set);
    }
    set.add(fn);
    return () => this.off(name, fn);
  }

  /**
   * Subscribe for a single emission.
   * @param {string} name
   * @param {Function} fn
   * @returns {() => void} remover
   */
  once(name, fn) {
    const off = this.on(name, (...args) => {
      off();
      fn(...args);
    });
    return off;
  }

  /**
   * Remove a listener.
   * @param {string} name
   * @param {Function} fn
   */
  off(name, fn) {
    const set = this._listeners.get(name);
    if (!set) return;
    set.delete(fn);
    if (set.size === 0) this._listeners.delete(name);
  }

  /**
   * Call every listener of `name` synchronously, in registration order.
   * @param {string} name
   * @param {...any} args
   */
  emit(name, ...args) {
    const set = this._listeners.get(name);
    if (!set || set.size === 0) return;
    for (const fn of Array.from(set)) {
      try {
        fn(...args);
      } catch (err) {
        console.error('[emitter] listener for "' + name + '" threw', err);
      }
    }
  }

  /**
   * @param {string} name
   * @returns {number}
   */
  listenerCount(name) {
    const set = this._listeners.get(name);
    return set ? set.size : 0;
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Ids and randomness                                                                         */
/* ------------------------------------------------------------------------------------------ */

/**
 * Cryptographically random bytes (falls back to Math.random only when no Web Crypto exists).
 * @param {number} n
 * @returns {Uint8Array}
 */
function randomBytes(n) {
  const out = new Uint8Array(n);
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(out);
  } else {
    for (let i = 0; i < n; i += 1) out[i] = Math.floor(Math.random() * 256);
  }
  return out;
}

/**
 * Random lowercase hex string of `bytes` bytes (2 chars per byte).
 * @param {number} [bytes=8]
 * @returns {string}
 */
export function randomHex(bytes = 8) {
  let s = '';
  for (const b of randomBytes(bytes)) s += (b < 16 ? '0' : '') + b.toString(16);
  return s;
}

/**
 * RFC 4122 v4 UUID; `crypto.randomUUID` is absent on insecure origins (SPEC section 0).
 * @returns {string}
 */
export function uuid() {
  const c = typeof globalThis !== 'undefined' ? globalThis.crypto : undefined;
  if (c && typeof c.randomUUID === 'function') {
    try {
      return c.randomUUID();
    } catch (_) {
      /* fall through to the manual version */
    }
  }
  const b = randomBytes(16);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => (x < 16 ? '0' : '') + x.toString(16)).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Idempotency key for msg.send / msg.forward: 8..64 chars of [A-Za-z0-9_-].
 * @returns {string}
 */
export function newClientId() {
  return 'c' + Date.now().toString(36) + '-' + randomHex(6);
}

/**
 * Uniform random number in [lo, hi).
 * @param {number} lo
 * @param {number} hi
 * @returns {number}
 */
export function randomBetween(lo, hi) {
  return lo + Math.random() * (hi - lo);
}

/* ------------------------------------------------------------------------------------------ */
/* Strings, numbers, timing                                                                   */
/* ------------------------------------------------------------------------------------------ */

/**
 * Length in Unicode code points (the unit of every server-side limit, SPEC 7.2).
 * @param {string} s
 * @returns {number}
 */
export function cpLength(s) {
  return Array.from(String(s == null ? '' : s)).length;
}

/**
 * First `n` code points of `s`.
 * @param {string} s
 * @param {number} n
 * @returns {string}
 */
export function cpSlice(s, n) {
  return Array.from(String(s == null ? '' : s)).slice(0, n).join('');
}

/**
 * @param {number} n
 * @param {number} lo
 * @param {number} hi
 * @returns {number}
 */
export function clamp(n, lo, hi) {
  return Math.min(hi, Math.max(lo, n));
}

/**
 * Trailing debounce with cancel()/flush().
 * @template {(...a: any[]) => void} F
 * @param {F} fn
 * @param {number} ms
 * @returns {F & {cancel: () => void, flush: () => void}}
 */
export function debounce(fn, ms) {
  let timer = null;
  let lastArgs = null;
  const run = () => {
    timer = null;
    const args = lastArgs;
    lastArgs = null;
    if (args) fn(...args);
  };
  const wrapped = (...args) => {
    lastArgs = args;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(run, ms);
  };
  wrapped.cancel = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
    lastArgs = null;
  };
  wrapped.flush = () => {
    if (timer !== null) {
      clearTimeout(timer);
      run();
    }
  };
  return /** @type {any} */ (wrapped);
}

/**
 * Leading+trailing throttle (at most one call per `ms`, the last call is never lost).
 * @template {(...a: any[]) => void} F
 * @param {F} fn
 * @param {number} ms
 * @returns {F}
 */
export function throttle(fn, ms) {
  let last = 0;
  let timer = null;
  let pending = null;
  const wrapped = (...args) => {
    const now = Date.now();
    const wait = ms - (now - last);
    if (wait <= 0) {
      last = now;
      fn(...args);
      return;
    }
    pending = args;
    if (timer === null) {
      timer = setTimeout(() => {
        timer = null;
        last = Date.now();
        const a = pending;
        pending = null;
        if (a) fn(...a);
      }, wait);
    }
  };
  return /** @type {any} */ (wrapped);
}

/**
 * @param {number} ms
 * @returns {Promise<void>}
 */
export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * NFKC + lower-case, used for client-side filtering (people pickers, chat filter).
 * @param {string} s
 * @returns {string}
 */
export function fold(s) {
  const str = String(s == null ? '' : s);
  try {
    return str.normalize('NFKC').toLowerCase();
  } catch (_) {
    return str.toLowerCase();
  }
}

/**
 * @param {number} n
 * @param {string} one
 * @param {string} [many]
 * @returns {string}
 */
export function pluralize(n, one, many) {
  return n === 1 ? one : many || one + 's';
}

/**
 * Find `@username` tokens (SPEC 7.4 grammar, matched WITHOUT regex lookbehind: Safari < 16.4
 * cannot even parse it, SPEC 9.8). The token is the greedy match of `[A-Za-z0-9._-]{3,32}` after a
 * boundary (start of text or a character outside that class); when `isKnown` rejects it, trailing
 * `.`/`-` are trimmed one at a time. Scanning resumes after the consumed token, so `@a @b` yields
 * two mentions.
 * @param {string} text
 * @param {(lowerCaseUsername: string) => boolean} isKnown is there a current member with exactly this username?
 * @returns {Array<{start: number, end: number, username: string}>} `start` = index of the "@",
 *          `end` = exclusive end of the resolved token, `username` = the text as written
 */
export function findMentions(text, isKnown) {
  const re = /(^|[^A-Za-z0-9._-])@([A-Za-z0-9._-]{3,32})/g;
  const s = String(text == null ? '' : text);
  const out = [];
  let m;
  while ((m = re.exec(s)) !== null) {
    const at = m.index + m[1].length;
    const greedy = m[2];
    let name = greedy;
    while (name.length >= 3 && !isKnown(name.toLowerCase())) {
      const last = name[name.length - 1];
      if (last !== '.' && last !== '-') {
        name = '';
        break;
      }
      name = name.slice(0, -1);
    }
    if (name.length >= 3) {
      out.push({ start: at, end: at + 1 + name.length, username: name });
      re.lastIndex = at + 1 + name.length;
    } else {
      re.lastIndex = at + 1 + greedy.length;
    }
  }
  return out;
}

/**
 * Run `worker` for every item strictly one after the other with pacing (SPEC 9.10 "Add
 * employees"): the next call starts no sooner than `gapMs` after the previous response;
 * `rate_limited` / `server_busy` wait `err.retry_after` and retry the SAME item; `conflict` is
 * reported as "exists" and not retried; any other error marks the row failed and the loop continues.
 * @template T
 * @param {T[]} items
 * @param {(item: T, index: number) => Promise<any>} worker should reject with {code, retry_after?, message?}
 * @param {{gapMs?: number, onRow?: (row: object, index: number) => void, signal?: AbortSignal}} [opts]
 * @returns {Promise<Array<{item: T, status: 'ok'|'exists'|'failed', value?: any, error?: {code: string, message: string}}>>}
 */
export async function runPaced(items, worker, opts = {}) {
  const { gapMs = 2100, onRow, signal } = opts;
  const rows = [];
  let lastEnd = 0;
  for (let i = 0; i < items.length; i += 1) {
    let row;
    for (;;) {
      if (signal && signal.aborted) return rows;
      const wait = lastEnd + gapMs - Date.now();
      if (wait > 0) await sleep(wait);
      try {
        const value = await worker(items[i], i);
        lastEnd = Date.now();
        row = { item: items[i], status: 'ok', value };
        break;
      } catch (err) {
        lastEnd = Date.now();
        const code = (err && err.code) || 'error';
        if (code === 'rate_limited' || code === 'server_busy') {
          await sleep(Math.max(1, Number(err && (err.retry_after ?? err.retryAfter)) || 1) * 1000);
          continue;
        }
        row = { item: items[i], status: code === 'conflict' ? 'exists' : 'failed', error: { code, message: errorText(err) } };
        break;
      }
    }
    rows.push(row);
    if (onRow) onRow(row, i);
  }
  return rows;
}

/* ------------------------------------------------------------------------------------------ */
/* Clock skew (SPEC 8.5)                                                                      */
/* ------------------------------------------------------------------------------------------ */

let skew = 0;

/**
 * Set `server_time - Date.now()/1000` (called by the store on every ev.ready).
 * @param {number} seconds
 */
export function setSkew(seconds) {
  skew = Number.isFinite(seconds) ? seconds : 0;
}

/** @returns {number} current skew in seconds */
export function getSkew() {
  return skew;
}

/**
 * Current server time in epoch seconds (float).
 * @returns {number}
 */
export function serverNow() {
  return Date.now() / 1000 + skew;
}

/* ------------------------------------------------------------------------------------------ */
/* Date and number formatting                                                                 */
/* ------------------------------------------------------------------------------------------ */

/** @returns {string|undefined} */
function locale() {
  try {
    return (typeof navigator !== 'undefined' && navigator.language) || undefined;
  } catch (_) {
    return undefined;
  }
}

/** @type {Map<string, Intl.DateTimeFormat>} */
const fmtCache = new Map();

/**
 * Cached Intl.DateTimeFormat.
 * @param {string} key
 * @param {Intl.DateTimeFormatOptions} opts
 * @returns {Intl.DateTimeFormat}
 */
function fmt(key, opts) {
  let f = fmtCache.get(key);
  if (!f) {
    try {
      f = new Intl.DateTimeFormat(locale(), opts);
    } catch (_) {
      f = new Intl.DateTimeFormat('en-GB', opts);
    }
    fmtCache.set(key, f);
  }
  return f;
}

/**
 * Whole calendar days between the local date of `ts` and the local date of server-now.
 * @param {number} ts epoch seconds
 * @returns {number} 0 = today, 1 = yesterday, negative = future
 */
function dayDiff(ts) {
  const a = new Date(ts * 1000);
  const b = new Date(serverNow() * 1000);
  const da = Date.UTC(a.getFullYear(), a.getMonth(), a.getDate());
  const db = Date.UTC(b.getFullYear(), b.getMonth(), b.getDate());
  return Math.round((db - da) / 86400000);
}

/**
 * @param {number} ts epoch seconds
 * @returns {boolean}
 */
function isCurrentYear(ts) {
  return new Date(ts * 1000).getFullYear() === new Date(serverNow() * 1000).getFullYear();
}

/**
 * "10:42" in the user's locale.
 * @param {number} ts epoch seconds
 * @returns {string}
 */
export function formatTime(ts) {
  if (!Number.isFinite(ts)) return '';
  return fmt('time', { hour: '2-digit', minute: '2-digit' }).format(new Date(ts * 1000));
}

/**
 * @param {number} ts
 * @param {boolean} withYear
 * @returns {string}
 */
function formatDate(ts, withYear) {
  return withYear
    ? fmt('dmy', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(ts * 1000))
    : fmt('dm', { day: 'numeric', month: 'short' }).format(new Date(ts * 1000));
}

/**
 * Date separator label: Today / Yesterday / weekday (< 7 days) / "12 Mar 2025".
 * @param {number} ts
 * @returns {string}
 */
export function formatDayLabel(ts) {
  if (!Number.isFinite(ts)) return '';
  const d = dayDiff(ts);
  if (d === 0) return 'Today';
  if (d === 1) return 'Yesterday';
  if (d > 1 && d < 7) return fmt('wd-long', { weekday: 'long' }).format(new Date(ts * 1000));
  return formatDate(ts, true);
}

/**
 * Chat-list time: HH:MM today, "Yesterday", weekday (< 7 days), else a short numeric date.
 * @param {number} ts
 * @returns {string}
 */
export function formatListTime(ts) {
  if (!Number.isFinite(ts)) return '';
  const d = dayDiff(ts);
  if (d <= 0) return formatTime(ts);
  if (d === 1) return 'Yesterday';
  if (d < 7) return fmt('wd-short', { weekday: 'short' }).format(new Date(ts * 1000));
  return fmt('num', { day: '2-digit', month: '2-digit', year: 'numeric' }).format(new Date(ts * 1000));
}

/**
 * Full date-time for tooltips.
 * @param {number} ts
 * @returns {string}
 */
export function formatDateTime(ts) {
  if (!Number.isFinite(ts)) return '';
  return `${formatDate(ts, true)}, ${formatTime(ts)}`;
}

/**
 * Presence line of SPEC 8.4: "online", "last seen today at 10:42", ... or "" when hidden/unknown.
 * @param {{online?: boolean, last_seen?: number|null}|null|undefined} user
 * @returns {string}
 */
export function formatLastSeen(user) {
  if (!user) return '';
  if (user.online) return 'online';
  const ts = user.last_seen;
  if (typeof ts !== 'number' || !Number.isFinite(ts)) return '';
  const d = dayDiff(ts);
  if (d <= 0) return `last seen today at ${formatTime(ts)}`;
  if (d === 1) return `last seen yesterday at ${formatTime(ts)}`;
  return `last seen ${formatDate(ts, !isCurrentYear(ts))} at ${formatTime(ts)}`;
}

/**
 * m:ss (or h:mm:ss).
 * @param {number} seconds
 * @returns {string}
 */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number.isFinite(seconds) ? seconds : 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = (s < 10 ? '0' : '') + s;
  if (h > 0) return `${h}:${(m < 10 ? '0' : '') + m}:${ss}`;
  return `${m}:${ss}`;
}

/**
 * Human readable size: "1.2 MB".
 * @param {number} n bytes
 * @returns {string}
 */
export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  const s = v >= 100 ? v.toFixed(0) : v.toFixed(1);
  return `${s.replace(/\.0$/, '')} ${units[i]}`;
}

/**
 * Badge text: the number, or "999+" above the cap.
 * @param {number} n
 * @param {number} [cap=999]
 * @returns {string}
 */
export function formatCount(n, cap = 999) {
  return n > cap ? `${cap}+` : String(Math.max(0, Math.floor(n || 0)));
}

/* ------------------------------------------------------------------------------------------ */
/* Error texts                                                                                */
/* ------------------------------------------------------------------------------------------ */

/** Codes whose server-supplied `msg` is already written for end users. */
const SERVER_TEXT_CODES = new Set([
  'bad_request', 'invalid_state', 'conflict', 'weak_password', 'bad_credentials', 'disabled',
  'username_taken', 'name_taken', 'registration_closed', 'setup_code_required', 'bad_setup_code',
  'bad_join_code', 'blocked_type', 'quota_exceeded', 'insufficient_storage', 'forbidden',
  'password_change_required',
]);

/**
 * Human readable text for an ApiError / SocketError / `{code, msg, retry_after}` object.
 * @param {any} err
 * @param {string} [fallback]
 * @returns {string}
 */
export function errorText(err, fallback) {
  const code = err && err.code;
  const server = err && typeof err.message === 'string' ? err.message : '';
  const retry = err && (err.retry_after ?? err.retryAfter);
  if (code && SERVER_TEXT_CODES.has(code) && server) return server;
  switch (code) {
    case 'offline': return "You're offline. It will work again once you're reconnected.";
    case 'connection_lost': return 'The connection was lost. Please try again.';
    case 'timeout': return 'The server took too long to answer.';
    case 'network': return "Can't reach the server.";
    case 'rate_limited':
      return Number.isFinite(retry) && retry > 0
        ? `Too many requests - try again in ${Math.ceil(retry)} s.`
        : 'Too many requests - please slow down.';
    case 'server_busy': return 'The server is busy - try again in a moment.';
    case 'request_timeout': return 'The server did not receive the request in time. Please try again.';
    case 'range_not_satisfiable': return 'That part of the file is not available.';
    case 'server_error': return 'Something went wrong on the server.';
    case 'unauthorized': return 'Your session has expired. Please sign in again.';
    case 'not_member': return 'You are not a member of this chat.';
    case 'not_found': return 'That item no longer exists.';
    case 'forbidden': return "You don't have permission to do that.";
    case 'window_expired': return 'The time limit for this action has passed.';
    case 'too_large': return server || 'That is too large.';
    case 'host_not_allowed': return 'This address is not allowed by the server.';
    default: break;
  }
  return server || fallback || 'Something went wrong.';
}

/* ------------------------------------------------------------------------------------------ */
/* Environment                                                                                */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string} hostname
 * @returns {boolean}
 */
export function isLoopbackHost(hostname) {
  const h = String(hostname || '').toLowerCase();
  return (
    h === 'localhost' || h === '[::1]' || h === '::1' || h.endsWith('.localhost') ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(h)
  );
}

/**
 * True for plain http: to a non-loopback host: the "not encrypted" banner case (SPEC 0).
 * @returns {boolean}
 */
export function isInsecureRemote() {
  try {
    return location.protocol === 'http:' && !isLoopbackHost(location.hostname);
  } catch (_) {
    return false;
  }
}

/**
 * Primary pointer is coarse (phone/tablet).
 * @returns {boolean}
 */
export function isTouchDevice() {
  try {
    return typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
  } catch (_) {
    return false;
  }
}

/**
 * iPhone/iPad (including iPadOS reporting a Mac platform with touch).
 * @returns {boolean}
 */
export function isIOS() {
  try {
    const ua = navigator.userAgent || '';
    return /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  } catch (_) {
    return false;
  }
}

/** @returns {boolean} */
export function prefersReducedMotion() {
  try {
    return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (_) {
    return false;
  }
}

/**
 * OS notifications exist only in secure contexts (SPEC 9.4).
 * @returns {boolean}
 */
export function supportsDesktopNotifications() {
  try {
    return Boolean(window.isSecureContext) && 'Notification' in window;
  } catch (_) {
    return false;
  }
}

/**
 * Voice notes need getUserMedia + MediaRecorder; both are absent on insecure origins (SPEC 0).
 * @returns {boolean}
 */
export function supportsRecording() {
  try {
    return Boolean(navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function' && typeof window.MediaRecorder === 'function');
  } catch (_) {
    return false;
  }
}

/**
 * Copy text: async clipboard API when available, else the execCommand fallback that works on
 * insecure origins.
 * @param {string} text
 * @returns {Promise<boolean>}
 */
export async function copyToClipboard(text) {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) {
    /* fall back below */
  }
  try {
    const prev = document.activeElement;
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.readOnly = true;
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (_) {
      ok = false;
    }
    ta.remove();
    if (prev && typeof prev.focus === 'function') prev.focus({ preventScroll: true });
    return ok;
  } catch (_) {
    return false;
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Namespaced storage (SPEC 9.7)                                                              */
/* ------------------------------------------------------------------------------------------ */

const KEY_ROOT = 'fc:v1:';

/**
 * localStorage with an in-memory mirror: every access is try/catch'd so a blocked or full
 * storage degrades to memory. Namespaced keys are `fc:v1:<instance_id>:<user_id>:<name>`.
 */
class Storage {
  constructor() {
    /** @type {Map<string, string>} */
    this._mem = new Map();
    /** @type {string|null} */
    this._ns = null;
  }

  /** @returns {Storage|null} the browser storage object or null */
  _ls() {
    try {
      return typeof window !== 'undefined' && window.localStorage ? window.localStorage : null;
    } catch (_) {
      return null;
    }
  }

  /** @param {string} key @returns {string|null} */
  _read(key) {
    const ls = this._ls();
    if (ls) {
      try {
        const v = ls.getItem(key);
        if (v !== null) return v;
      } catch (_) {
        /* use memory */
      }
    }
    return this._mem.has(key) ? /** @type {string} */ (this._mem.get(key)) : null;
  }

  /** @param {string} key @param {string} value */
  _write(key, value) {
    this._mem.set(key, value);
    const ls = this._ls();
    if (!ls) return;
    try {
      ls.setItem(key, value);
    } catch (_) {
      /* quota or blocked: the memory mirror still holds it */
    }
  }

  /** @param {string} key */
  _delete(key) {
    this._mem.delete(key);
    const ls = this._ls();
    if (!ls) return;
    try {
      ls.removeItem(key);
    } catch (_) {
      /* ignore */
    }
  }

  /** @param {string} prefix @returns {string[]} full keys starting with prefix */
  _keys(prefix) {
    const out = new Set();
    for (const k of this._mem.keys()) if (k.startsWith(prefix)) out.add(k);
    const ls = this._ls();
    if (ls) {
      try {
        for (let i = 0; i < ls.length; i += 1) {
          const k = ls.key(i);
          if (k && k.startsWith(prefix)) out.add(k);
        }
      } catch (_) {
        /* ignore */
      }
    }
    return Array.from(out);
  }

  /** @param {string} s @param {any} fallback */
  _parse(s, fallback) {
    if (s === null) return fallback;
    try {
      return JSON.parse(s);
    } catch (_) {
      return fallback;
    }
  }

  /**
   * Select the namespace for user data. Must be called before any namespaced access.
   * @param {string|number} instanceId
   * @param {string|number} userId
   */
  setNamespace(instanceId, userId) {
    this._ns = `${KEY_ROOT}${String(instanceId)}:${String(userId)}:`;
  }

  /** Forget the namespace (logout): namespaced accessors become no-ops. */
  clearNamespace() {
    this._ns = null;
  }

  /** @returns {boolean} whether a namespace is selected */
  get ready() {
    return this._ns !== null;
  }

  /**
   * @param {string} name
   * @param {any} [fallback=null]
   * @returns {any}
   */
  get(name, fallback = null) {
    if (this._ns === null) return fallback;
    return this._parse(this._read(this._ns + name), fallback);
  }

  /**
   * @param {string} name
   * @param {any} value JSON-serialisable
   */
  set(name, value) {
    if (this._ns === null) return;
    try {
      this._write(this._ns + name, JSON.stringify(value));
    } catch (_) {
      /* unserialisable value: ignore */
    }
  }

  /** @param {string} name */
  remove(name) {
    if (this._ns === null) return;
    this._delete(this._ns + name);
  }

  /**
   * Names (without the namespace) of the current namespace starting with `namePrefix`.
   * @param {string} [namePrefix='']
   * @returns {string[]}
   */
  keys(namePrefix = '') {
    if (this._ns === null) return [];
    const ns = this._ns;
    return this._keys(ns + namePrefix).map((k) => k.slice(ns.length));
  }

  /** Remove every key of the current namespace (logout / kick / 401). */
  wipe() {
    if (this._ns === null) return;
    for (const k of this._keys(this._ns)) this._delete(k);
  }

  /**
   * Device-level (not user-specific) value, e.g. UI preferences.
   * @param {string} name
   * @param {any} [fallback=null]
   * @returns {any}
   */
  getGlobal(name, fallback = null) {
    return this._parse(this._read(KEY_ROOT + name), fallback);
  }

  /** @param {string} name @param {any} value */
  setGlobal(name, value) {
    try {
      this._write(KEY_ROOT + name, JSON.stringify(value));
    } catch (_) {
      /* ignore */
    }
  }

  /** @param {string} name */
  removeGlobal(name) {
    this._delete(KEY_ROOT + name);
  }
}

export const storage = new Storage();
