/**
 * core/notify.js - in-page and OS notifications, tab title / favicon badge, title blink, the
 * "N new messages in M chats" summary on resume and the multi-tab leader election (SPEC 9.4).
 *
 * Everything reacts synchronously to store events (no animation-frame scheduling): `incoming`
 * (chime, toast, OS notification), `badge` (title + favicon), `read_sync` (close the OS
 * notification of that chat). `new Notification()` is only used in a secure context with granted
 * permission and always inside try/catch.
 *
 * Exported API (docs/ui-core-api.md section 11): notify.start, status, requestPermission,
 * isSupported, closeFor; class Notifier (tests).
 */

import { store as defaultStore } from './store.js';
import { sound as defaultSound } from './sound.js';
import { router as defaultRouter } from './router.js';
import { toast as defaultToast } from './ui.js';
import { supportsDesktopNotifications, isTouchDevice, randomHex, pluralize } from './util.js';

const BLINK_MS = 1000;
const BLINK_TEXT = '\u{1F4AC} New message';
const ICON_SIZE = 64;

export class Notifier {
  /**
   * @param {{store?: any, sound?: any, router?: any, toast?: Function}} [deps]
   */
  constructor(deps = {}) {
    this._store = deps.store || defaultStore;
    this._sound = deps.sound || defaultSound;
    this._router = deps.router || defaultRouter;
    this._toast = deps.toast || defaultToast;
    this._started = false;
    this._tabId = randomHex(4);
    /** @type {string|null} */
    this._leaderId = null;
    this._leaderTs = 0;
    /** @type {any} */
    this._channel = null;
    /** @type {Map<number, number>} messages received while the tab was hidden, per chat */
    this._missed = new Map();
    /** @type {Map<number, Notification>} */
    this._notifications = new Map();
    this._blinkTimer = /** @type {any} */ (null);
    this._blinkOn = false;
    this._baseTitle = '';
    this._iconHref = /** @type {string|null} */ (null);
    this._lastBadgeKey = '';
    this._wasVisible = true;
  }

  /** Subscribe to the store and the page (main.js, once). */
  start() {
    if (this._started) return;
    this._started = true;
    const s = this._store;
    s.on('incoming', (e) => this._onIncoming(e));
    s.on('badge', () => this._updateBadge());
    s.on('workspace', () => this._updateBadge());
    s.on('ready', () => this._updateBadge());
    s.on('reset', () => this._onReset());
    s.on('read_sync', (e) => {
      if (e && e.counters && e.counters.unread === 0) {
        this.closeFor(e.chat_id);
        this._missed.delete(e.chat_id);
      }
    });
    s.on('ui', (e) => {
      if (e && e.keys && (e.keys.includes('visible') || e.keys.includes('focused'))) this._onVisibilityOrFocus();
    });
    this._wasVisible = s.ui.visible;
    this._setupChannel();
    if (s.ui.focused && s.ui.visible) this._announceFocus();
    this._updateBadge();
  }

  /** @returns {boolean} OS notifications can be used at all (secure context + API) */
  isSupported() {
    return supportsDesktopNotifications();
  }

  /**
   * Status for Settings -> Notifications (SPEC 9.4 mitigation 3).
   * @returns {{state: 'enabled'|'blocked'|'default'|'unavailable', text: string, why: string, note: string}}
   */
  status() {
    const note = isTouchDevice() ? 'Alerts only work while DeskTalk is open on screen.' : '';
    if (typeof window === 'undefined' || !('Notification' in window)) {
      return {
        state: 'unavailable',
        text: 'Desktop notifications: not available in this browser',
        why: 'This browser does not support desktop notifications. In-page alerts, sounds and the tab badge still work.',
        note,
      };
    }
    if (!window.isSecureContext) {
      return {
        state: 'unavailable',
        text: 'Desktop notifications: not available on this connection (http)',
        why: 'Browsers only allow desktop notifications on secure connections (https, or localhost on the server PC). '
          + 'Ask your admin to enable HTTPS, or to allow this address through the browser policy OverrideSecurityRestrictionsOnInsecureOrigin. '
          + 'In-page alerts, sounds and the tab badge keep working while DeskTalk is open.',
        note,
      };
    }
    const perm = Notification.permission;
    if (perm === 'granted') return { state: 'enabled', text: 'Desktop notifications: enabled', why: '', note };
    if (perm === 'denied') {
      return {
        state: 'blocked',
        text: 'Desktop notifications: blocked in browser settings',
        why: 'Notifications were blocked for this site. Change the permission in the browser\'s site settings (the icon next to the address).',
        note,
      };
    }
    return { state: 'default', text: 'Desktop notifications: not enabled yet', why: 'Click "Enable" to let your browser ask for permission.', note };
  }

  /**
   * Ask the browser for notification permission. Must be called from a user click.
   * @returns {Promise<'enabled'|'blocked'|'default'|'unavailable'>}
   */
  async requestPermission() {
    if (!this.isSupported()) return this.status().state;
    try {
      await new Promise((resolve) => {
        const r = Notification.requestPermission(resolve);
        if (r && typeof r.then === 'function') r.then(resolve, resolve);
      });
    } catch (_) {
      /* the status below reflects whatever happened */
    }
    return this.status().state;
  }

  /**
   * Close the OS notification of a chat.
   * @param {number} chatId
   */
  closeFor(chatId) {
    const n = this._notifications.get(chatId);
    if (!n) return;
    this._notifications.delete(chatId);
    try {
      n.close();
    } catch (_) {
      /* ignore */
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Incoming messages                                                                        */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * @param {{message: any, chat: any, notifiable: boolean, mention: boolean}} e
   */
  _onIncoming(e) {
    if (!e || !e.notifiable) return;
    const { message, chat, mention } = e;
    const ui = this._store.ui;
    if (!ui.visible) this._missed.set(chat.id, (this._missed.get(chat.id) || 0) + 1);
    if (this._isLeader()) {
      this._sound.chime({ chatId: chat.id, mention });
      if (ui.focused && ui.visible) this._inPageToast(message, chat);
      else this._desktopNotification(message, chat);
    }
    this._syncBlink();
  }

  /** Is this tab the one that should chime / notify? */
  _isLeader() {
    return !this._channel || this._leaderId === null || this._leaderId === this._tabId;
  }

  /**
   * @param {any} message
   * @param {any} chat
   */
  _inPageToast(message, chat) {
    const s = this._store;
    const sender = s.userName(message.sender_id);
    const title = chat.kind === 'group' ? `${sender} in ${s.chatTitle(chat)}` : sender;
    const body = s.prefs.get('previews') ? s.summarize(message) : 'New message';
    this._toast(`${title}: ${body}`, { type: 'info', key: `msg-${chat.id}`, onClick: () => this._router.openChat(chat.id) });
  }

  /**
   * @param {any} message
   * @param {any} chat
   */
  _desktopNotification(message, chat) {
    const s = this._store;
    if (!this.isSupported() || Notification.permission !== 'granted' || !s.prefs.get('desktopNotifications')) return;
    try {
      const sender = s.userName(message.sender_id);
      const title = chat.kind === 'group' ? `${sender} (${s.chatTitle(chat)})` : sender;
      const body = s.prefs.get('previews') ? s.summarize(message) : 'New message';
      this.closeFor(chat.id);
      const n = new Notification(title, { body, tag: `chat-${chat.id}`, silent: true });
      n.onclick = () => {
        try {
          window.focus();
        } catch (_) {
          /* ignore */
        }
        this._router.openChat(chat.id);
        n.close();
      };
      n.onclose = () => {
        if (this._notifications.get(chat.id) === n) this._notifications.delete(chat.id);
      };
      this._notifications.set(chat.id, n);
    } catch (_) {
      /* Notification constructor can throw on some platforms (Android Chrome) */
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Visibility, focus, leader election                                                       */
  /* ---------------------------------------------------------------------------------------- */

  _onVisibilityOrFocus() {
    const ui = this._store.ui;
    if (ui.focused && ui.visible) this._announceFocus();
    if (ui.visible && !this._wasVisible) this._showResumeSummary();
    this._wasVisible = ui.visible;
    this._syncBlink();
  }

  /** One summary toast when the tab becomes visible again (SPEC 9.4 mitigation 4). */
  _showResumeSummary() {
    if (this._missed.size === 0) return;
    let total = 0;
    for (const n of this._missed.values()) total += n;
    const chats = this._missed.size;
    const first = this._missed.keys().next().value;
    this._missed.clear();
    this._toast(`${total} new ${pluralize(total, 'message')} in ${chats} ${pluralize(chats, 'chat')}`, {
      type: 'info',
      key: 'resume-summary',
      onClick: () => (chats === 1 ? this._router.openChat(first) : this._router.home()),
    });
    try {
      if (typeof navigator.vibrate === 'function') navigator.vibrate(200);
    } catch (_) {
      /* not supported */
    }
  }

  _setupChannel() {
    if (typeof BroadcastChannel !== 'function') return;
    try {
      this._channel = new BroadcastChannel('desktalk');
      this._channel.onmessage = (ev) => {
        const m = ev && ev.data;
        if (!m || typeof m !== 'object') return;
        if (m.t === 'focus' && typeof m.ts === 'number' && m.ts >= this._leaderTs) {
          this._leaderId = m.id;
          this._leaderTs = m.ts;
        } else if (m.t === 'bye' && m.id === this._leaderId) {
          this._leaderId = null;
          this._leaderTs = 0;
        }
      };
      window.addEventListener('pagehide', () => {
        try {
          this._channel.postMessage({ t: 'bye', id: this._tabId });
        } catch (_) {
          /* ignore */
        }
      });
    } catch (_) {
      this._channel = null;
    }
  }

  /** This tab became the most recently focused one. */
  _announceFocus() {
    this._leaderId = this._tabId;
    this._leaderTs = Date.now();
    if (!this._channel) return;
    try {
      this._channel.postMessage({ t: 'focus', id: this._tabId, ts: this._leaderTs });
    } catch (_) {
      /* ignore */
    }
  }

  _onReset() {
    this._missed.clear();
    for (const id of Array.from(this._notifications.keys())) this.closeFor(id);
    this._updateBadge();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Title, favicon, blink                                                                    */
  /* ---------------------------------------------------------------------------------------- */

  _updateBadge() {
    if (typeof document === 'undefined') return;
    const { count, display } = this._store.totalUnread();
    const name = (this._store.workspace && this._store.workspace.name) || 'DeskTalk';
    this._baseTitle = count > 0 ? `(${display}) ${name}` : name;
    const key = `${count > 0 ? display : ''}`;
    if (!this._blinkOn) document.title = this._baseTitle;
    if (key !== this._lastBadgeKey) {
      this._lastBadgeKey = key;
      this._setFavicon(count > 0 ? display : '');
    }
    this._syncBlink();
  }

  /** Blink the title while the tab is hidden and something is unread. */
  _syncBlink() {
    if (typeof document === 'undefined') return;
    const want = document.visibilityState === 'hidden' && this._store.totalUnread().count > 0;
    if (want && this._blinkTimer === null) {
      this._blinkTimer = setInterval(() => {
        this._blinkOn = !this._blinkOn;
        document.title = this._blinkOn ? BLINK_TEXT : this._baseTitle;
      }, BLINK_MS);
    } else if (!want && this._blinkTimer !== null) {
      clearInterval(this._blinkTimer);
      this._blinkTimer = null;
      this._blinkOn = false;
      document.title = this._baseTitle;
    }
  }

  /**
   * Draw the favicon (canvas, data: URL - allowed by the CSP) with an optional red badge.
   * @param {string} label badge text, empty for none
   */
  _setFavicon(label) {
    try {
      const link = this._iconLink();
      if (!link) return;
      if (this._iconHref === null) this._iconHref = link.getAttribute('href') || '';
      if (!label) {
        if (this._iconHref) link.setAttribute('href', this._iconHref);
        return;
      }
      const canvas = document.createElement('canvas');
      canvas.width = ICON_SIZE;
      canvas.height = ICON_SIZE;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const r = 14;
      ctx.fillStyle = '#0f766e';
      ctx.beginPath();
      ctx.moveTo(r, 4);
      ctx.arcTo(60, 4, 60, 60, r);
      ctx.arcTo(60, 60, 4, 60, r);
      ctx.arcTo(4, 60, 4, 4, r);
      ctx.arcTo(4, 4, 60, 4, r);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.beginPath();
      ctx.ellipse(32, 29, 18, 14, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(18, 38);
      ctx.lineTo(14, 50);
      ctx.lineTo(28, 41);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = '#dc2626';
      ctx.beginPath();
      ctx.arc(46, 18, 17, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.font = `bold ${label.length > 2 ? 17 : 22}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(label, 46, 19);
      link.setAttribute('href', canvas.toDataURL('image/png'));
    } catch (_) {
      /* the favicon is cosmetic */
    }
  }

  /** @returns {HTMLLinkElement|null} the page's <link rel=icon> (created when missing) */
  _iconLink() {
    let link = /** @type {HTMLLinkElement|null} */ (document.querySelector('link[rel~="icon"]'));
    if (!link && document.head) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    return link;
  }
}

export const notify = new Notifier();
