/**
 * core/store.js - all server-derived client state and its reducers (SPEC 7.6(9), 8, 9.1, 9.6).
 *
 * The store subscribes to the socket's `ev.*` events and keeps: me, users, chats (with the
 * section 9.1 reducers and the max()-merge rules), the message window of the ONE open chat
 * (one contiguous id range + has_more flags, history buffering of 9.6), unread counters with the
 * as-of rule of 8.2, derived message status of 8.1, typing, drafts, delivered/read receipts and
 * the device preferences. Views read it through the functions below and listen to its events.
 *
 * Full API reference: docs/ui-core-api.md section 7. Events (store.on): ready, reset, me,
 * workspace, limits, users, user:<id>, presence, chats, chat:<id>, chat_removed, messages:<chatId>,
 * message_update, message_removed, own_message, incoming, read_sync, typing:<chatId>, connection,
 * ui, active_chat, prefs, draft:<chatId>, drafts, outbox:<chatId>, outbox, badge.
 *
 * Everything here runs synchronously inside the socket handler (SPEC 9.6 rule 8); the only
 * timers are the 150 ms delivered batch and the 300 ms read debounce of SPEC 8.2 (plain
 * setTimeout, no animation-frame scheduling).
 */

import { Emitter, setSkew, serverNow, storage, fold, cpSlice, formatDuration, isTouchDevice } from './util.js';
import { socket as defaultSocket, SocketError } from './socket.js';

/** Maximum number of messages kept in the open window (SPEC 9.6). */
export const WINDOW_CAP = 400;
const PAGE = 50;
const UNREAD_CAP = 1000;
const TYPING_TTL_MS = 8000;
const TYPING_OUT_REFRESH_MS = 2500;
const TYPING_OUT_IDLE_MS = 5000;
const IGNORE_CHAT_MS = 60000;
const BANNER_DELAY_MS = 2000;
const MAX_UNKNOWN_QUEUE = 100;

/** Defaults for ev.ready.limits (the server value always wins). */
const DEFAULT_LIMITS = Object.freeze({
  max_upload_bytes: 100 * 1024 * 1024,
  max_body_chars: 8000,
  edit_window_s: 900,
  delete_window_s: 172800,
  max_pinned_chats: 3,
  max_pinned_messages: 5,
  max_group_members: 200,
  max_forward_messages: 20,
  max_forward_chats: 5,
});

const STATUS_RANK = { sent: 1, delivered: 2, read: 3 };
const PUBLIC_USER_FIELDS = ['id', 'username', 'display_name', 'status_text', 'role', 'online', 'last_seen', 'disabled', 'activated', 'read_receipts'];

/** Calls whose responses carry state that must be applied (`store.request`). */
const APPLY_MESSAGE_TYPES = new Set(['msg.send', 'msg.edit', 'msg.delete', 'msg.react', 'msg.star']);

/* ------------------------------------------------------------------------------------------ */
/* Pure helpers (exported for tests)                                                          */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string|null|undefined} s
 * @returns {number} 0 for null
 */
export function statusRank(s) {
  return (s && STATUS_RANK[/** @type {'sent'} */ (s)]) || 0;
}

/**
 * The higher of two statuses (sent < delivered < read); null loses against any status.
 * @param {string|null|undefined} a
 * @param {string|null|undefined} b
 * @returns {string|null}
 */
export function maxStatus(a, b) {
  return statusRank(b) > statusRank(a) ? (b || null) : (a || null);
}

/**
 * Message status of the sender's message `m` per SPEC 8.1.
 * @param {any} chat the Chat (members with watermarks)
 * @param {any} m the Message
 * @param {number} meId
 * @param {(id: number) => any} getUser directory lookup
 * @returns {'sent'|'delivered'|'read'|null}
 */
export function deriveStatus(chat, m, meId, getUser) {
  if (!chat || !m || m.sender_id !== meId || m.kind === 'system') return null;
  if (chat.kind === 'direct' && chat.peer_id === meId) return null;
  return statusFromAggregates(statusAggregates(chat, meId, getUser), m.id);
}

/**
 * Group-level numbers behind SPEC 8.1, computed once per chat so that large groups stay cheap:
 * R = members except me, disabled users and never-activated users; R_read = those of R with read
 * receipts on.
 * @param {any} chat
 * @param {number} meId
 * @param {(id: number) => any} getUser
 * @returns {{any: boolean, minDelivered: number, anyReader: boolean, minRead: number}}
 */
export function statusAggregates(chat, meId, getUser) {
  let anyRecipient = false;
  let minDelivered = Infinity;
  let anyReader = false;
  let minRead = Infinity;
  for (const mem of chat.members || []) {
    if (mem.user_id === meId) continue;
    const u = getUser(mem.user_id);
    if (u && (u.disabled || u.activated === false)) continue;
    anyRecipient = true;
    const read = mem.read_up_to || 0;
    const delivered = Math.max(mem.delivered_up_to || 0, read);
    if (delivered < minDelivered) minDelivered = delivered;
    if (!u || u.read_receipts !== false) {
      anyReader = true;
      if (read < minRead) minRead = read;
    }
  }
  return { any: anyRecipient, minDelivered, anyReader, minRead };
}

/**
 * @param {{any: boolean, minDelivered: number, anyReader: boolean, minRead: number}} agg
 * @param {number} messageId
 * @returns {'sent'|'delivered'|'read'}
 */
export function statusFromAggregates(agg, messageId) {
  if (!agg.any || agg.minDelivered < messageId) return 'sent';
  return agg.anyReader && agg.minRead >= messageId ? 'read' : 'delivered';
}

/**
 * Binary search in an id-ascending array of messages.
 * @param {Array<{id: number}>} items
 * @param {number} id
 * @returns {number} index or -1
 */
export function findIndexById(items, id) {
  let lo = 0;
  let hi = items.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const v = items[mid].id;
    if (v === id) return mid;
    if (v < id) lo = mid + 1;
    else hi = mid - 1;
  }
  return -1;
}

/**
 * @param {number|null|undefined} a
 * @param {number|null|undefined} b
 * @returns {number|null} the larger value; null only when both are null/undefined
 */
function maxNullable(a, b) {
  if (a === null || a === undefined) return b === undefined ? null : b;
  if (b === null || b === undefined) return a;
  return Math.max(a, b);
}

/**
 * Index at which `id` must be inserted to keep an id-ascending array sorted.
 * @param {Array<{id: number}>} items
 * @param {number} id
 * @returns {number}
 */
export function insertionIndex(items, id) {
  let lo = 0;
  let hi = items.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (items[mid].id < id) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Recompute lo/hi of a window from its items.
 * @param {{lo: number, hi: number, items: Array<{id: number}>}} w
 */
function fixBounds(w) {
  w.lo = w.items.length ? w.items[0].id : 0;
  w.hi = w.items.length ? w.items[w.items.length - 1].id : 0;
}

/**
 * @param {any} u a user object
 * @returns {any} only the public directory fields
 */
function publicUser(u) {
  const out = {};
  for (const k of PUBLIC_USER_FIELDS) if (k in u) out[k] = u[k];
  return out;
}

/**
 * @param {Array<{id: number}>} heldMentions held countable mention entries
 * @returns {number|null} the smallest id, or null when there is none
 */
function minHeldMention(heldMentions) {
  let min = null;
  for (const e of heldMentions) if (min === null || e.id < min) min = e.id;
  return min;
}

/**
 * @param {any} m
 * @param {number} meId
 * @returns {boolean} whether the message counts towards the unread counter (SPEC 9.1)
 */
function isCountable(m, meId) {
  return m.sender_id !== meId && m.kind !== 'system';
}

/**
 * @param {any} m
 * @param {number} meId
 * @returns {boolean}
 */
function mentionsMe(m, meId) {
  return Array.isArray(m.mentions) && m.mentions.includes(meId);
}

/**
 * @param {string} s
 * @returns {string} the first non-empty line, trimmed
 */
function firstLine(s) {
  const line = String(s || '').split(/\r?\n/).find((l) => l.trim() !== '');
  return (line || '').trim();
}

/* ------------------------------------------------------------------------------------------ */
/* Preferences                                                                                */
/* ------------------------------------------------------------------------------------------ */

const PREF_DEFAULTS = Object.freeze({
  theme: 'system',
  fontSize: 'normal',
  sound: true,
  volume: 0.7,
  previews: true,
  enterSends: null,
  desktopNotifications: true,
  sentSound: false,
});

/** @type {Record<string, (v: any) => any>} */
const PREF_VALIDATORS = {
  theme: (v) => (v === 'light' || v === 'dark' ? v : 'system'),
  fontSize: (v) => (v === 'small' || v === 'large' ? v : 'normal'),
  sound: (v) => Boolean(v),
  volume: (v) => (Number.isFinite(Number(v)) ? Math.min(1, Math.max(0, Number(v))) : 0.7),
  previews: (v) => Boolean(v),
  enterSends: (v) => (v === null || v === undefined ? null : Boolean(v)),
  desktopNotifications: (v) => Boolean(v),
  sentSound: (v) => Boolean(v),
};

/** Device-level preferences persisted under the global storage key `prefs`. */
class Prefs {
  /**
   * @param {(key: string, value: any) => void} onChange
   */
  constructor(onChange) {
    this._onChange = onChange;
    const saved = storage.getGlobal('prefs', {});
    /** @type {Record<string, any>} */
    this._v = { ...PREF_DEFAULTS };
    if (saved && typeof saved === 'object') {
      for (const k of Object.keys(PREF_VALIDATORS)) if (k in saved) this._v[k] = PREF_VALIDATORS[k](saved[k]);
    }
  }

  /**
   * @param {string} key
   * @returns {any}
   */
  get(key) {
    return this._v[key];
  }

  /**
   * @param {string} key one of the documented preference keys
   * @param {any} value
   */
  set(key, value) {
    const validate = PREF_VALIDATORS[key];
    if (!validate) return;
    const v = validate(value);
    if (this._v[key] === v) return;
    this._v[key] = v;
    storage.setGlobal('prefs', this._v);
    this._onChange(key, v);
  }

  /** @returns {Record<string, any>} a copy of all preferences */
  all() {
    return { ...this._v };
  }

  /** @returns {boolean} does Enter send the message (auto: yes on desktop, no on touch devices) */
  enterSends() {
    const v = this._v.enterSends;
    return v === null || v === undefined ? !isTouchDevice() : Boolean(v);
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Store                                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * Per-chat client-local state (never replaced by server events).
 * @typedef {object} ChatLocal
 * @property {null|{lo:number, hi:number, has_more_before:boolean, has_more_after:boolean, seen_hi:number, items:any[]}} win
 * @property {number} gen incremented whenever the window is replaced
 * @property {number} histSeq sequence of replace-loads
 * @property {null|{seq:number, buffered:any[], newestEv:number}} inflight history buffering state (SPEC 9.6); newestEv = largest ev.message id seen since the request was sent
 * @property {Array<{id:number, mention:boolean}>} held countable messages since the last counters payload (SPEC 8.2)
 * @property {{stuck:boolean, anchorId:number|null}} view
 * @property {any} openInfo
 * @property {boolean} loadingBefore
 * @property {boolean} loadingAfter
 */

export class Store extends Emitter {
  /**
   * @param {{socket?: any}} [deps]
   */
  constructor(deps = {}) {
    super();
    this._socket = deps.socket || defaultSocket;
    /** @type {any} */
    this.me = null;
    this.workspace = { name: 'DeskTalk', registration_open: false };
    this.limits = { ...DEFAULT_LIMITS };
    /** @type {string|null} */
    this.instanceId = null;
    this.epoch = 0;
    this.isReady = false;
    this.connection = { state: 'idle', banner: false, online: true, outdated: false };
    this.ui = { activeChatId: /** @type {number|null} */ (null), visible: true, focused: true, soundBlocked: false };
    /** @type {Map<number, any>} */
    this._users = new Map();
    /** @type {Map<number, any>} */
    this._chats = new Map();
    /** @type {Map<number, ChatLocal>} */
    this._local = new Map();
    /** @type {Map<number, Map<number, {state: string, until: number}>>} */
    this._typing = new Map();
    this._typingTimer = /** @type {any} */ (null);
    /** @type {Map<number, {state: string, at: number, timer: any}>} */
    this._typingOut = new Map();
    /** @type {Map<number, any>} */
    this._drafts = new Map();
    /** @type {Map<number, any>} */
    this._draftTimers = new Map();
    /** @type {Map<number, any[]>} */
    this._unknown = new Map();
    /** @type {Map<number, Promise<any>>} */
    this._refreshing = new Map();
    /** @type {Map<number, number>} */
    this._ignoredUntil = new Map();
    /** @type {Set<number>} */
    this._deliveredPending = new Set();
    /** @type {Map<number, number>} */
    this._deliveredSent = new Map();
    this._deliveredTimer = /** @type {any} */ (null);
    /** @type {Map<number, any>} */
    this._readTimers = new Map();
    /** @type {Map<number, number>} */
    this._readSent = new Map();
    this._bannerTimer = /** @type {any} */ (null);
    this._lastBadge = { count: -1, display: '' };
    this._started = false;
    this.prefs = new Prefs((key, value) => this.emit('prefs', { key, value }));
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Lifecycle                                                                                */
  /* ---------------------------------------------------------------------------------------- */

  /** Subscribe to the socket and the page (idempotent). */
  start() {
    if (this._started) return;
    this._started = true;
    const s = this._socket;
    /** @type {Record<string, (d: any) => void>} */
    const handlers = {
      'ev.ready': (d) => this._onReady(d),
      'ev.me': (d) => this._onMe(d),
      'ev.message': (d) => d && d.message && this._onMessage(d.message, 'event'),
      'ev.message_update': (d) => d && d.message && this._onMessageUpdate(d.message),
      'ev.message_removed': (d) => this._onMessageRemoved(d),
      'ev.receipt': (d) => this._onReceipt(d),
      'ev.read_sync': (d) => this._onReadSync(d),
      'ev.typing': (d) => this._onTyping(d),
      'ev.presence': (d) => this._onPresence(d),
      'ev.chat_update': (d) => d && d.chat && this._applyChat(d.chat, 'event'),
      'ev.chat_members': (d) => this._onChatMembers(d),
      'ev.chat_removed': (d) => d && this._onChatRemoved(d.chat_id),
      'ev.user_update': (d) => d && d.user && this._onUserUpdate(d.user),
      'ev.workspace': (d) => this._onWorkspace(d),
    };
    for (const name of Object.keys(handlers)) s.on(name, handlers[name]);
    s.on('state', (state) => this._onSocketState(state));
    this._onSocketState(s.state);
    if (typeof document !== 'undefined' && typeof window !== 'undefined') {
      this.ui.visible = document.visibilityState !== 'hidden';
      this.ui.focused = typeof document.hasFocus === 'function' ? document.hasFocus() : true;
      this.connection.online = navigator.onLine !== false;
      document.addEventListener('visibilitychange', () => this._onVisibility());
      window.addEventListener('focus', () => this._onFocus(true));
      window.addEventListener('blur', () => this._onFocus(false));
      window.addEventListener('pageshow', () => this._onVisibility());
      window.addEventListener('online', () => this._onOnline(true));
      window.addEventListener('offline', () => this._onOnline(false));
    }
  }

  /** Drop all state (logout, kicked, 401). Subscriptions stay. */
  reset() {
    for (const t of this._readTimers.values()) clearTimeout(t);
    for (const t of this._draftTimers.values()) clearTimeout(t);
    for (const o of this._typingOut.values()) clearTimeout(o.timer);
    if (this._deliveredTimer) clearTimeout(this._deliveredTimer);
    if (this._typingTimer) clearInterval(this._typingTimer);
    this._deliveredTimer = null;
    this._typingTimer = null;
    this._readTimers.clear();
    this._draftTimers.clear();
    this._typingOut.clear();
    this._typing.clear();
    this._deliveredPending.clear();
    this._deliveredSent.clear();
    this._readSent.clear();
    this._unknown.clear();
    this._refreshing.clear();
    this._ignoredUntil.clear();
    this._drafts.clear();
    this._users = new Map();
    this._chats = new Map();
    this._local = new Map();
    this.me = null;
    this.instanceId = null;
    this.isReady = false;
    this.ui.activeChatId = null;
    storage.clearNamespace();
    this._lastBadge = { count: -1, display: '' };
    this.emit('reset');
    this.emit('chats');
    this._checkBadge();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Lookups and derived helpers                                                              */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {number} id @returns {any} */
  getUser(id) {
    return this._users.get(id);
  }

  /** @returns {any[]} directory including me */
  users() {
    return Array.from(this._users.values());
  }

  /** @param {number|null|undefined} id @returns {string} */
  userName(id) {
    const u = id === null || id === undefined ? null : this._users.get(id);
    return u ? u.display_name : 'Unknown user';
  }

  /** @param {number} id @returns {any} */
  getChat(id) {
    return this._chats.get(id);
  }

  /** @returns {any[]} */
  chats() {
    return Array.from(this._chats.values());
  }

  /**
   * Sorted chat list (SPEC 3.2(7)): pinned first (pinned_at desc), then last_activity_at desc.
   * @param {{filter?: 'all'|'unread'|'groups', query?: string, archived?: boolean}} [o]
   * @returns {any[]}
   */
  chatList(o = {}) {
    const { filter = 'all', query = '', archived = false } = o;
    const q = fold(query).trim();
    const list = [];
    for (const c of this._chats.values()) {
      if (Boolean(c.me && c.me.archived) !== archived) continue;
      if (filter === 'unread' && !(c.me.unread > 0 || c.me.unread_mentions > 0)) continue;
      if (filter === 'groups' && c.kind !== 'group') continue;
      if (q) {
        const peer = this.chatPeer(c);
        const hay = fold(this.chatTitle(c)) + '\n' + (peer ? fold(peer.username) : '');
        if (!hay.includes(q)) continue;
      }
      list.push(c);
    }
    list.sort((a, b) => {
      const pa = a.me.pinned ? 1 : 0;
      const pb = b.me.pinned ? 1 : 0;
      if (pa !== pb) return pb - pa;
      if (pa && pb) {
        const d = (b.me.pinned_at || 0) - (a.me.pinned_at || 0);
        if (d !== 0) return d;
      }
      const d = (b.last_activity_at || 0) - (a.last_activity_at || 0);
      return d !== 0 ? d : b.id - a.id;
    });
    return list;
  }

  /** @param {any} chat @returns {boolean} */
  isSelfChat(chat) {
    return Boolean(chat && this.me && chat.kind === 'direct' && chat.peer_id === this.me.id);
  }

  /** @param {any} chat @returns {number|null} */
  chatPeerId(chat) {
    return chat && chat.kind === 'direct' && typeof chat.peer_id === 'number' ? chat.peer_id : null;
  }

  /** @param {any} chat @returns {any} the peer User of a direct chat */
  chatPeer(chat) {
    const id = this.chatPeerId(chat);
    return id === null ? undefined : this._users.get(id);
  }

  /** @param {any} chat @returns {string} */
  chatTitle(chat) {
    if (!chat) return '';
    if (chat.kind === 'group') return chat.title || 'Group';
    if (this.isSelfChat(chat)) return 'You';
    const peer = this.chatPeer(chat);
    return peer ? peer.display_name : 'Unknown user';
  }

  /** @param {number} userId @returns {any} existing direct chat with that user */
  directChatWith(userId) {
    for (const c of this._chats.values()) if (c.kind === 'direct' && c.peer_id === userId) return c;
    return undefined;
  }

  /** @param {any} chat @param {number} userId @returns {any} */
  memberOf(chat, userId) {
    return chat && Array.isArray(chat.members) ? chat.members.find((m) => m.user_id === userId) : undefined;
  }

  /** @param {any} chat @returns {any} */
  myMember(chat) {
    return this.me ? this.memberOf(chat, this.me.id) : undefined;
  }

  /** @param {any} chat @param {number} [userId] @returns {boolean} */
  isGroupAdmin(chat, userId) {
    const id = userId === undefined && this.me ? this.me.id : userId;
    const m = this.memberOf(chat, /** @type {number} */ (id));
    return Boolean(m && m.role === 'admin');
  }

  /** @param {any} chat @returns {boolean} */
  isMuted(chat) {
    return Boolean(chat && chat.me && chat.me.muted_until > serverNow());
  }

  /**
   * Can the user compose in this chat? (SPEC 9.7)
   * @param {any} chat
   * @returns {{ok: true}|{ok: false, reason: string, text: string}}
   */
  canPost(chat) {
    if (!chat) return { ok: false, reason: 'not_member', text: 'You are no longer a member of this chat.' };
    if (chat.kind === 'group' && chat.only_admins_post && !this.isGroupAdmin(chat)) {
      return { ok: false, reason: 'admins_only', text: 'Only admins can send messages to this group.' };
    }
    if (chat.kind === 'direct' && !this.isSelfChat(chat)) {
      const peer = this.chatPeer(chat);
      if (peer && peer.disabled) return { ok: false, reason: 'peer_disabled', text: `${peer.display_name}'s account is disabled` };
    }
    return { ok: true };
  }

  /**
   * Total badge number of SPEC 9.4: unread of unarchived unmuted chats + mentions of muted unarchived ones.
   * @returns {{count: number, display: string}}
   */
  totalUnread() {
    let n = 0;
    for (const c of this._chats.values()) {
      if (c.me.archived) continue;
      n += this.isMuted(c) ? c.me.unread_mentions || 0 : c.me.unread || 0;
    }
    return { count: n, display: n > 99 ? '99+' : String(n) };
  }

  /**
   * Derived status of an own message (SPEC 8.1).
   * @param {number} chatId
   * @param {any} message
   * @returns {'sent'|'delivered'|'read'|null}
   */
  messageStatus(chatId, message) {
    return deriveStatus(this._chats.get(chatId), message, this.me ? this.me.id : -1, (id) => this._users.get(id));
  }

  /**
   * One-line preview text of a message (SPEC 9.10).
   * @param {any} message
   * @returns {string}
   */
  summarize(message) {
    if (!message) return '';
    const own = this.me && message.sender_id === this.me.id;
    if (message.deleted) return own ? 'You deleted this message' : 'This message was deleted';
    const att = message.attachment;
    let t;
    switch (message.kind) {
      case 'image': t = 'Photo'; break;
      case 'video': t = 'Video'; break;
      case 'audio': t = att && att.duration ? `Audio (${formatDuration(att.duration)})` : 'Audio'; break;
      case 'file': t = (att && att.name) || 'File'; break;
      case 'system': return message.body || '';
      default: t = cpSlice(firstLine(message.body), 100); break;
    }
    return message.forwarded ? `Forwarded: ${t}` : t;
  }

  /**
   * Chat-row preview of the last message (SPEC 9.10).
   * @param {any} chat
   * @returns {{prefix: string, text: string, deleted: boolean, own: boolean, message: any}|null}
   */
  lastPreview(chat) {
    const m = chat && chat.last_message;
    if (!m) return null;
    const own = Boolean(this.me && m.sender_id === this.me.id);
    let prefix = '';
    if (m.kind !== 'system' && !m.deleted) {
      if (own) prefix = 'You: ';
      else if (chat.kind === 'group' && m.sender_id !== null) prefix = `${this.userName(m.sender_id)}: `;
    }
    return { prefix, text: this.summarize(m), deleted: Boolean(m.deleted), own, message: m };
  }

  /* ---------------------------------------------------------------------------------------- */
  /* UI state                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Merge keys into `store.ui` and emit 'ui'.
   * @param {Record<string, any>} patch
   */
  setUi(patch) {
    const keys = [];
    for (const k of Object.keys(patch)) {
      if (/** @type {any} */ (this.ui)[k] !== patch[k]) {
        /** @type {any} */ (this.ui)[k] = patch[k];
        keys.push(k);
      }
    }
    if (keys.length) this.emit('ui', { keys });
  }

  /** @param {string} state socket state */
  _onSocketState(state) {
    this.connection.state = state;
    this.connection.outdated = state === 'outdated';
    const terminal = ['idle', 'stopped', 'kicked', 'unauthorized', 'password_change', 'toomany', 'outdated'].includes(state);
    if (state === 'ready' || terminal) {
      if (this._bannerTimer) clearTimeout(this._bannerTimer);
      this._bannerTimer = null;
      this.connection.banner = false;
    } else if (!this.connection.banner && !this._bannerTimer) {
      this._bannerTimer = setTimeout(() => {
        this._bannerTimer = null;
        if (this._socket.state !== 'ready') {
          this.connection.banner = true;
          this.emit('connection', this.connection);
        }
      }, BANNER_DELAY_MS);
    }
    this.emit('connection', this.connection);
  }

  /** @param {boolean} online */
  _onOnline(online) {
    this.connection.online = online;
    this.emit('connection', this.connection);
  }

  _onVisibility() {
    const visible = document.visibilityState !== 'hidden';
    this.setUi({ visible });
    if (visible) this._scheduleRead(this.ui.activeChatId, 0);
  }

  /** @param {boolean} focused */
  _onFocus(focused) {
    this.setUi({ focused });
    if (focused) this._scheduleRead(this.ui.activeChatId, 0);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* ev.ready                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Replace ALL server-derived state (SPEC 8.5): users, chats, me, limits, skew; drop every
   * held message; keep UI state (drafts, view state of the open chat, outbox).
   * @param {any} d ev.ready payload
   */
  _onReady(d) {
    if (!d || !d.me) return;
    const prevActive = this.ui.activeChatId;
    const prevLocal = this._local;
    this.epoch += 1;
    setSkew((Number(d.server_time) || Date.now() / 1000) - Date.now() / 1000);
    this.instanceId = d.instance_id === undefined || d.instance_id === null ? null : String(d.instance_id);
    this.me = { ...d.me };
    const ws = d.workspace || {};
    this.workspace = { name: ws.name || 'DeskTalk', registration_open: Boolean(ws.registration_open) };
    this.limits = { ...DEFAULT_LIMITS, ...(d.limits || {}) };
    if (this.instanceId !== null) storage.setNamespace(this.instanceId, this.me.id);

    this._users = new Map();
    for (const u of d.users || []) this._users.set(u.id, u);
    if (!this._users.has(this.me.id)) this._users.set(this.me.id, publicUser(this.me));

    this._chats = new Map();
    this._local = new Map();
    for (const c of d.chats || []) {
      this._chats.set(c.id, c);
      this._ignoredUntil.delete(c.id);
    }
    if (prevActive !== null && prevLocal.has(prevActive)) {
      const old = /** @type {ChatLocal} */ (prevLocal.get(prevActive));
      const L = this._loc(prevActive);
      L.view = old.view;
      L.openInfo = old.openInfo;
    }
    for (const c of this._chats.values()) this._seedLastMessage(c);

    const hadTyping = Array.from(this._typing.keys());
    this._typing.clear();
    for (const o of this._typingOut.values()) clearTimeout(o.timer);
    this._typingOut.clear();
    for (const t of this._readTimers.values()) clearTimeout(t);
    this._readTimers.clear();
    if (this._deliveredTimer) clearTimeout(this._deliveredTimer);
    this._deliveredTimer = null;
    this._deliveredPending.clear();
    this._deliveredSent.clear();
    this._readSent.clear();
    this._unknown.clear();
    this._refreshing.clear();
    this.isReady = true;
    this._loadDrafts();

    this.emit('me', this.me);
    this.emit('workspace', this.workspace);
    this.emit('limits', this.limits);
    this.emit('users');
    for (const c of this._chats.values()) this.emit(`chat:${c.id}`, c);
    for (const id of hadTyping) this.emit(`typing:${id}`, []);
    this._touchChats();
    if (prevActive !== null) {
      if (this._chats.has(prevActive)) {
        this.emit(`messages:${prevActive}`, { type: 'reset', chatId: prevActive, ids: [] });
        this._reloadAfterReady(prevActive);
      } else {
        this.ui.activeChatId = null;
        this.emit('ui', { keys: ['activeChatId'] });
        this.emit('active_chat', { chatId: null, prev: prevActive });
        this.emit('chat_removed', { chat_id: prevActive, title: '', kind: 'group' });
      }
    }
    this._evaluateDeliveredAll();
    this.emit('ready', this.epoch);
  }

  /**
   * Reload the open chat's window after a reconnect (SPEC 8.5(c)).
   * @param {number} chatId
   */
  async _reloadAfterReady(chatId) {
    const L = this._loc(chatId);
    const { stuck, anchorId } = L.view;
    try {
      if (stuck || !anchorId) {
        await this._loadReplace(chatId, {});
      } else {
        try {
          await this._loadReplace(chatId, { around_id: anchorId, limit: 100 });
        } catch (err) {
          if (err && err.code === 'not_found') await this._loadReplace(chatId, {});
          else throw err;
        }
      }
    } catch (err) {
      if (err && err.code === 'not_member') this._dropChat(chatId);
      else if (!err || err.code !== 'cancelled') console.warn('[store] window reload failed', err);
    }
  }

  /** Seed the derived status of an own last_message. @param {any} chat */
  _seedLastMessage(chat) {
    const m = chat.last_message;
    if (m && this.me && m.sender_id === this.me.id) m.status = maxStatus(m.status, this.messageStatus(chat.id, m));
    else if (m) m.status = null;
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Per-chat local state                                                                     */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * @param {number} chatId
   * @returns {ChatLocal}
   */
  _loc(chatId) {
    let L = this._local.get(chatId);
    if (!L) {
      L = {
        win: null, gen: 0, histSeq: 0, inflight: null, held: [], view: { stuck: true, anchorId: null },
        openInfo: null, loadingBefore: false, loadingAfter: false,
      };
      this._local.set(chatId, L);
    }
    return L;
  }

  /** Emit the coarse chat-list events and the badge. */
  _touchChats() {
    this.emit('chats');
    this._checkBadge();
  }

  _checkBadge() {
    const b = this.totalUnread();
    if (b.count === this._lastBadge.count && b.display === this._lastBadge.display) return;
    this._lastBadge = b;
    this.emit('badge', b);
  }

  /**
   * Chat events for an unknown chat id: remember the event, fetch the chat once, re-apply
   * afterwards (SPEC 7.6(9)).
   * @param {number} chatId
   * @param {{t: string, [k: string]: any}} ev
   */
  _queueForUnknown(chatId, ev) {
    if (typeof chatId !== 'number' || (this._ignoredUntil.get(chatId) || 0) > Date.now()) return;
    let q = this._unknown.get(chatId);
    if (!q) {
      q = [];
      this._unknown.set(chatId, q);
    }
    if (q.length < MAX_UNKNOWN_QUEUE) q.push(ev);
    if (this._refreshing.has(chatId)) return;
    this.refreshChat(chatId).then((chat) => {
      const events = this._unknown.get(chatId) || [];
      this._unknown.delete(chatId);
      if (!chat) return;
      for (const e of events) this._replayUnknown(e);
    }, () => {
      this._unknown.delete(chatId);
    });
  }

  /** @param {any} e */
  _replayUnknown(e) {
    if (e.t === 'message') this._onMessage(e.m, e.source);
    else if (e.t === 'update') this._onMessageUpdate(e.m);
    else if (e.t === 'removed') this._onMessageRemoved(e.d);
    else if (e.t === 'members') this._onChatMembers(e.d);
    else if (e.t === 'typing') this._onTyping(e.d);
    else if (e.t === 'read_sync') this._onReadSync(e.d);
  }

  /**
   * Fetch a chat with `chat.get` (deduplicated). Resolves null on `not_member`, which also makes
   * the store ignore events of that chat for 60 s.
   * @param {number} chatId
   * @returns {Promise<any>}
   */
  refreshChat(chatId) {
    const existing = this._refreshing.get(chatId);
    if (existing) return existing;
    const p = this._socket.request('chat.get', { chat_id: chatId }).then(
      (res) => {
        this._refreshing.delete(chatId);
        if (res && res.chat) {
          this.applyChat(res.chat);
          return this._chats.get(chatId) || null;
        }
        return null;
      },
      (err) => {
        this._refreshing.delete(chatId);
        if (err && err.code === 'not_member') {
          this._ignoredUntil.set(chatId, Date.now() + IGNORE_CHAT_MS);
          if (this._chats.has(chatId)) this._dropChat(chatId);
          return null;
        }
        throw err;
      },
    );
    this._refreshing.set(chatId, p);
    return p;
  }

  /**
   * Remove a chat locally and announce it.
   * @param {number} chatId
   */
  _dropChat(chatId) {
    const chat = this._chats.get(chatId);
    if (!chat) return;
    const title = this.chatTitle(chat);
    const kind = chat.kind;
    // SPEC 9.6: the Chat, its window, held countable entries, typing entries and inflight[chat] go;
    // drafts stay; events for the id are ignored for 60 s unless a later ev.chat_update re-creates it.
    this._chats.delete(chatId);
    this._local.delete(chatId);
    this._typing.delete(chatId);
    this._unknown.delete(chatId);
    this._ignoredUntil.set(chatId, Date.now() + IGNORE_CHAT_MS);
    const out = this._typingOut.get(chatId);
    if (out) clearTimeout(out.timer);
    this._typingOut.delete(chatId);
    const t = this._readTimers.get(chatId);
    if (t) clearTimeout(t);
    this._readTimers.delete(chatId);
    this._deliveredPending.delete(chatId);
    this._deliveredSent.delete(chatId);
    this._readSent.delete(chatId);
    if (this.ui.activeChatId === chatId) {
      this.ui.activeChatId = null;
      this.emit('ui', { keys: ['activeChatId'] });
      this.emit('active_chat', { chatId: null, prev: chatId });
    }
    this.emit(`typing:${chatId}`, []);
    this.emit('chat_removed', { chat_id: chatId, title, kind });
    this._touchChats();
  }

  /** @param {number} chatId */
  _onChatRemoved(chatId) {
    if (this._chats.has(chatId)) this._dropChat(chatId);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Chats: merge, update, members                                                            */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Apply a Chat that came back in a `res` (SPEC 7.6(9) "res{chat}"): everything is replaced except
   * the max-merged fields, the counters go through the as-of rule, and `last_message` is kept when
   * the held one has a greater id. Idempotent. (`ev.chat_update` uses the same rules but always
   * replaces `last_message`; the store handles that event itself.)
   * @param {any} incoming a Chat
   */
  applyChat(incoming) {
    this._applyChat(incoming, 'response');
  }

  /**
   * @param {any} incoming a Chat
   * @param {'event'|'response'} source
   */
  _applyChat(incoming, source) {
    if (!incoming || typeof incoming.id !== 'number' || !incoming.me) return;
    if ((this._ignoredUntil.get(incoming.id) || 0) > Date.now()) this._ignoredUntil.delete(incoming.id);
    const old = this._chats.get(incoming.id);
    const L = this._loc(incoming.id);
    const merged = this._mergeChat(old, incoming, L, source);
    this._chats.set(merged.id, merged);
    this._seedLastMessage(merged);
    const w = L.win;
    if (w && merged.me.cleared_before_id > (old ? old.me.cleared_before_id : 0)) this._dropClearedFromWindow(merged.id, merged.me.cleared_before_id);
    const members = old ? this._memberSignature(old) !== this._memberSignature(merged) : false;
    this._recomputeStatuses(merged.id, members);
    this.emit(`chat:${merged.id}`, merged);
    this._touchChats();
    this._queueDeliveredFor(merged);
  }

  /**
   * @param {any} chat
   * @returns {string} signature of the receipt-relevant member set
   */
  _memberSignature(chat) {
    return (chat.members || []).map((m) => m.user_id).sort((a, b) => a - b).join(',');
  }

  /**
   * @param {any} old previous chat or undefined
   * @param {any} inc incoming chat
   * @param {ChatLocal} L
   * @param {'event'|'response'} source
   * @returns {any} merged chat
   */
  _mergeChat(old, inc, L, source) {
    // SPEC 7.6(9), exhaustive: only last_message_id, me.last_read_id, me.cleared_before_id and per
    // member delivered_up_to / read_up_to are max-merged; last_message is replaced by an event and
    // kept in a response when the held one has a greater id; the three counters go through the
    // as-of rule; every other field replaces the held value.
    const c = { ...inc };
    const incLast = inc.last_message_id === undefined ? null : inc.last_message_id;
    const oldLast = old && old.last_message_id !== undefined ? old.last_message_id : null;
    c.last_message_id = maxNullable(incLast, oldLast);
    if (source === 'response' && old && old.last_message && old.last_message.id > (inc.last_message ? inc.last_message.id : 0)) {
      c.last_message = old.last_message;
    }
    const oldMembers = new Map((old ? old.members || [] : []).map((m) => [m.user_id, m]));
    c.members = (inc.members || []).map((m) => {
      const o = oldMembers.get(m.user_id);
      return {
        ...m,
        read_up_to: Math.max(m.read_up_to || 0, o ? o.read_up_to || 0 : 0),
        delivered_up_to: Math.max(m.delivered_up_to || 0, o ? o.delivered_up_to || 0 : 0),
      };
    });
    c.pinned_messages = Array.isArray(inc.pinned_messages) ? inc.pinned_messages : [];
    c.pinned_message_ids = Array.isArray(inc.pinned_message_ids) ? inc.pinned_message_ids : [];
    const me = { ...inc.me };
    if (old) {
      me.last_read_id = Math.max(me.last_read_id || 0, old.me.last_read_id || 0);
      me.cleared_before_id = Math.max(me.cleared_before_id || 0, old.me.cleared_before_id || 0);
    }
    // As-of rule (SPEC 8.2): the snapshot is valid as of its last_message_id; add the countable
    // messages we received after it.
    const asOf = incLast === null ? 0 : incLast;
    const held = L.held.filter((e) => e.id > asOf && e.id > me.last_read_id);
    const heldMentions = held.filter((e) => e.mention);
    me.unread = Math.min(UNREAD_CAP, (inc.me.unread || 0) + held.length);
    me.unread_mentions = Math.min(UNREAD_CAP, (inc.me.unread_mentions || 0) + heldMentions.length);
    me.first_unread_mention_id = inc.me.first_unread_mention_id !== null && inc.me.first_unread_mention_id !== undefined
      ? inc.me.first_unread_mention_id
      : minHeldMention(heldMentions);
    L.held = held;
    c.me = me;
    return c;
  }

  /**
   * Drop window items hidden by `chat.clear` (SPEC 7.4).
   * @param {number} chatId
   * @param {number} upTo
   */
  _dropClearedFromWindow(chatId, upTo) {
    const L = this._loc(chatId);
    const w = L.win;
    if (!w) return;
    const removed = w.items.filter((m) => m.id <= upTo).map((m) => m.id);
    if (!removed.length) return;
    w.items = w.items.filter((m) => m.id > upTo);
    fixBounds(w);
    w.has_more_before = false;
    this.emit(`messages:${chatId}`, { type: 'remove', chatId, ids: removed });
  }

  /** @param {any} d ev.chat_members payload */
  _onChatMembers(d) {
    if (!d || typeof d.chat_id !== 'number') return;
    const chat = this._chats.get(d.chat_id);
    if (!chat) {
      this._queueForUnknown(d.chat_id, { t: 'members', d });
      return;
    }
    const removed = new Set(d.removed || []);
    let members = (chat.members || []).filter((m) => !removed.has(m.user_id));
    const upsert = (m) => {
      const i = members.findIndex((x) => x.user_id === m.user_id);
      if (i >= 0) {
        const o = members[i];
        const read = Math.max(m.read_up_to || 0, o.read_up_to || 0);
        members[i] = { ...o, ...m, read_up_to: read, delivered_up_to: Math.max(m.delivered_up_to || 0, o.delivered_up_to || 0, read) };
      } else {
        members.push({ ...m, delivered_up_to: Math.max(m.delivered_up_to || 0, m.read_up_to || 0) });
      }
    };
    for (const m of d.updated || []) upsert(m);
    for (const m of d.added || []) upsert(m);
    members = members.slice();
    const next = { ...chat, members };
    this._chats.set(next.id, next);
    this._recomputeStatuses(next.id, true);
    this.emit(`chat:${next.id}`, next);
    this._touchChats();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Status derivation                                                                        */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Recompute the status of own messages of a chat (window + last_message). `scratch` recomputes
   * from zero (R changed); otherwise ranks never drop within an epoch (SPEC 8.1).
   * @param {number} chatId
   * @param {boolean} [scratch=false]
   */
  _recomputeStatuses(chatId, scratch = false) {
    const chat = this._chats.get(chatId);
    if (!chat || !this.me) return;
    const meId = this.me.id;
    if (chat.kind === 'direct' && chat.peer_id === meId) return;
    const agg = statusAggregates(chat, meId, (id) => this._users.get(id));
    const changed = [];
    const apply = (m) => {
      if (m.sender_id !== meId || m.kind === 'system') return false;
      if (!scratch && m.status === 'read') return false; // ranks never drop outside a scratch recompute
      const s = statusFromAggregates(agg, m.id);
      const next = scratch ? s : maxStatus(m.status, s);
      if (next === m.status) return false;
      m.status = next;
      return true;
    };
    const L = this._local.get(chatId);
    if (L && L.win) {
      for (const m of L.win.items) if (apply(m)) changed.push(m.id);
    }
    const lastChanged = chat.last_message ? apply(chat.last_message) : false;
    if (changed.length) this.emit(`messages:${chatId}`, { type: 'update', chatId, ids: changed });
    if (lastChanged || changed.length) this.emit(`chat:${chatId}`, chat);
  }

  /** Recompute statuses in every chat that has the given user as a member. @param {number} userId */
  _recomputeForUser(userId) {
    for (const c of this._chats.values()) {
      if (this.memberOf(c, userId)) this._recomputeStatuses(c.id, true);
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Small events: me, workspace, users, presence                                             */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} d */
  _onMe(d) {
    if (!d || !d.me) return;
    const receiptsWereOff = !(this.me && this.me.read_receipts !== false);
    this.me = { ...this.me, ...d.me };
    // SPEC 8.2: after read_receipts is switched on the rule is re-evaluated for the open chat
    if (receiptsWereOff && this.me.read_receipts) this._scheduleRead(this.ui.activeChatId, 0);
    const pub = publicUser(d.me);
    const old = this._users.get(this.me.id);
    this._users.set(this.me.id, { ...old, ...pub });
    this.emit('me', this.me);
    this.emit(`user:${this.me.id}`, this._users.get(this.me.id));
    this.emit('users');
  }

  /** @param {any} d */
  _onWorkspace(d) {
    if (!d) return;
    this.workspace = { name: d.name || this.workspace.name, registration_open: Boolean(d.registration_open) };
    this.emit('workspace', this.workspace);
  }

  /** @param {any} user a public User */
  _onUserUpdate(user) {
    const old = this._users.get(user.id);
    const merged = { ...old, ...user };
    this._users.set(user.id, merged);
    if (this.me && user.id === this.me.id) {
      const receiptsWereOff = this.me.read_receipts === false;
      this.me = { ...this.me, ...publicUser(user) };
      if (receiptsWereOff && this.me.read_receipts) this._scheduleRead(this.ui.activeChatId, 0);
      this.emit('me', this.me);
    }
    if (old && (old.disabled !== merged.disabled || old.activated !== merged.activated || old.read_receipts !== merged.read_receipts)) {
      this._recomputeForUser(user.id);
    }
    this.emit(`user:${user.id}`, merged);
    this.emit('users');
    if (!old || old.display_name !== merged.display_name || old.disabled !== merged.disabled) {
      const dm = this.directChatWith(user.id);
      if (dm) {
        this.emit(`chat:${dm.id}`, dm);
        this._touchChats();
      }
    }
  }

  /** @param {any} d ev.presence */
  _onPresence(d) {
    if (!d || typeof d.user_id !== 'number') return;
    const old = this._users.get(d.user_id);
    if (!old) return;
    const merged = { ...old, online: Boolean(d.online), last_seen: d.last_seen === undefined ? null : d.last_seen };
    this._users.set(d.user_id, merged);
    if (!d.online) for (const chatId of Array.from(this._typing.keys())) this._removeTyping(chatId, d.user_id);
    this.emit('presence', { user_id: d.user_id });
    this.emit(`user:${d.user_id}`, merged);
    this.emit('users');
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Messages                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Reducer of ev.message (and of own responses): window, chat metadata, counters, receipts,
   * notifications. SPEC 9.1 "Store reducers", 8.2, 9.6.
   * @param {any} m the Message
   * @param {'event'|'response'} source
   */
  _onMessage(m, source) {
    if (!m || typeof m.id !== 'number' || typeof m.chat_id !== 'number' || !this.me) return;
    const chat = this._chats.get(m.chat_id);
    if (!chat) {
      this._queueForUnknown(m.chat_id, { t: 'message', m, source });
      return;
    }
    if ((this._ignoredUntil.get(chat.id) || 0) > Date.now()) return;
    const L = this._loc(chat.id);
    const meId = this.me.id;
    const own = m.sender_id === meId;
    if (L.inflight && source === 'event') {
      L.inflight.buffered.push({ t: 'message', m });
      L.inflight.newestEv = Math.max(L.inflight.newestEv, m.id);
    }
    m.status = own ? maxStatus(m.status, this.messageStatus(chat.id, m)) : null;

    // 1. window: items stay sorted strictly by id (SPEC 9.6 "Item order"); a held id is an upsert
    /** @type {null|'append'|'insert'} */
    let inserted = null;
    let updatedInPlace = false;
    const w = L.win;
    if (w) {
      const idx = findIndexById(w.items, m.id);
      if (idx >= 0) {
        w.items[idx] = this._mergeHeld(chat, w.items[idx], m);
        updatedInPlace = true;
      } else if (!w.has_more_after) {
        if (w.items.length === 0 || m.id > w.hi) {
          w.items.push(m);
          fixBounds(w);
          inserted = 'append';
        } else if (m.id > w.lo) {
          w.items.splice(insertionIndex(w.items, m.id), 0, m);
          inserted = 'insert';
        }
        // an id below the window's lo stays out: the window is one contiguous range
      }
    }

    // 2. chat metadata (SPEC 9.1 reducers + the as-of unread rule of 8.2)
    const prevLast = chat.last_message_id === null || chat.last_message_id === undefined ? 0 : chat.last_message_id;
    const next = { ...chat, me: { ...chat.me } };
    next.last_message_id = Math.max(prevLast, m.id);
    if (!chat.last_message || m.id > chat.last_message.id) next.last_message = m;
    else if (chat.last_message.id === m.id) next.last_message = m;
    const isJoined = m.kind === 'system' && m.system && m.system.event === 'joined';
    if (!isJoined) next.last_activity_at = Math.max(chat.last_activity_at || 0, m.created_at || 0);
    const countable = isCountable(m, meId);
    const mention = countable && mentionsMe(m, meId);
    if (source === 'event' && countable && m.id > prevLast) {
      next.me.unread = Math.min(UNREAD_CAP, (chat.me.unread || 0) + 1);
      L.held.push({ id: m.id, mention });
      if (mention) {
        next.me.unread_mentions = Math.min(UNREAD_CAP, (chat.me.unread_mentions || 0) + 1);
        const cur = next.me.first_unread_mention_id;
        next.me.first_unread_mention_id = cur === null || cur === undefined ? m.id : Math.min(cur, m.id);
      }
    }
    this._chats.set(chat.id, next);

    // 3. own message reconciliation BEFORE the window event (the outbox drops its bubble)
    if (own && m.client_id) this.emit('own_message', m);

    // 4. window events
    if (inserted) {
      this.emit(`messages:${chat.id}`, { type: inserted, chatId: chat.id, ids: [m.id] });
      this._trimWindowStart(chat.id, L);
      if (countable && this.ui.activeChatId === chat.id && (!this.ui.focused || !this.ui.visible) && L.openInfo && L.openInfo.dividerId === null) {
        L.openInfo.dividerId = m.id;
        this.emit(`messages:${chat.id}`, { type: 'divider', chatId: chat.id, ids: [m.id] });
      }
    } else if (updatedInPlace) {
      this.emit(`messages:${chat.id}`, { type: 'update', chatId: chat.id, ids: [m.id] });
    }
    this.emit(`chat:${chat.id}`, next);
    this._touchChats();

    if (source !== 'event') return;

    // 5. typing, receipts and notifications (synchronous, SPEC 9.4 / 9.6 rule 8)
    if (!own && m.sender_id !== null) this._removeTyping(chat.id, m.sender_id); // the typist's message ends the indicator
    if (!this.isSelfChat(next)) this._queueDelivered(chat.id); // value = Chat.last_message_id at flush time (SPEC 8.2)
    // A replayed or duplicated event (id not above what we already knew) never notifies twice.
    if (m.id <= prevLast) return;
    if (inserted) this._scheduleRead(chat.id, 0);
    const muted = this.isMuted(next);
    const activeFocused = this.ui.activeChatId === chat.id && this.ui.focused;
    const notifiable = countable && (!muted || mention) && !activeFocused;
    this.emit('incoming', { message: m, chat: next, notifiable, mention });
  }

  /**
   * Replace a held copy with an update, keeping our status rank (SPEC 8.1 authority of status).
   * @param {any} chat
   * @param {any} old held copy (may be undefined)
   * @param {any} incoming
   * @returns {any}
   */
  _mergeHeld(chat, old, incoming) {
    const merged = { ...incoming };
    if (this.me && incoming.sender_id === this.me.id) {
      merged.status = maxStatus(old ? old.status : null, maxStatus(incoming.status, this.messageStatus(chat.id, incoming)));
    } else {
      merged.status = null;
    }
    return merged;
  }

  /**
   * Trim the oldest items of the window above the cap, but only while the view sticks to the
   * bottom (SPEC 9.6).
   * @param {number} chatId
   * @param {ChatLocal} L
   */
  _trimWindowStart(chatId, L) {
    const w = L.win;
    if (!w || w.items.length <= WINDOW_CAP || !L.view.stuck) return;
    const removed = w.items.splice(0, w.items.length - WINDOW_CAP);
    w.has_more_before = true;
    fixBounds(w);
    this.emit(`messages:${chatId}`, { type: 'trim', chatId, side: 'start', ids: removed.map((m) => m.id) });
  }

  /** @param {any} m ev.message_update payload message */
  _onMessageUpdate(m) {
    if (!m || typeof m.id !== 'number' || typeof m.chat_id !== 'number' || !this.me) return;
    const chat = this._chats.get(m.chat_id);
    if (!chat) {
      this._queueForUnknown(m.chat_id, { t: 'update', m });
      return;
    }
    const L = this._loc(chat.id);
    if (L.inflight) L.inflight.buffered.push({ t: 'update', m });
    this._patchEverywhere(chat.id, m);
    this.emit('message_update', m);
  }

  /**
   * Patch every held copy of `m` (SPEC 7.6(9)): window, last_message, pinned_messages and every
   * held reply_to quoting it.
   * @param {number} chatId
   * @param {any} m
   */
  _patchEverywhere(chatId, m) {
    const chat = this._chats.get(chatId);
    if (!chat) return;
    const L = this._loc(chatId);
    const updated = [];
    const w = L.win;
    if (w) {
      const idx = findIndexById(w.items, m.id);
      if (idx >= 0) {
        w.items[idx] = this._mergeHeld(chat, w.items[idx], m);
        updated.push(m.id);
      }
      for (let i = 0; i < w.items.length; i += 1) {
        const q = w.items[i];
        if (q.reply_to && q.reply_to.id === m.id) {
          w.items[i] = { ...q, reply_to: this._patchedQuote(q.reply_to, m) };
          if (!updated.includes(q.id)) updated.push(q.id);
        }
      }
    }
    let chatChanged = false;
    let next = chat;
    if (chat.last_message && chat.last_message.id === m.id) {
      next = { ...next, last_message: this._mergeHeld(chat, chat.last_message, m) };
      chatChanged = true;
    }
    if (Array.isArray(chat.pinned_messages) && chat.pinned_messages.some((p) => p.id === m.id)) {
      next = { ...next, pinned_messages: chat.pinned_messages.map((p) => (p.id === m.id ? { ...m, status: null } : p)) };
      chatChanged = true;
    }
    if (chatChanged) {
      this._chats.set(chatId, next);
      this.emit(`chat:${chatId}`, next);
      this._touchChats();
    }
    if (updated.length) this.emit(`messages:${chatId}`, { type: 'update', chatId, ids: updated });
  }

  /**
   * New `reply_to` object after the quoted message changed (SPEC 7.2: body = first 200 code points).
   * @param {any} quote
   * @param {any} m the updated quoted message
   * @returns {any}
   */
  _patchedQuote(quote, m) {
    return {
      ...quote,
      kind: m.kind || quote.kind,
      body: m.deleted ? '' : cpSlice(m.body || '', 200),
      attachment_name: m.deleted ? null : (m.attachment ? m.attachment.name : quote.attachment_name),
      deleted: Boolean(m.deleted),
      unavailable: false,
    };
  }

  /** @param {any} d ev.message_removed */
  _onMessageRemoved(d) {
    if (!d || typeof d.chat_id !== 'number' || !Array.isArray(d.message_ids)) return;
    const chat = this._chats.get(d.chat_id);
    if (!chat) {
      this._queueForUnknown(d.chat_id, { t: 'removed', d });
      return;
    }
    const L = this._loc(chat.id);
    if (L.inflight) L.inflight.buffered.push({ t: 'removed', ids: d.message_ids });
    const ids = new Set(d.message_ids);
    const w = L.win;
    if (w) {
      const removed = w.items.filter((m) => ids.has(m.id)).map((m) => m.id);
      if (removed.length) {
        w.items = w.items.filter((m) => !ids.has(m.id));
        fixBounds(w);
        this.emit(`messages:${chat.id}`, { type: 'remove', chatId: chat.id, ids: removed });
      }
    }
    this.emit('message_removed', { chat_id: d.chat_id, message_ids: d.message_ids });
  }

  /**
   * Idempotent upsert of a message obtained from a response (as if it had arrived as ev.message).
   * @param {any} message
   */
  applyMessage(message) {
    if (message) this._onMessage(message, 'response');
  }

  /**
   * Patch a message copy held outside the store (Starred list, search results) the way the store
   * patches its own copies: content comes from `incoming`, own status never lowers.
   * @param {any} held
   * @param {any} incoming
   * @returns {any} `held`
   */
  patchMessage(held, incoming) {
    const keep = held.status;
    Object.assign(held, incoming);
    held.status = this.me && incoming.sender_id === this.me.id ? maxStatus(keep, incoming.status) : null;
    return held;
  }

  /**
   * Update every `reply_to` in `messages` that quotes `incoming` (deleted/body/unavailable).
   * @param {any[]} messages
   * @param {any} incoming the updated quoted message
   * @returns {number} number of quotes patched
   */
  patchQuotes(messages, incoming) {
    let n = 0;
    for (const q of messages) {
      if (q && q.reply_to && q.reply_to.id === incoming.id) {
        q.reply_to = this._patchedQuote(q.reply_to, incoming);
        n += 1;
      }
    }
    return n;
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Counters (SPEC 8.2)                                                                      */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} d Counters payload */
  _onReadSync(d) {
    if (!d || typeof d.chat_id !== 'number') return;
    if (!this._chats.has(d.chat_id)) {
      this._queueForUnknown(d.chat_id, { t: 'read_sync', d });
      return;
    }
    this.applyCounters(d);
  }

  /**
   * Apply a Counters payload with the as-of rule: unread = payload.unread + countable messages
   * received after payload.last_message_id.
   * @param {any} c `{chat_id, last_read_id, last_message_id, unread, unread_mentions, first_unread_mention_id}`
   */
  applyCounters(c) {
    if (!c || typeof c.chat_id !== 'number') return;
    const chat = this._chats.get(c.chat_id);
    if (!chat) return;
    const L = this._loc(chat.id);
    const me = { ...chat.me };
    me.last_read_id = Math.max(me.last_read_id || 0, c.last_read_id || 0);
    const asOf = c.last_message_id === null || c.last_message_id === undefined ? 0 : c.last_message_id;
    const held = L.held.filter((e) => e.id > asOf && e.id > me.last_read_id);
    const heldMentions = held.filter((e) => e.mention);
    me.unread = Math.min(UNREAD_CAP, (c.unread || 0) + held.length);
    me.unread_mentions = Math.min(UNREAD_CAP, (c.unread_mentions || 0) + heldMentions.length);
    me.first_unread_mention_id = c.first_unread_mention_id !== null && c.first_unread_mention_id !== undefined
      ? c.first_unread_mention_id
      : minHeldMention(heldMentions);
    L.held = held;
    const next = { ...chat, me, last_message_id: maxNullable(chat.last_message_id, c.last_message_id) };
    this._chats.set(chat.id, next);
    this.emit('read_sync', { chat_id: chat.id, counters: c });
    this.emit(`chat:${chat.id}`, next);
    this._touchChats();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Receipts (SPEC 8.2)                                                                      */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} d ev.receipt payload */
  _onReceipt(d) {
    if (!d || typeof d.chat_id !== 'number') return;
    const chat = this._chats.get(d.chat_id);
    if (!chat) return;
    const i = (chat.members || []).findIndex((m) => m.user_id === d.user_id);
    if (i < 0) return;
    const m = chat.members[i];
    const read = Math.max(m.read_up_to || 0, d.read_up_to || 0);
    const delivered = Math.max(m.delivered_up_to || 0, d.delivered_up_to || 0, read);
    if (read === m.read_up_to && delivered === m.delivered_up_to) return;
    const members = chat.members.slice();
    members[i] = { ...m, read_up_to: read, delivered_up_to: delivered };
    const next = { ...chat, members };
    this._chats.set(next.id, next);
    this._recomputeStatuses(next.id, false);
    this.emit(`chat:${next.id}`, next);
    this._touchChats();
  }

  /**
   * Queue `receipt.delivered` for a chat (batched with setTimeout 150 ms; never rAF, which does not
   * fire in hidden tabs). The value is `Chat.last_message_id` at flush time (store metadata, system
   * messages included, never derived from window content).
   * @param {number} chatId
   */
  _queueDelivered(chatId) {
    this._deliveredPending.add(chatId);
    if (this._deliveredTimer === null) this._deliveredTimer = setTimeout(() => this._flushDelivered(), 150);
  }

  /** @param {any} chat queue it; the flush decides whether anything is left to send */
  _queueDeliveredFor(chat) {
    if (!this.isSelfChat(chat)) this._queueDelivered(chat.id);
  }

  /** After ev.ready: one batched request for every chat that has undelivered messages. */
  _evaluateDeliveredAll() {
    for (const c of this._chats.values()) this._queueDeliveredFor(c);
  }

  /**
   * Send the pending delivered marks (<= 50 per request, one retry on not_member). A chat is sent
   * only when `Chat.last_message_id` is not null and greater than `members[me].delivered_up_to`.
   */
  async _flushDelivered() {
    this._deliveredTimer = null;
    const items = [];
    for (const chatId of this._deliveredPending) {
      const chat = this._chats.get(chatId);
      if (!chat || this.isSelfChat(chat)) continue;
      const upTo = chat.last_message_id;
      const mine = this.myMember(chat);
      if (upTo === null || upTo === undefined || upTo <= (mine ? mine.delivered_up_to : 0) || upTo <= (this._deliveredSent.get(chatId) || 0)) continue;
      items.push({ chat_id: chatId, up_to_id: upTo });
    }
    this._deliveredPending.clear();
    if (!items.length || !this._socket.isReady) return;
    for (let i = 0; i < items.length; i += 50) {
      await this._sendDelivered(items.slice(i, i + 50), true);
    }
  }

  /**
   * @param {Array<{chat_id: number, up_to_id: number}>} items
   * @param {boolean} retryOnce
   */
  async _sendDelivered(items, retryOnce) {
    if (!items.length) return;
    try {
      await this._socket.request('receipt.delivered', items.length === 1 ? items[0] : { items });
      for (const it of items) this._deliveredSent.set(it.chat_id, Math.max(this._deliveredSent.get(it.chat_id) || 0, it.up_to_id));
    } catch (err) {
      if (err && err.code === 'not_member' && retryOnce && typeof err.chat_id === 'number') {
        await this._sendDelivered(items.filter((it) => it.chat_id !== err.chat_id), false);
      } else if (err && err.code === 'rate_limited') {
        const wait = Math.max(1, Number(err.retry_after) || 1) * 1000;
        for (const it of items) this._queueDeliveredLater(it.chat_id, wait);
      }
    }
  }

  /** @param {number} chatId @param {number} ms */
  _queueDeliveredLater(chatId, ms) {
    setTimeout(() => this._queueDelivered(chatId), ms);
  }

  /**
   * Schedule the read-receipt evaluation of a chat.
   * @param {number|null} chatId
   * @param {number} delay ms (300 = SPEC 8.2 debounce, 0 = immediate)
   */
  _scheduleRead(chatId, delay = 300) {
    if (chatId === null || chatId === undefined) return;
    const prev = this._readTimers.get(chatId);
    if (prev) clearTimeout(prev);
    this._readTimers.set(chatId, setTimeout(() => {
      this._readTimers.delete(chatId);
      this._tryRead(chatId);
    }, delay));
  }

  /**
   * Re-evaluate the read rule of SPEC 8.2 for a chat now.
   * @param {number} chatId
   */
  requestRead(chatId) {
    this._scheduleRead(chatId, 0);
  }

  /**
   * Send `receipt.read` when ALL conditions hold: active chat, tab visible and focused, window holds
   * the newest message (`has_more_after` false) and the view sits within 40 px of the bottom.
   * @param {number} chatId
   */
  async _tryRead(chatId) {
    const chat = this._chats.get(chatId);
    const L = this._local.get(chatId);
    if (!chat || !L || !L.win || !this.me || this.isSelfChat(chat)) return;
    if (this.ui.activeChatId !== chatId || !this.ui.visible || !this.ui.focused) return;
    if (L.win.has_more_after || !L.view.stuck || !L.win.items.length) return;
    if (!this._socket.isReady) return;
    // SPEC 8.2: newest = window.hi, sent only when above the value the server holds for me: the public
    // read watermark while read receipts are on, else the private last_read_id (never resent unchanged)
    const upTo = L.win.hi;
    const mine = this.myMember(chat);
    const base = this.me.read_receipts ? (mine ? mine.read_up_to : 0) : chat.me.last_read_id || 0;
    if (upTo <= base || upTo <= (this._readSent.get(chatId) || 0)) return;
    this._readSent.set(chatId, upTo);
    try {
      await this.request('receipt.read', { chat_id: chatId, up_to_id: upTo });
    } catch (err) {
      this._readSent.delete(chatId);
      if (err && err.code === 'rate_limited') {
        setTimeout(() => this._scheduleRead(chatId, 0), Math.max(1, Number(err.retry_after) || 1) * 1000);
      }
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Message window (SPEC 9.6)                                                                */
  /* ---------------------------------------------------------------------------------------- */

  /** @returns {number|null} */
  get activeChatId() {
    return this.ui.activeChatId;
  }

  /**
   * @param {number} chatId
   * @returns {any} the OpenInfo of the open chat
   */
  getOpenInfo(chatId) {
    const L = this._local.get(chatId);
    return L && L.openInfo ? L.openInfo : null;
  }

  /**
   * @param {number} chatId
   * @returns {{lo: number, hi: number, has_more_before: boolean, has_more_after: boolean, seen_hi: number, items: any[]}|null}
   */
  getWindow(chatId) {
    const L = this._local.get(chatId);
    return L && L.win ? L.win : null;
  }

  /**
   * @param {number} chatId
   * @param {number} id
   * @returns {any}
   */
  getMessage(chatId, id) {
    const w = this.getWindow(chatId);
    if (!w) return undefined;
    const i = findIndexById(w.items, id);
    return i >= 0 ? w.items[i] : undefined;
  }

  /**
   * Value for msg.send `seen_up_to_id` (SPEC 7.4, 9.6): `win.seen_hi`, only when the window ends at
   * the newest message and `seen_hi > 0`; otherwise undefined (omit the field).
   * @param {number} chatId
   * @returns {number|undefined}
   */
  seenUpTo(chatId) {
    const w = this.getWindow(chatId);
    return w && !w.has_more_after && w.seen_hi > 0 ? w.seen_hi : undefined;
  }

  /**
   * The scroll / IntersectionObserver handler reports a message whose bubble is at least 50 % inside
   * the scroll viewport: `win.seen_hi` only ever rises, and only while the tab is visible and
   * focused (SPEC 9.6). `window.hi` is never a substitute: loaded but never scrolled into view
   * is not "seen". Reset to 0 whenever the window is replaced or the chat is opened.
   * @param {number} chatId
   * @param {number} messageId id of a held server message
   */
  markSeen(chatId, messageId) {
    const w = this.getWindow(chatId);
    if (!w || !this.ui.visible || !this.ui.focused) return;
    if (!Number.isInteger(messageId) || messageId <= w.seen_hi || findIndexById(w.items, messageId) < 0) return;
    w.seen_hi = messageId;
  }

  /**
   * The conversation view reports scroll state: `stuck` = within 40 px of the bottom, `anchorId` =
   * id of the message anchored at the top of the viewport.
   * @param {number} chatId
   * @param {{stuck?: boolean, anchorId?: number|null}} vs
   */
  setViewState(chatId, vs) {
    const L = this._loc(chatId);
    const wasStuck = L.view.stuck;
    if (typeof vs.stuck === 'boolean') L.view.stuck = vs.stuck;
    if (vs.anchorId !== undefined) L.view.anchorId = vs.anchorId;
    if (L.view.stuck && !wasStuck) this._scheduleRead(chatId, 300);
  }

  /**
   * Open a chat: set it active and load its window (SPEC 9.6 "Opening a chat").
   * @param {number} chatId
   * @returns {Promise<{chatId: number, lastRead: number, unreadAtOpen: number, dividerId: number|null, scroll: 'bottom'|'divider'}>}
   */
  async openChat(chatId) {
    const chat = this._chats.get(chatId);
    if (!chat) throw new SocketError('not_member', 'You are not a member of this chat.');
    const prev = this.ui.activeChatId;
    if (prev === chatId) {
      const L0 = this._loc(chatId);
      if (L0.win && L0.openInfo) return L0.openInfo;
    } else if (prev !== null) {
      this.closeChat(prev);
    }
    this.ui.activeChatId = chatId;
    this.emit('ui', { keys: ['activeChatId'] });
    this.emit('active_chat', { chatId, prev });
    const L = this._loc(chatId);
    L.view = { stuck: true, anchorId: null };
    const lastRead = chat.me.last_read_id || 0;
    const unread = chat.me.unread || 0;
    const info = { chatId, lastRead, unreadAtOpen: unread, dividerId: /** @type {number|null} */ (null), scroll: /** @type {'bottom'|'divider'} */ ('bottom') };
    L.openInfo = info;
    if (unread === 0) {
      await this._loadReplace(chatId, {});
    } else {
      await this._loadReplace(chatId, { after_id: lastRead, limit: PAGE });
      const w = L.win;
      if (w && w.has_more_before) {
        try {
          await this._extendOlder(chatId, 20);
        } catch (_) {
          /* a failed context page is ignored (SPEC 9.6) */
        }
      }
      const win = L.win;
      if (win) {
        const first = win.items.find((m) => m.id > lastRead && m.sender_id !== this.me.id && m.kind !== 'system' && !m.deleted);
        if (first) {
          info.dividerId = first.id;
          info.scroll = 'divider';
        }
      }
    }
    return info;
  }

  /**
   * Close the open chat: drop its window, stop typing, clear the active chat.
   * @param {number} chatId
   */
  closeChat(chatId) {
    if (this.ui.activeChatId !== chatId) return;
    this.emitTyping(chatId, 'stop');
    const L = this._local.get(chatId);
    if (L) {
      L.win = null;
      L.gen += 1;
      L.histSeq += 1;
      L.inflight = null;
      L.openInfo = null;
      L.loadingBefore = false;
      L.loadingAfter = false;
      L.view = { stuck: true, anchorId: null };
    }
    const t = this._readTimers.get(chatId);
    if (t) clearTimeout(t);
    this._readTimers.delete(chatId);
    this.ui.activeChatId = null;
    this.emit('ui', { keys: ['activeChatId'] });
    this.emit('active_chat', { chatId: null, prev: chatId });
  }

  /**
   * Replace the window with a history page, with the buffering procedure of SPEC 9.6.
   * @param {number} chatId
   * @param {Record<string, any>} params chat.history cursor params
   * @param {{followup?: boolean}} [opts]
   * @returns {Promise<void>}
   */
  async _loadReplace(chatId, params, opts = {}) {
    const { followup = true } = opts;
    const L = this._loc(chatId);
    L.histSeq += 1;
    const inflight = { seq: L.histSeq, buffered: /** @type {any[]} */ ([]), newestEv: 0 };
    L.inflight = inflight;
    let res;
    try {
      res = await this._socket.request('chat.history', { chat_id: chatId, limit: PAGE, ...params });
    } catch (err) {
      if (L.inflight === inflight) L.inflight = null;
      throw err;
    }
    const chat = this._chats.get(chatId);
    if (this._local.get(chatId) !== L || L.inflight !== inflight || !chat) throw new SocketError('cancelled', 'Superseded');
    L.inflight = null;
    const items = Array.isArray(res.messages) ? res.messages : [];
    const w = { lo: 0, hi: 0, has_more_before: Boolean(res.has_more_before), has_more_after: Boolean(res.has_more_after), seen_hi: 0, items };
    for (const m of items) this._normalizeLoaded(chat, m);
    fixBounds(w);
    L.gen += 1;
    L.win = w;
    // (2) replay what arrived while the request was in flight
    for (const ev of inflight.buffered) this._replayBuffered(w, ev);
    this._dropClearedSilently(chat, w); // ANY history response: ids <= the CURRENT cleared_before_id go
    fixBounds(w);
    // (4) own messages already in the page reconcile the outbox
    for (const m of w.items) if (this.me && m.sender_id === this.me.id && m.client_id) this.emit('own_message', m);
    this._recomputeStatuses(chatId, false);
    this.emit(`messages:${chatId}`, { type: 'reset', chatId, ids: [] });
    this._scheduleRead(chatId, 300);
    // (3) catch-up: `newest_known` uses VISIBLE ids only (Chat.last_message / events), never the raw
    // Chat.last_message_id, which may belong to a message this viewer cannot see. At most ONE extra
    // request; its response never runs this step again.
    const latest = this._chats.get(chatId);
    const newestKnown = Math.max(latest && latest.last_message ? latest.last_message.id : 0, inflight.newestEv);
    if (followup && latest && !w.has_more_after && newestKnown > (w.hi || 0)) {
      try {
        await this._extendNewer(chatId);
      } catch (_) {
        /* the live events keep the window current; a failed catch-up is not fatal */
      }
    }
  }

  /**
   * Remove window items with id <= the chat's CURRENT cleared_before_id without emitting (the
   * caller emits the event of the load that triggered it).
   * @param {any} chat
   * @param {{items: any[], has_more_before: boolean}} w
   * @returns {boolean} whether something was removed
   */
  _dropClearedSilently(chat, w) {
    const cleared = chat && chat.me ? chat.me.cleared_before_id || 0 : 0;
    if (!cleared || !w.items.some((m) => m.id <= cleared)) return false;
    w.items = w.items.filter((m) => m.id > cleared);
    w.has_more_before = false;
    return true;
  }

  /**
   * @param {any} chat
   * @param {any} m a message loaded from history: seed own status, clear foreign status
   */
  _normalizeLoaded(chat, m) {
    m.status = this.me && m.sender_id === this.me.id ? maxStatus(m.status, this.messageStatus(chat.id, m)) : null;
  }

  /**
   * Replay a buffered event onto a freshly loaded window.
   * @param {{lo:number, hi:number, has_more_before:boolean, has_more_after:boolean, items:any[]}} w
   * @param {any} ev
   */
  _replayBuffered(w, ev) {
    if (ev.t === 'message') {
      const m = ev.m;
      if (!w.has_more_after && m.id > (w.items.length ? w.items[w.items.length - 1].id : 0)) w.items.push(m);
    } else if (ev.t === 'update') {
      const i = findIndexById(w.items, ev.m.id);
      if (i >= 0) {
        const chat = this._chats.get(ev.m.chat_id);
        w.items[i] = this._mergeHeld(chat, w.items[i], ev.m);
      }
      for (let k = 0; k < w.items.length; k += 1) {
        const q = w.items[k];
        if (q.reply_to && q.reply_to.id === ev.m.id) w.items[k] = { ...q, reply_to: this._patchedQuote(q.reply_to, ev.m) };
      }
    } else if (ev.t === 'removed') {
      const ids = new Set(ev.ids);
      w.items = w.items.filter((m) => !ids.has(m.id));
    }
  }

  /**
   * Prepend one older page (`before_id = lo`).
   * @param {number} chatId
   * @param {number} [limit=50]
   * @returns {Promise<number>} number of messages added
   */
  async _extendOlder(chatId, limit = PAGE) {
    const L = this._loc(chatId);
    const w = L.win;
    if (!w || !w.has_more_before || L.loadingBefore) return 0;
    L.loadingBefore = true;
    const gen = L.gen;
    try {
      const res = await this._socket.request('chat.history', { chat_id: chatId, before_id: w.lo, limit });
      const chat = this._chats.get(chatId);
      if (this._local.get(chatId) !== L || L.gen !== gen || L.win !== w || !chat) return 0;
      const cleared = chat.me.cleared_before_id || 0;
      const incoming = (Array.isArray(res.messages) ? res.messages : []).filter((m) => m.id < w.lo && m.id > cleared);
      w.has_more_before = Boolean(res.has_more_before) && !(cleared && (Array.isArray(res.messages) ? res.messages : []).some((m) => m.id <= cleared));
      if (incoming.length) {
        for (const m of incoming) this._normalizeLoaded(chat, m);
        w.items = incoming.concat(w.items);
        fixBounds(w);
        for (const m of incoming) if (this.me && m.sender_id === this.me.id && m.client_id) this.emit('own_message', m);
        this.emit(`messages:${chatId}`, { type: 'prepend', chatId, ids: incoming.map((m) => m.id) });
        if (w.items.length > WINDOW_CAP) {
          const removed = w.items.splice(WINDOW_CAP);
          w.has_more_after = true;
          fixBounds(w);
          this.emit(`messages:${chatId}`, { type: 'trim', chatId, side: 'end', ids: removed.map((m) => m.id) });
        }
      }
      return incoming.length;
    } finally {
      L.loadingBefore = false;
    }
  }

  /**
   * Append one newer page (`after_id = hi`).
   * @param {number} chatId
   * @returns {Promise<number>} number of messages added
   */
  async _extendNewer(chatId) {
    const L = this._loc(chatId);
    const w = L.win;
    if (!w || L.loadingAfter) return 0;
    L.loadingAfter = true;
    const gen = L.gen;
    try {
      const res = await this._socket.request('chat.history', { chat_id: chatId, after_id: w.hi, limit: PAGE });
      const chat = this._chats.get(chatId);
      if (this._local.get(chatId) !== L || L.gen !== gen || L.win !== w || !chat) return 0;
      const incoming = (Array.isArray(res.messages) ? res.messages : []).filter((m) => m.id > w.hi && m.id > (chat.me.cleared_before_id || 0));
      w.has_more_after = Boolean(res.has_more_after);
      if (incoming.length) {
        for (const m of incoming) this._normalizeLoaded(chat, m);
        w.items = w.items.concat(incoming);
        fixBounds(w);
        for (const m of incoming) if (this.me && m.sender_id === this.me.id && m.client_id) this.emit('own_message', m);
        this.emit(`messages:${chatId}`, { type: 'append', chatId, ids: incoming.map((m) => m.id) });
        if (w.items.length > WINDOW_CAP) {
          const removed = w.items.splice(0, w.items.length - WINDOW_CAP);
          w.has_more_before = true;
          fixBounds(w);
          this.emit(`messages:${chatId}`, { type: 'trim', chatId, side: 'start', ids: removed.map((m) => m.id) });
        }
        this._scheduleRead(chatId, 300);
      }
      return incoming.length;
    } finally {
      L.loadingAfter = false;
    }
  }

  /**
   * Load one older page (before_id = lo).
   * @param {number} chatId
   * @returns {Promise<number>} messages added (0 when none/already loading)
   */
  loadOlder(chatId) {
    return this._extendOlder(chatId);
  }

  /**
   * Load one newer page (after_id = hi); only meaningful while `has_more_after`.
   * @param {number} chatId
   * @returns {Promise<number>}
   */
  loadNewer(chatId) {
    const w = this.getWindow(chatId);
    if (!w || !w.has_more_after) return Promise.resolve(0);
    return this._extendNewer(chatId);
  }

  /**
   * Make sure message `messageId` is in the window (chat.history around_id). Rejects
   * SocketError('not_found') when it is gone.
   * @param {number} chatId
   * @param {number} messageId
   * @returns {Promise<void>}
   */
  async jumpTo(chatId, messageId) {
    if (this.ui.activeChatId !== chatId) throw new SocketError('invalid_state', 'That chat is not open');
    if (this.getMessage(chatId, messageId)) return;
    await this._loadReplace(chatId, { around_id: messageId, limit: 100 });
  }

  /**
   * Make the window end at the newest message (the "scroll to bottom" FAB, sending while
   * `has_more_after`).
   * @param {number} chatId
   * @returns {Promise<void>}
   */
  async jumpToLatest(chatId) {
    const w = this.getWindow(chatId);
    if (!w || !w.has_more_after) return;
    await this._loadReplace(chatId, {});
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Typing (SPEC 8.3)                                                                        */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} d ev.typing */
  _onTyping(d) {
    if (!d || typeof d.chat_id !== 'number' || typeof d.user_id !== 'number') return;
    if (this.me && d.user_id === this.me.id) return;
    if (!this._chats.has(d.chat_id)) {
      this._queueForUnknown(d.chat_id, { t: 'typing', d });
      return;
    }
    let m = this._typing.get(d.chat_id);
    if (d.state === 'stop') {
      this._removeTyping(d.chat_id, d.user_id);
      return;
    }
    if (d.state !== 'typing' && d.state !== 'recording') return;
    if (!m) {
      m = new Map();
      this._typing.set(d.chat_id, m);
    }
    const had = m.get(d.user_id);
    m.set(d.user_id, { state: d.state, until: Date.now() + TYPING_TTL_MS });
    if (!had || had.state !== d.state) this.emit(`typing:${d.chat_id}`, this.typingUsers(d.chat_id));
    this._ensureTypingSweeper();
  }

  /**
   * Drop the typing entry of `(chat, user)` and tell the views.
   * @param {number} chatId
   * @param {number} userId
   */
  _removeTyping(chatId, userId) {
    const m = this._typing.get(chatId);
    if (m && m.delete(userId)) {
      if (m.size === 0) this._typing.delete(chatId);
      this.emit(`typing:${chatId}`, this.typingUsers(chatId));
    }
  }

  _ensureTypingSweeper() {
    if (this._typingTimer !== null) return;
    this._typingTimer = setInterval(() => {
      const now = Date.now();
      for (const [chatId, m] of this._typing) {
        let changed = false;
        for (const [userId, e] of m) {
          if (e.until <= now) {
            m.delete(userId);
            changed = true;
          }
        }
        if (m.size === 0) this._typing.delete(chatId);
        if (changed) this.emit(`typing:${chatId}`, this.typingUsers(chatId));
      }
      if (this._typing.size === 0 && this._typingTimer !== null) {
        clearInterval(this._typingTimer);
        this._typingTimer = null;
      }
    }, 1000);
  }

  /**
   * @param {number} chatId
   * @returns {Array<{user_id: number, state: 'typing'|'recording'}>}
   */
  typingUsers(chatId) {
    const m = this._typing.get(chatId);
    if (!m) return [];
    return Array.from(m, ([user_id, e]) => ({ user_id, state: /** @type {'typing'|'recording'} */ (e.state) }));
  }

  /**
   * Indicator text per SPEC 8.3.
   * @param {number} chatId
   * @returns {{text: string, state: 'typing'|'recording'}|null}
   */
  typingLabel(chatId) {
    const users = this.typingUsers(chatId);
    const chat = this._chats.get(chatId);
    if (!users.length || !chat) return null;
    const rec = users.every((u) => u.state === 'recording');
    const verb = rec ? 'recording audio…' : 'typing…';
    if (chat.kind === 'direct') return { text: verb, state: rec ? 'recording' : 'typing' };
    const names = users.map((u) => this.userName(u.user_id));
    let text;
    if (names.length === 1) text = `${names[0]} is ${verb}`;
    else if (names.length === 2) text = `${names[0]} and ${names[1]} are ${verb}`;
    else text = `${names.length} people are ${verb}`;
    return { text, state: rec ? 'recording' : 'typing' };
  }

  /**
   * Tell the others that I am typing/recording/stopped. Throttled to one frame per 2.5 s; an
   * automatic `stop` follows 5 s after the last call (SPEC 8.3).
   * @param {number} chatId
   * @param {'typing'|'recording'|'stop'} state
   */
  emitTyping(chatId, state) {
    const chat = this._chats.get(chatId);
    if (!chat || this.isSelfChat(chat) || !this.canPost(chat).ok) return;
    let o = this._typingOut.get(chatId);
    if (!o) {
      o = { state: 'stop', at: 0, timer: null };
      this._typingOut.set(chatId, o);
    }
    if (o.timer) clearTimeout(o.timer);
    o.timer = null;
    const now = Date.now();
    if (state === 'stop') {
      if (o.state !== 'stop') this._socket.send('typing', { chat_id: chatId, state: 'stop' });
      o.state = 'stop';
      return;
    }
    if (o.state !== state || now - o.at >= TYPING_OUT_REFRESH_MS) {
      if (this._socket.send('typing', { chat_id: chatId, state })) {
        o.state = state;
        o.at = now;
      }
    }
    o.timer = setTimeout(() => this.emitTyping(chatId, 'stop'), TYPING_OUT_IDLE_MS);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Drafts (SPEC 9.7)                                                                        */
  /* ---------------------------------------------------------------------------------------- */

  _loadDrafts() {
    this._drafts.clear();
    for (const name of storage.keys('draft:')) {
      const id = Number(name.slice(6));
      const d = storage.get(name, null);
      if (Number.isInteger(id) && d && typeof d.text === 'string' && this._chats.has(id)) this._drafts.set(id, d);
    }
    this.emit('drafts');
  }

  /**
   * @param {number} chatId
   * @returns {{text: string, reply_to_id?: number, edit_id?: number}|null}
   */
  getDraft(chatId) {
    return this._drafts.get(chatId) || null;
  }

  /** @returns {number[]} ids of chats with a draft */
  draftChatIds() {
    return Array.from(this._drafts.keys()).filter((id) => this._chats.has(id));
  }

  /**
   * Store (or clear, with null / empty content) the draft of a chat; persisted debounced.
   * @param {number} chatId
   * @param {{text?: string, reply_to_id?: number|null, edit_id?: number|null}|null} draft
   */
  setDraft(chatId, draft) {
    const text = draft && typeof draft.text === 'string' ? draft.text : '';
    const empty = !draft || (text.trim() === '' && !draft.reply_to_id && !draft.edit_id);
    const key = `draft:${chatId}`;
    if (empty) {
      if (!this._drafts.has(chatId)) return;
      this._drafts.delete(chatId);
    } else {
      /** @type {any} */
      const d = { text };
      if (draft && draft.reply_to_id) d.reply_to_id = draft.reply_to_id;
      if (draft && draft.edit_id) d.edit_id = draft.edit_id;
      this._drafts.set(chatId, d);
    }
    const t = this._draftTimers.get(chatId);
    if (t) clearTimeout(t);
    this._draftTimers.set(chatId, setTimeout(() => {
      this._draftTimers.delete(chatId);
      const cur = this._drafts.get(chatId);
      if (cur) storage.set(key, cur);
      else storage.remove(key);
    }, 400));
    this.emit(`draft:${chatId}`, this._drafts.get(chatId) || null);
    this.emit('drafts');
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Requests with automatic state application                                                */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * socket.request + apply the response to the store (messages, chats, counters, profile).
   * @param {string} type a SPEC 7.4 request name
   * @param {object} [d]
   * @param {{timeout?: number}} [opts]
   * @returns {Promise<any>}
   */
  async request(type, d = {}, opts = {}) {
    const res = await this._socket.request(type, d, opts);
    try {
      this._applyResponse(type, res);
    } catch (err) {
      console.error('[store] applying the response of ' + type + ' failed', err);
    }
    return res;
  }

  /**
   * @param {string} type
   * @param {any} res
   */
  _applyResponse(type, res) {
    if (!res) return;
    if (APPLY_MESSAGE_TYPES.has(type) && res.message) this.applyMessage(res.message);
    else if (type === 'msg.forward' && Array.isArray(res.messages)) for (const m of res.messages) this.applyMessage(m);
    else if ((type.startsWith('chat.') && type !== 'chat.history' && res.chat) || (type === 'msg.pin' && res.chat)) this.applyChat(res.chat);
    else if (type === 'receipt.read') this.applyCounters(res);
    else if (type === 'profile.update' && res.me) this._onMe({ me: res.me });
  }
}

export const store = new Store();
export const prefs = store.prefs;
