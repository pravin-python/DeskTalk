/**
 * core/outbox.js - unsent messages (SPEC 7.1.1 retry taxonomy, 8.5, 9.7).
 *
 * Items live in memory and (text items only) in namespaced localStorage. Per chat the outbox keeps
 * strict FIFO with at most ONE `msg.send` in flight; an item waiting for its upload blocks the
 * ones behind it, a failed item does not. Retryable errors (socket loss, timeout, rate_limited,
 * server_busy, server_error x3) keep the item queued; every other code makes it `failed`.
 * `client_id` is reused for every retry, so the server dedupes (idempotent).
 *
 * Exported API (docs/ui-core-api.md section 8): outbox.add, items, get, all, count, hasPending,
 * retry, discard, update (for upload.js), start, reset; class Outbox (tests).
 * Events go through the store: `outbox:<chatId>` {type:'add'|'update'|'remove', item} and `outbox`.
 */

import { store as defaultStore } from './store.js';
import { socket as defaultSocket, isTransientError } from './socket.js';
import { storage, newClientId, serverNow, errorText } from './util.js';
import { toast as defaultToast } from './ui.js';
import { sound as defaultSound } from './sound.js';

const MAX_SERVER_ERROR_RETRIES = 3;
const PERSIST_DELAY_MS = 60;
const PROGRESS_EMIT_MS = 100;

/**
 * @typedef {object} OutboxItem
 * @property {string} client_id
 * @property {number} chat_id
 * @property {number} owner_id
 * @property {string} body
 * @property {number|null} reply_to_id
 * @property {number} created_at serverNow() when queued
 * @property {'queued'|'uploading'|'sending'|'failed'} state
 * @property {{code: string, message: string}|null} error
 * @property {number} progress 0..1 while uploading
 * @property {null|{name: string, size: number, mime: string, kind: string, width: number|null, height: number|null, duration: number|null, url: string|null}} attachment
 * @property {string|null} attachment_id set when the upload finished
 * @property {number} tries transmissions so far
 * @property {number} retryAt epoch ms before which the item is not retried
 * @property {any} upload upload job (attachment items) or null
 */

export class Outbox {
  /**
   * @param {{store?: any, socket?: any, toast?: Function, sound?: any}} [deps]
   */
  constructor(deps = {}) {
    this._store = deps.store || defaultStore;
    this._socket = deps.socket || defaultSocket;
    this._toast = deps.toast || defaultToast;
    this._sound = deps.sound || defaultSound;
    /** @type {Map<string, OutboxItem>} */
    this._byId = new Map();
    /** @type {Map<number, OutboxItem[]>} */
    this._chats = new Map();
    /** @type {Set<number>} chats with a msg.send in flight */
    this._inflight = new Set();
    /** @type {Map<number, any>} */
    this._timers = new Map();
    this._started = false;
    this._loaded = false;
    this._persistTimer = /** @type {any} */ (null);
    this._lastProgressEmit = 0;
  }

  /** Subscribe to the store (main.js, once). */
  start() {
    if (this._started) return;
    this._started = true;
    this._store.on('ready', () => this._onReady());
    this._store.on('own_message', (m) => this._onOwnMessage(m));
    this._store.on('chat_removed', (e) => this._failChat(e.chat_id));
    // The store handles ev.ready BEFORE the socket flips to 'ready', so the resend has to be
    // triggered by the state change (the store's own 'ready' event only restores and validates).
    this._socket.on('state', (state) => {
      if (state === 'ready') this._pumpAll();
    });
  }

  /** Try to send the head of every chat's queue. */
  _pumpAll() {
    for (const chatId of Array.from(this._chats.keys())) this._pump(chatId);
  }

  /**
   * Forget everything (logout / kicked / 401). `wipe` also removes the persisted copy.
   * @param {{wipe?: boolean}} [opts]
   */
  reset(opts = {}) {
    for (const t of this._timers.values()) clearTimeout(t);
    if (this._persistTimer) clearTimeout(this._persistTimer);
    this._persistTimer = null;
    this._timers.clear();
    for (const item of this._byId.values()) {
      if (item.upload && typeof item.upload.cancel === 'function') item.upload.cancel();
      this._revokePreview(item);
    }
    this._byId.clear();
    this._chats.clear();
    this._inflight.clear();
    this._loaded = false;
    if (opts.wipe) storage.remove('outbox');
    this._store.emit('outbox');
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Queries                                                                                  */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Items of a chat in send order (a copy).
   * @param {number} chatId
   * @returns {OutboxItem[]}
   */
  items(chatId) {
    const list = this._chats.get(chatId);
    return list ? list.slice() : [];
  }

  /** @param {string} clientId @returns {OutboxItem|undefined} */
  get(clientId) {
    return this._byId.get(clientId);
  }

  /** @returns {OutboxItem[]} every item */
  all() {
    return Array.from(this._byId.values());
  }

  /** @returns {number} number of items (queued, uploading, sending and failed) */
  count() {
    return this._byId.size;
  }

  /** @returns {boolean} true when something would be lost by wiping the outbox */
  hasPending() {
    return this._byId.size > 0;
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Adding, retrying, discarding                                                             */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Queue a message. `attachment_promise` (from upload.js) makes the item wait in state
   * `uploading` until the upload resolved to an attachment object.
   * @param {{chat_id: number, body?: string, reply_to_id?: number|null, client_id?: string,
   *          attachment_promise?: Promise<any>, preview?: any, upload?: any}} spec
   * @returns {OutboxItem}
   */
  add(spec) {
    const existing = spec.client_id ? this._byId.get(spec.client_id) : undefined;
    if (existing) return existing;
    const me = this._store.me;
    /** @type {OutboxItem} */
    const item = {
      client_id: spec.client_id || newClientId(),
      chat_id: spec.chat_id,
      owner_id: me ? me.id : 0,
      body: spec.body || '',
      reply_to_id: spec.reply_to_id || null,
      created_at: serverNow(),
      state: spec.attachment_promise ? 'uploading' : 'queued',
      error: null,
      progress: 0,
      attachment: spec.preview || null,
      attachment_id: null,
      tries: 0,
      retryAt: 0,
      upload: spec.upload || null,
    };
    // Defined but non-enumerable bookkeeping for the server-error retry cap.
    Object.defineProperty(item, '_serverErrors', { value: 0, writable: true, enumerable: false });
    this._byId.set(item.client_id, item);
    let list = this._chats.get(item.chat_id);
    if (!list) {
      list = [];
      this._chats.set(item.chat_id, list);
    }
    list.push(item);
    const w = this._store.getWindow(item.chat_id);
    if (w && w.has_more_after) this._store.jumpToLatest(item.chat_id).catch(() => {});
    this._emit('add', item);
    this._persist();
    if (spec.attachment_promise) this._watchUpload(item, spec.attachment_promise);
    this._pump(item.chat_id);
    return item;
  }

  /**
   * Failed -> queued again (attachment items restart their upload).
   * @param {string} clientId
   */
  retry(clientId) {
    const item = this._byId.get(clientId);
    if (!item || item.state !== 'failed') return;
    item.error = null;
    item.tries = 0;
    item.retryAt = 0;
    /** @type {any} */ (item)._serverErrors = 0;
    if (item.upload && !item.attachment_id && typeof item.upload.restart === 'function') {
      item.state = 'uploading';
      item.progress = 0;
      this._watchUpload(item, item.upload.restart());
    } else {
      item.state = 'queued';
    }
    this._emit('update', item);
    this._persist();
    this._pump(item.chat_id);
  }

  /**
   * Remove an item (cancels a running upload). Items behind it are released.
   * @param {string} clientId
   */
  discard(clientId) {
    const item = this._byId.get(clientId);
    if (!item) return;
    if (item.upload && typeof item.upload.cancel === 'function' && !item.attachment_id) item.upload.cancel();
    this._remove(item);
    this._pump(item.chat_id);
  }

  /**
   * Update display fields of an item (used by upload.js): `progress` (0..1) and `attachment`
   * (merged). Progress events are throttled.
   * @param {string} clientId
   * @param {{progress?: number, attachment?: object}} patch
   */
  update(clientId, patch) {
    const item = this._byId.get(clientId);
    if (!item) return;
    if (patch.attachment) item.attachment = /** @type {any} */ ({ ...item.attachment, ...patch.attachment });
    if (typeof patch.progress === 'number') {
      const done = patch.progress >= 1;
      item.progress = patch.progress;
      const now = Date.now();
      if (!done && !patch.attachment && now - this._lastProgressEmit < PROGRESS_EMIT_MS) return;
      this._lastProgressEmit = now;
    }
    this._emit('update', item);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Sending                                                                                  */
  /* ---------------------------------------------------------------------------------------- */

  /** After every ev.ready: restore (first time), validate, resend (SPEC 8.5). */
  _onReady() {
    if (!this._loaded) {
      this._loaded = true;
      this._restore();
    }
    for (const item of this._byId.values()) if (item.state === 'sending') item.state = 'queued';
    this._inflight.clear();
    const me = this._store.me;
    for (const item of this.all()) {
      if (!this._store.getChat(item.chat_id) || (me && item.owner_id !== me.id)) this._remove(item);
    }
    this._pumpAll();
  }

  /** @param {any} m an own message that arrived through the store */
  _onOwnMessage(m) {
    if (!m || !m.client_id) return;
    const item = this._byId.get(m.client_id);
    if (!item || item.chat_id !== m.chat_id) return;
    this._remove(item);
    this._pump(item.chat_id);
  }

  /**
   * The chat is gone (ev.chat_removed): its queued items become `failed` with reason `not_member`
   * (SPEC 9.6); a running upload is cancelled but its item stays as a failed one.
   * @param {number} chatId
   */
  _failChat(chatId) {
    const list = this._chats.get(chatId);
    if (!list) return;
    for (const item of list) {
      if (item.state === 'failed') continue;
      const uploading = item.state === 'uploading';
      item.state = 'failed';
      item.error = { code: 'not_member', message: errorText({ code: 'not_member' }) };
      if (uploading && item.upload && !item.attachment_id && typeof item.upload.cancel === 'function') item.upload.cancel();
      this._emit('update', item);
    }
    this._persist();
  }

  /**
   * Start the next transmission of a chat if the rules allow it.
   * @param {number} chatId
   */
  _pump(chatId) {
    if (!this._store.isReady || !this._socket.isReady || this._inflight.has(chatId)) return;
    const list = this._chats.get(chatId);
    if (!list) return;
    for (const item of list) {
      if (item.state === 'failed') continue;
      if (item.state === 'uploading' || item.state === 'sending') return;
      const wait = item.retryAt - Date.now();
      if (wait > 0) {
        this._scheduleRetry(chatId, wait);
        return;
      }
      this._transmit(item);
      return;
    }
  }

  /** @param {number} chatId @param {number} ms */
  _scheduleRetry(chatId, ms) {
    const prev = this._timers.get(chatId);
    if (prev) clearTimeout(prev);
    this._timers.set(chatId, setTimeout(() => {
      this._timers.delete(chatId);
      this._pump(chatId);
    }, ms + 5));
  }

  /**
   * Send one item with `msg.send`.
   * @param {OutboxItem} item
   */
  _transmit(item) {
    const chatId = item.chat_id;
    item.state = 'sending';
    item.tries += 1;
    this._inflight.add(chatId);
    this._emit('update', item);
    /** @type {Record<string, any>} */
    const d = { chat_id: chatId, client_id: item.client_id, body: item.body };
    if (item.attachment_id) d.attachment_id = item.attachment_id;
    if (item.reply_to_id) d.reply_to_id = item.reply_to_id;
    const seen = this._store.seenUpTo(chatId);
    if (seen !== undefined) d.seen_up_to_id = seen;
    this._socket.request('msg.send', d).then(
      (res) => {
        this._inflight.delete(chatId);
        if (res && res.message) this._store.applyMessage(res.message);
        if (this._byId.has(item.client_id)) this._remove(item);
        if (this._store.prefs.get('sentSound')) this._sound.sent();
        this._pump(chatId);
      },
      (err) => {
        this._inflight.delete(chatId);
        if (!this._byId.has(item.client_id)) {
          this._pump(chatId);
          return;
        }
        this._onSendError(item, err);
        this._pump(chatId);
      },
    );
  }

  /**
   * Classify a failed msg.send (SPEC 7.1.1).
   * @param {OutboxItem} item
   * @param {any} err
   */
  _onSendError(item, err) {
    if (!isTransientError(err)) {
      this._fail(item, err);
      return;
    }
    item.state = 'queued';
    const code = err.code;
    if (code === 'timeout') {
      item.retryAt = Date.now() + Math.min(8000, 1000 * 2 ** Math.min(3, item.tries - 1));
    } else if (code === 'rate_limited' || code === 'server_busy') {
      item.retryAt = Date.now() + Math.max(1, Number(err.retry_after) || 2) * 1000;
    } else if (code === 'server_error') {
      const n = /** @type {any} */ (item)._serverErrors + 1;
      /** @type {any} */ (item)._serverErrors = n;
      if (n > MAX_SERVER_ERROR_RETRIES) {
        this._fail(item, err);
        return;
      }
      item.retryAt = Date.now() + 1000 * 2 ** (n - 1);
    } else {
      item.retryAt = 0; // offline / connection_lost: wait for the next ev.ready
    }
    this._emit('update', item);
  }

  /**
   * @param {OutboxItem} item
   * @param {any} err
   */
  _fail(item, err) {
    item.state = 'failed';
    const own = err && err.name === 'UploadError' && err.message; // upload errors already carry the end-user text
    item.error = { code: (err && err.code) || 'error', message: own || errorText(err, 'Could not send the message.') };
    this._emit('update', item);
    this._persist();
  }

  /**
   * Follow an upload promise: queued on success, failed (or removed when cancelled) otherwise.
   * @param {OutboxItem} item
   * @param {Promise<any>} promise resolves to the attachment object of POST /api/upload
   */
  _watchUpload(item, promise) {
    promise.then(
      (att) => {
        if (!this._byId.has(item.client_id)) return;
        item.attachment_id = att && att.id ? att.id : null;
        if (att) {
          item.attachment = /** @type {any} */ ({
            ...item.attachment,
            name: att.name, size: att.size, mime: att.mime, kind: att.kind,
            width: att.width ?? null, height: att.height ?? null, duration: att.duration ?? null,
          });
        }
        item.progress = 1;
        item.state = 'queued';
        this._emit('update', item);
        this._persist();
        this._pump(item.chat_id);
      },
      (err) => {
        if (!this._byId.has(item.client_id)) return;
        if (err && err.code === 'cancelled') {
          if (item.state !== 'failed') this._remove(item); // a cancelled upload of a failed item (chat removed) stays failed
        } else {
          this._fail(item, err);
        }
        this._pump(item.chat_id);
      },
    );
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Bookkeeping                                                                              */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * @param {OutboxItem} item
   */
  _remove(item) {
    if (!this._byId.delete(item.client_id)) return;
    const list = this._chats.get(item.chat_id);
    if (list) {
      const i = list.indexOf(item);
      if (i >= 0) list.splice(i, 1);
      if (list.length === 0) this._chats.delete(item.chat_id);
    }
    this._revokePreview(item);
    this._emit('remove', item);
    this._persist();
  }

  /** @param {OutboxItem} item */
  _revokePreview(item) {
    const url = item.attachment && item.attachment.url;
    if (url && typeof url === 'string' && url.startsWith('blob:')) {
      try {
        URL.revokeObjectURL(url);
      } catch (_) {
        /* ignore */
      }
    }
  }

  /**
   * @param {'add'|'update'|'remove'} type
   * @param {OutboxItem} item
   */
  _emit(type, item) {
    this._store.emit(`outbox:${item.chat_id}`, { type, item });
    this._store.emit('outbox');
  }

  _persist() {
    if (this._persistTimer) return;
    this._persistTimer = setTimeout(() => {
      this._persistTimer = null;
      this._persistNow();
    }, PERSIST_DELAY_MS);
  }

  /** Write the text items (attachment items are never persisted; only counted). */
  _persistNow() {
    if (!storage.ready) return;
    const items = [];
    let att = 0;
    for (const item of this._byId.values()) {
      if (item.upload) {
        att += 1;
        continue;
      }
      items.push({
        client_id: item.client_id, chat_id: item.chat_id, owner_id: item.owner_id, body: item.body,
        reply_to_id: item.reply_to_id, created_at: item.created_at,
        failed: item.state === 'failed', error: item.error,
      });
    }
    if (items.length === 0 && att === 0) storage.remove('outbox');
    else storage.set('outbox', { v: 1, items, att });
  }

  /** Restore the persisted outbox of the current user (first ev.ready of a page load). */
  _restore() {
    const saved = storage.get('outbox', null);
    if (!saved || saved.v !== 1 || !Array.isArray(saved.items)) {
      if (saved) storage.remove('outbox');
      return;
    }
    const me = this._store.me;
    for (const it of saved.items) {
      if (!it || typeof it.client_id !== 'string' || typeof it.chat_id !== 'number' || typeof it.body !== 'string') continue;
      if (!me || it.owner_id !== me.id || !this._store.getChat(it.chat_id) || this._byId.has(it.client_id)) continue;
      /** @type {OutboxItem} */
      const item = {
        client_id: it.client_id, chat_id: it.chat_id, owner_id: it.owner_id, body: it.body,
        reply_to_id: it.reply_to_id || null, created_at: it.created_at || serverNow(),
        state: it.failed ? 'failed' : 'queued', error: it.failed ? it.error || { code: 'error', message: 'Could not send the message.' } : null,
        progress: 0, attachment: null, attachment_id: null, tries: 0, retryAt: 0, upload: null,
      };
      Object.defineProperty(item, '_serverErrors', { value: 0, writable: true, enumerable: false });
      this._byId.set(item.client_id, item);
      let list = this._chats.get(item.chat_id);
      if (!list) {
        list = [];
        this._chats.set(item.chat_id, list);
      }
      list.push(item);
      this._emit('add', item);
    }
    if (saved.att > 0) {
      const n = Number(saved.att);
      this._toast(`${n} unsent ${n === 1 ? 'attachment was' : 'attachments were'} discarded`, { type: 'error', key: 'outbox-discarded' });
    }
    this._persistNow();
  }
}

export const outbox = new Outbox();
