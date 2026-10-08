/**
 * views/conversation.js - the Conversation screen (SPEC 9.2, 9.6): header, pinned banner, in-chat search,
 * the message list with its window model and scroll rules, the unread divider, the scroll-to-bottom button,
 * drag-and-drop, the composer and the info drawer.
 *
 * The list is reconciled from `store.getWindow(chatId)` (one contiguous id range): rows are created for new
 * ids, removed for dropped ones, date separators / run flags are recomputed, and the scroll position is kept
 * through an ANCHOR row (the top-most visible message): prepends, trims, removals and image loads never move
 * what the user is looking at (SPEC 9.6 rules 2 and 4). `stuck` (<= 40 px from the bottom) is only recomputed
 * on scroll events and is what pins appended messages / resized content to the bottom (rules 1, 3, 4, 7).
 * The view reports `stuck`, the anchor id and the "seen up to" id to the store (read receipts, window trimming,
 * the post-reconnect reload, `seen_up_to_id`).
 *
 * Exported: mount(container, ctx) per the core view contract; returns { update(route), unmount() }.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { outbox } from '../core/outbox.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { createSearcher } from '../core/api.js';
import { chatAvatar } from '../core/avatar.js';
import { formatLastSeen, errorText, prefersReducedMotion, pluralize } from '../core/util.js';
import {
  createRow, patchRow, disposeRow, createDateSeparator, createUnreadDivider, unreadLabel, isSameRun, setRunContinuation,
  bindMessageActions, pendingToMessage,
} from './messageview.js';
import { openMessageMenu, openPendingMenu, reactTo, afterMenuClose } from './messagemenu.js';
import { openLightbox, lightboxItemFromMessage } from './lightbox.js';
import { createComposer } from './composer.js';
import { createInfoDrawer } from './infodrawer.js';

const STICK_PX = 40;
const LOAD_MORE_PX = 300;
const FAB_PX = 200;
const FLASH_MS = 1500;
const EAGER_COUNT = 20;
const DIVIDER_TOP_RATIO = 0.25;

/**
 * @param {number} ts epoch seconds
 * @returns {string} local calendar day key
 */
function dayKey(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/**
 * Count the messages the divider label refers to (SPEC 9.6: non-own, non-system, from the divider on).
 * @param {any[]} items
 * @param {number} fromId
 * @returns {number}
 */
function countUnreadFrom(items, fromId) {
  const meId = store.me ? store.me.id : -1;
  let n = 0;
  for (const m of items) if (m.id >= fromId && m.sender_id !== meId && m.kind !== 'system' && !m.deleted) n += 1;
  return n;
}

class ConversationView {
  /**
   * @param {HTMLElement} container
   * @param {any} ctx
   */
  constructor(container, ctx) {
    this.container = container;
    this.ctx = ctx;
    this.chatId = 0;
    this.token = 0;
    this.destroyed = false;
    this.ready = false;
    /** @type {any} */
    this.info = null;
    /** @type {Array<() => void>} */
    this.offs = [];
    /** @type {Map<number, HTMLElement>} */
    this.rows = new Map();
    /** @type {Map<number, HTMLElement>} */
    this.seps = new Map();
    /** @type {Map<string, HTMLElement>} */
    this.pendingRows = new Map();
    /** @type {HTMLElement|null} */
    this.dividerEl = null;
    this.dividerCount = 0;
    this.openNewest = 0;
    this.stuck = true;
    /** @type {{id: number, offset: number}|null} */
    this.anchor = null;
    this.reported = { stuck: true, anchorId: /** @type {number|null} */ (null) };
    this.loadingOlder = false;
    this.loadingNewer = false;
    this.retryAt = 0;
    this.suppressLoad = false;
    this.progPending = false;
    this.progTop = 0;
    this.pendingCenter = 0;
    this.pendingBottom = false;
    this.pinIndex = 0;
    this.eagerFrom = 0;
    this.highlight = '';
    this.dragDepth = 0;
    /** @type {any} */
    this.drawer = null;
    /** @type {any} */
    this.drawerLayer = null;
    /** @type {any} */
    this.search = null;
    this.timers = new Set();
    this.viewEnv = {
      context: /** @type {'list'} */ ('list'),
      getChat: () => store.getChat(this.chatId),
      isEager: (id) => id >= this.eagerFrom,
      highlight: '',
    };
  }

  /* -------------------------------------------------------------------------------------- */
  /* Lifecycle                                                                              */
  /* -------------------------------------------------------------------------------------- */

  /** @param {any} route */
  start(route) {
    this.buildShell();
    this.composer = createComposer(this.composerHost, { chatId: route.chatId });
    this.shellOffs = [
      store.on('ui', () => this.updateSeen()),
      store.on('connection', () => this.paintStatus()),
    ];
    const vv = window.visualViewport;
    if (vv) {
      const onVv = () => {
        if (this.ready && this.stuck) this.scrollToBottom();
      };
      vv.addEventListener('resize', onVv);
      this.shellOffs.push(() => vv.removeEventListener('resize', onVv));
    }
    this.resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => this.onResize()) : null;
    if (this.resizeObserver) {
      this.resizeObserver.observe(this.list);
      this.resizeObserver.observe(this.scroller);
    }
    this.openRoute(route);
  }

  /** @param {any} route */
  update(route) {
    if (this.destroyed) return;
    if (route.chatId !== this.chatId) this.openRoute(route);
    else if (route.messageId) this.jumpTo(route.messageId);
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    this.token += 1;
    this.teardownChat();
    for (const off of this.shellOffs || []) off();
    if (this.resizeObserver) this.resizeObserver.disconnect();
    for (const t of this.timers) clearTimeout(t);
    this.timers.clear();
    this.closeSearch(false);
    this.closeDrawer();
    if (this.composer) this.composer.destroy();
    if (this.unbindActions) this.unbindActions();
    if (this.unbindActionsOutbox) this.unbindActionsOutbox();
    for (const row of this.rows.values()) disposeRow(row);
    for (const row of this.pendingRows.values()) disposeRow(row);
    store.closeChat(this.chatId);
    this.container.replaceChildren();
  }

  /**
   * Drop the listeners and DOM state of the current chat.
   */
  teardownChat() {
    for (const off of this.offs.splice(0)) off();
    for (const row of this.rows.values()) disposeRow(row);
    for (const row of this.pendingRows.values()) disposeRow(row);
    this.rows.clear();
    this.seps.clear();
    this.pendingRows.clear();
    this.dividerEl = null;
    if (this.msgs) this.msgs.replaceChildren();
    if (this.outboxEl) this.outboxEl.replaceChildren();
    this.ready = false;
    this.info = null;
  }

  /**
   * Switch to the chat of `route` (the shell stays; the per-chat state is rebuilt).
   * @param {any} route
   */
  async openRoute(route) {
    const chatId = route.chatId;
    const chat = store.getChat(chatId);
    this.token += 1;
    const token = this.token;
    if (this.chatId && this.chatId !== chatId) store.closeChat(this.chatId);
    this.teardownChat();
    this.closeSearch(false);
    this.closeDrawer();
    this.chatId = chatId;
    this.stuck = true;
    this.anchor = null;
    this.reported = { stuck: true, anchorId: null };
    this.loadingOlder = false;
    this.loadingNewer = false;
    this.pinIndex = 0;
    this.highlight = '';
    this.viewEnv.highlight = '';
    this.pendingCenter = 0;
    this.pendingBottom = false;
    this.retryAt = 0;
    if (!chat) {
      this.showEmpty('This chat is not available.');
      return;
    }
    this.scroller.setAttribute('aria-label', `Messages in ${store.chatTitle(chat)}`);
    this.paintHeader();
    this.showLoading(true);
    this.setEmpty(false);
    this.composer.setChat(chatId);
    this.offs.push(
      store.on(`messages:${chatId}`, (ev) => this.onMessagesEvent(ev)),
      store.on(`chat:${chatId}`, () => this.onChat()),
      store.on(`outbox:${chatId}`, (ev) => this.onOutbox(ev)),
      store.on(`typing:${chatId}`, () => this.paintStatus()),
      store.on('users', () => this.paintStatus()),
    );
    try {
      const info = await store.openChat(chatId);
      if (token !== this.token || this.destroyed) return;
      this.info = info;
      this.ready = true;
      this.showLoading(false);
      this.renderAll({ first: true, messageId: route.messageId });
    } catch (err) {
      if (token !== this.token || this.destroyed || (err && err.code === 'cancelled')) return;
      this.showLoading(false);
      if (err && err.code === 'not_member') {
        router.home({ replace: true });
        return;
      }
      this.showEmpty(errorText(err, 'Could not open this chat.'));
      ui.toast(errorText(err, 'Could not open this chat.'), { type: 'error', key: 'open-chat' });
    }
  }

  /* -------------------------------------------------------------------------------------- */
  /* Shell DOM                                                                              */
  /* -------------------------------------------------------------------------------------- */

  buildShell() {
    this.titleEl = h('h2.conv-title.truncate', { dir: 'auto' });
    this.statusEl = h('p.conv-status.truncate', { 'aria-live': 'polite', dir: 'auto' });
    this.avatarSlot = h('span.conv-avatar');
    this.titleBtn = h('button.conv-title-btn', { type: 'button', 'aria-label': 'Chat info', onClick: () => this.toggleDrawer() },
      this.avatarSlot, h('span.conv-title-main', this.titleEl, this.statusEl));
    this.searchBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'Search in chat', title: 'Search in chat', onClick: () => this.openSearch() }, icon('search'));
    this.infoBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'Chat info', title: 'Chat info', onClick: () => this.toggleDrawer() }, icon('info'));
    this.moreBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'More options', title: 'More options', onClick: () => this.openHeaderMenu() }, icon('more'));
    const header = h('header.pane-header.conv-header',
      h('button.btn-icon.conv-back.only-narrow', { type: 'button', 'aria-label': 'Back to chats', onClick: () => router.up() }, icon('back')),
      this.titleBtn, h('span.spacer'), this.searchBtn, this.infoBtn, this.moreBtn);

    this.pinnedBar = h('button.conv-pinned', { type: 'button', hidden: true, onClick: () => this.onPinnedClick() });
    this.searchBar = this.buildSearchBar();

    this.topEl = h('div.conv-top', { 'aria-hidden': 'true' });
    this.msgs = h('div.conv-msgs');
    this.outboxEl = h('div.conv-outbox');
    this.emptyEl = h('div.conv-empty', { hidden: true }, h('p.muted', 'No messages yet. Say hello!'));
    this.list = h('div.conv-list', this.topEl, this.msgs, this.outboxEl, this.emptyEl);
    this.scroller = h('div.conv-scroll.scroll-y', {
      role: 'log', 'aria-live': 'polite', 'aria-relevant': 'additions', 'aria-label': 'Messages', tabIndex: -1,
      onScroll: () => this.onScroll(),
      onFocusin: (e) => this.onRowFocus(e),
      onKeydown: (e) => this.onListKey(e),
    }, this.list);
    this.loadingEl = h('div.conv-loading', { role: 'status', hidden: true }, ui.spinner(28), h('span.sr-only', 'Loading messages'));
    this.fabBadge = h('span.conv-fab-badge', { hidden: true });
    this.fab = h('button.conv-fab', { type: 'button', hidden: true, 'aria-label': 'Scroll to the newest message', onClick: () => this.onFab() }, icon('chevron-down', { size: 26 }), this.fabBadge);
    this.mentionFab = h('button.conv-fab.conv-mention-fab', { type: 'button', hidden: true, 'aria-label': 'Go to your first unread mention', onClick: () => this.onMentionFab() }, icon('at', { size: 22 }));
    this.dropEl = h('div.conv-drop', { hidden: true, 'aria-hidden': 'true' }, icon('attach', { size: 40 }), h('p', 'Drop files to send them'));
    this.composerHost = h('div.conv-composer');
    const body = h('div.conv-body', this.scroller, this.loadingEl, this.mentionFab, this.fab, this.dropEl);

    const root = h('div.conv', {
      onDragenter: (e) => this.onDrag(e, 'enter'),
      onDragover: (e) => this.onDrag(e, 'over'),
      onDragleave: (e) => this.onDrag(e, 'leave'),
      onDrop: (e) => this.onDrag(e, 'drop'),
    }, header, this.pinnedBar, this.searchBar.el, body, this.composerHost);
    this.container.replaceChildren(root);

    this.actionEnv = {
      getMessage: (id) => store.getMessage(this.chatId, id),
      getPending: (cid) => outbox.get(cid),
      onMenu: (subject, anchor, point) => this.onRowMenu(subject, anchor, point),
      onQuote: (replyTo) => this.jumpTo(replyTo.id),
      onMedia: (message) => this.openMedia(message),
      onReact: (message, emoji) => {
        reactTo(message, emoji);
      },
      onRetry: (cid) => outbox.retry(cid),
      onCancel: (cid) => outbox.discard(cid),
      onJump: (id) => this.jumpTo(id),
    };
    this.unbindActions = bindMessageActions(this.msgs, this.actionEnv);
    this.unbindActionsOutbox = bindMessageActions(this.outboxEl, this.actionEnv);
  }

  /* -------------------------------------------------------------------------------------- */
  /* Header                                                                                 */
  /* -------------------------------------------------------------------------------------- */

  paintHeader() {
    const chat = store.getChat(this.chatId);
    if (!chat) return;
    this.titleEl.textContent = store.chatTitle(chat);
    this.avatarSlot.replaceChildren(chatAvatar(chat, { size: 'md', online: null }));
    this.scroller.setAttribute('aria-label', `Messages in ${store.chatTitle(chat)}`);
    this.paintStatus();
    this.paintPinned(chat);
  }

  /** Status line: typing, presence or the group's member names. */
  paintStatus() {
    const chat = store.getChat(this.chatId);
    if (!chat || !this.statusEl) return;
    const typing = store.typingLabel(this.chatId);
    let text = '';
    if (typing) {
      text = typing.text;
    } else if (chat.kind === 'direct') {
      const peer = store.chatPeer(chat);
      if (store.isSelfChat(chat)) text = 'Message yourself';
      else if (peer && peer.disabled) text = 'Account disabled';
      else text = peer ? formatLastSeen(peer) : '';
    } else {
      const meId = store.me ? store.me.id : -1;
      const names = (chat.members || []).filter((m) => m.user_id !== meId).slice(0, 30).map((m) => store.userName(m.user_id));
      text = names.length ? `${names.join(', ')}${chat.members.length - 1 > names.length ? '…' : ''}, You` : 'You';
    }
    if (this.statusEl.textContent !== text) this.statusEl.textContent = text;
    this.statusEl.classList.toggle('typing', Boolean(typing));
  }

  /** @param {any} chat */
  paintPinned(chat) {
    const pins = Array.isArray(chat.pinned_messages) ? chat.pinned_messages : [];
    this.pinnedBar.hidden = pins.length === 0;
    if (pins.length === 0) return;
    const i = this.pinIndex % pins.length;
    this.pinnedBar.replaceChildren(
      h('span.conv-pinned-icon', icon('pin', { size: 18 })),
      h('span.conv-pinned-text', h('span.conv-pinned-label', pins.length > 1 ? `Pinned message ${i + 1} of ${pins.length}` : 'Pinned message'),
        h('span.conv-pinned-preview.truncate', { dir: 'auto' }, store.summarize(pins[i]))));
    this.pinnedBar.setAttribute('aria-label', `Go to pinned message: ${store.summarize(pins[i])}`);
  }

  onPinnedClick() {
    const chat = store.getChat(this.chatId);
    const pins = chat && Array.isArray(chat.pinned_messages) ? chat.pinned_messages : [];
    if (!pins.length) return;
    const target = pins[this.pinIndex % pins.length];
    this.pinIndex = (this.pinIndex + 1) % pins.length;
    this.paintPinned(/** @type {any} */ (chat));
    this.jumpTo(target.id);
  }

  openHeaderMenu() {
    const chat = store.getChat(this.chatId);
    if (!chat) return;
    const items = [
      { label: chat.kind === 'group' ? 'Group info' : 'Contact info', icon: 'info', onSelect: () => afterMenuClose(() => this.openDrawer()) },
      { label: 'Search', icon: 'search', onSelect: () => afterMenuClose(() => this.openSearch()) },
      { label: 'Starred messages', icon: 'star-outline', onSelect: () => router.openStarred() },
    ];
    ui.menu(items, { anchor: this.moreBtn, label: 'Chat options', sheet: 'auto' });
  }

  onChat() {
    this.paintHeader();
    this.paintFab();
  }

  /* -------------------------------------------------------------------------------------- */
  /* Rendering                                                                              */
  /* -------------------------------------------------------------------------------------- */

  /** @param {boolean} on */
  showLoading(on) {
    this.loadingEl.hidden = !on;
  }

  /** @param {string} text */
  showEmpty(text) {
    this.emptyEl.hidden = false;
    this.emptyEl.firstElementChild.textContent = text;
  }

  /** @param {boolean} on */
  setEmpty(on) {
    this.emptyEl.hidden = !on;
    if (on) this.emptyEl.firstElementChild.textContent = 'No messages yet. Say hello!';
  }

  /** @returns {any[]} window items */
  items() {
    const w = store.getWindow(this.chatId);
    return w ? w.items : [];
  }

  /**
   * Full (re)render of the window.
   * @param {{first?: boolean, messageId?: number}} [opts]
   */
  renderAll(opts = {}) {
    const keep = !opts.first ? this.captureAnchor() : null;
    const wasStuck = this.stuck;
    if (opts.first && this.info) {
      const chat = store.getChat(this.chatId);
      const w = store.getWindow(this.chatId);
      this.dividerCount = this.info.unreadAtOpen;
      this.openNewest = Math.max(chat && chat.last_message_id ? chat.last_message_id : 0, w ? w.hi : 0);
    }
    this.scroller.setAttribute('aria-busy', 'true');
    for (const row of this.rows.values()) disposeRow(row);
    this.rows.clear();
    this.seps.clear();
    this.msgs.replaceChildren();
    for (const row of this.pendingRows.values()) disposeRow(row);
    this.pendingRows.clear();
    this.outboxEl.replaceChildren();
    this.reconcile();
    this.reconcileOutbox();
    this.paintTop();
    this.scroller.removeAttribute('aria-busy');
    this.setEmpty(this.items().length === 0 && this.pendingRows.size === 0);

    const center = opts.messageId || this.pendingCenter;
    this.pendingCenter = 0;
    const bottom = this.pendingBottom;
    this.pendingBottom = false;
    if (center && this.rows.has(center)) {
      this.centerOn(center);
    } else if (opts.first && this.info && this.info.scroll === 'divider' && this.dividerEl && this.dividerEl.isConnected) {
      this.setTop(this.dividerEl.offsetTop - this.scroller.clientHeight * DIVIDER_TOP_RATIO);
    } else if (!opts.first && !bottom && !wasStuck && keep && this.rows.has(keep.id)) {
      this.restoreAnchor(keep);
    } else {
      this.scrollToBottom();
    }
    this.afterPosition();
    if (opts.messageId && !this.rows.has(opts.messageId)) this.jumpTo(opts.messageId);
    this.composer.resolveContext();
  }

  /** Re-label the divider (the count is fixed at open plus the messages that arrived since). */
  paintDivider() {
    if (!this.dividerEl) return;
    const label = unreadLabel(this.dividerCount);
    const span = this.dividerEl.firstElementChild;
    if (span && span.textContent !== label) span.textContent = label;
  }

  /**
   * Make the DOM match `win.items`: create, remove and order rows, date separators and the unread
   * divider; recompute the run flags. Touches the DOM only where it differs.
   */
  reconcile() {
    const items = this.items();
    const meta = this.info;
    const dividerId = meta && meta.dividerId ? meta.dividerId : null;
    this.eagerFrom = items.length > EAGER_COUNT ? items[items.length - EAGER_COUNT].id : 0;
    /** @type {Node[]} */
    const nodes = [];
    const want = new Set();
    let prev = null;
    for (const m of items) {
      want.add(m.id);
      let row = this.rows.get(m.id);
      if (!row) {
        row = createRow(m, this.viewEnv);
        this.rows.set(m.id, row);
      }
      if (!prev || dayKey(prev.created_at) !== dayKey(m.created_at)) {
        let sep = this.seps.get(m.id);
        if (!sep) {
          sep = createDateSeparator(m.created_at);
          this.seps.set(m.id, sep);
        }
        nodes.push(sep);
      } else if (this.seps.has(m.id)) {
        /** @type {HTMLElement} */ (this.seps.get(m.id)).remove();
        this.seps.delete(m.id);
      }
      if (dividerId === m.id) {
        if (!this.dividerEl) this.dividerEl = createUnreadDivider(1);
        nodes.push(this.dividerEl);
      }
      nodes.push(row);
      const cont = isSameRun(prev, m);
      if (row.classList.contains('run-cont') !== cont || (!cont && !row.classList.contains('run-first'))) setRunContinuation(row, cont);
      prev = m;
    }
    for (const [id, row] of this.rows) {
      if (want.has(id)) continue;
      disposeRow(row);
      row.remove();
      this.rows.delete(id);
      const sep = this.seps.get(id);
      if (sep) {
        sep.remove();
        this.seps.delete(id);
      }
    }
    if (this.dividerEl && !(dividerId && want.has(dividerId))) {
      this.dividerEl.remove();
      this.dividerEl = null;
    }
    let cur = this.msgs.firstChild;
    for (const node of nodes) {
      if (node === cur) cur = cur.nextSibling;
      else this.msgs.insertBefore(node, cur);
    }
    while (cur) {
      const next = cur.nextSibling;
      cur.remove();
      cur = next;
    }
    this.paintDivider();
    this.markRoving();
  }

  /** Outbox bubbles after the window items (SPEC 9.6 item order); only when the window ends at the newest message. */
  reconcileOutbox() {
    const w = store.getWindow(this.chatId);
    const list = w && !w.has_more_after ? outbox.items(this.chatId) : [];
    const items = this.items();
    /** @type {Node[]} */
    const nodes = [];
    const want = new Set();
    let prevMsg = items.length ? items[items.length - 1] : null;
    for (const item of list) {
      want.add(item.client_id);
      const m = pendingToMessage(item);
      let row = this.pendingRows.get(item.client_id);
      if (!row) {
        row = createRow(m, this.viewEnv, item);
        this.pendingRows.set(item.client_id, row);
      }
      if (!prevMsg || dayKey(prevMsg.created_at) !== dayKey(m.created_at)) {
        nodes.push(createDateSeparator(m.created_at));
      }
      nodes.push(row);
      setRunContinuation(row, isSameRun(prevMsg, m));
      prevMsg = m;
    }
    for (const [cid, row] of this.pendingRows) {
      if (want.has(cid)) continue;
      disposeRow(row);
      row.remove();
      this.pendingRows.delete(cid);
    }
    this.outboxEl.replaceChildren(...nodes);
    this.setEmpty(this.ready && items.length === 0 && this.pendingRows.size === 0);
  }

  paintTop() {
    const w = store.getWindow(this.chatId);
    this.topEl.replaceChildren(...(this.loadingOlder ? [ui.spinner(20)] : []));
    this.topEl.classList.toggle('loading', this.loadingOlder);
    this.topEl.dataset.more = w && w.has_more_before ? '1' : '';
  }

  /**
   * Window events of the open chat.
   * @param {{type: string, ids?: number[], side?: string}} ev
   */
  onMessagesEvent(ev) {
    if (!this.ready) {
      if (ev.type === 'reset') {
        const info = store.getOpenInfo(this.chatId);
        if (info && info.unreadAtOpen > 0) store.setViewState(this.chatId, { stuck: false });
      }
      return;
    }
    switch (ev.type) {
      case 'reset':
        this.renderAll({});
        break;
      case 'append':
      case 'insert': {
        const meId = store.me ? store.me.id : -1;
        for (const id of ev.ids || []) {
          const m = store.getMessage(this.chatId, id);
          if (m && m.id > this.openNewest) {
            this.openNewest = m.id;
            if (m.sender_id !== meId && m.kind !== 'system' && !m.deleted) this.dividerCount += 1;
          }
        }
        const mine = (ev.ids || []).some((id) => {
          const m = store.getMessage(this.chatId, id);
          return Boolean(m && m.sender_id === meId);
        });
        const follow = this.stuck || (ev.type === 'append' && mine);
        const anchor = follow ? null : this.captureAnchor();
        this.reconcile();
        this.reconcileOutbox();
        if (follow) this.scrollToBottom();
        else if (anchor) this.restoreAnchor(anchor);
        this.afterPosition();
        break;
      }
      case 'prepend':
      case 'trim':
      case 'remove': {
        const anchor = this.stuck ? null : this.captureAnchor();
        const before = this.scroller.scrollHeight;
        this.reconcile();
        this.reconcileOutbox();
        if (this.stuck) this.scrollToBottom();
        else if (anchor && this.rows.has(anchor.id)) this.restoreAnchor(anchor);
        else if (ev.type === 'trim' && ev.side === 'start') this.setTop(this.scroller.scrollTop - (before - this.scroller.scrollHeight));
        this.suppressLoadBriefly();
        this.paintTop();
        this.afterPosition();
        break;
      }
      case 'update':
        for (const id of ev.ids || []) {
          const row = this.rows.get(id);
          const m = store.getMessage(this.chatId, id);
          if (row && m) patchRow(row, m, this.viewEnv);
        }
        break;
      case 'divider': {
        const w = store.getWindow(this.chatId);
        this.dividerCount = countUnreadFrom(this.items(), (ev.ids || [0])[0]);
        this.openNewest = w ? w.hi : this.openNewest;
        this.reconcile();
        break;
      }
      default:
        break;
    }
  }

  /**
   * Outbox events of this chat.
   * @param {{type: string, item: any}} ev
   */
  onOutbox(ev) {
    if (!this.ready) return;
    const w = store.getWindow(this.chatId);
    if (!w || w.has_more_after) return;
    if (ev.type === 'update') {
      const row = this.pendingRows.get(ev.item.client_id);
      if (row) patchRow(row, pendingToMessage(ev.item), this.viewEnv, ev.item);
      return;
    }
    const follow = this.stuck || ev.type === 'add';
    this.reconcileOutbox();
    if (follow) this.scrollToBottom();
    this.afterPosition();
  }

  /* -------------------------------------------------------------------------------------- */
  /* Scrolling                                                                              */
  /* -------------------------------------------------------------------------------------- */

  /** @returns {number} distance from the bottom edge in px */
  distanceFromBottom() {
    const s = this.scroller;
    return s.scrollHeight - s.scrollTop - s.clientHeight;
  }

  /**
   * Programmatic scroll. The scroll event it causes must not recompute `stuck`: content that changed size in the
   * same frame (the composer growing, an image loading) would then flip it to false before the ResizeObserver
   * got to re-pin the list.
   * @param {number} top
   */
  setTop(top) {
    this.suppressLoadBriefly();
    const s = this.scroller;
    const before = s.scrollTop;
    s.scrollTop = Math.max(0, top);
    this.progPending = s.scrollTop !== before;
    this.progTop = s.scrollTop;
  }

  /** Ignore "near the top" while a programmatic scroll settles (SPEC 9.6 rule 2). */
  suppressLoadBriefly() {
    this.suppressLoad = true;
    const t = setTimeout(() => {
      this.timers.delete(t);
      this.suppressLoad = false;
      this.maybeLoadMore();
    }, 120);
    this.timers.add(t);
  }

  /** @param {boolean} [smooth] */
  scrollToBottom(smooth = false) {
    const s = this.scroller;
    if (smooth && !prefersReducedMotion() && typeof s.scrollTo === 'function') {
      this.suppressLoadBriefly();
      s.scrollTo({ top: s.scrollHeight, behavior: 'smooth' });
    } else {
      this.setTop(s.scrollHeight);
    }
    this.stuck = true;
  }

  /** @returns {{id: number, offset: number}|null} the top-most visible message and its offset */
  captureAnchor() {
    const items = this.items();
    const top = this.scroller.scrollTop;
    let lo = 0;
    let hi = items.length - 1;
    let found = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const row = this.rows.get(items[mid].id);
      if (row && row.offsetTop + row.offsetHeight > top) {
        found = mid;
        hi = mid - 1;
      } else {
        lo = mid + 1;
      }
    }
    if (found < 0) return null;
    const row = /** @type {HTMLElement} */ (this.rows.get(items[found].id));
    return { id: items[found].id, offset: row.offsetTop - top };
  }

  /** @param {{id: number, offset: number}} a */
  restoreAnchor(a) {
    const row = this.rows.get(a.id);
    if (row && row.isConnected) this.setTop(row.offsetTop - a.offset);
  }

  /** @param {number} id message id (must be rendered) */
  centerOn(id) {
    const row = this.rows.get(id);
    if (!row) return;
    this.setTop(row.offsetTop - (this.scroller.clientHeight - row.offsetHeight) / 2);
    this.flash(row);
  }

  /** @param {HTMLElement} row */
  flash(row) {
    row.classList.remove('flash');
    // Restart the CSS animation when the same row is flashed twice in a row.
    void row.offsetWidth;
    row.classList.add('flash');
    const t = setTimeout(() => {
      this.timers.delete(t);
      row.classList.remove('flash');
    }, FLASH_MS);
    this.timers.add(t);
  }

  /** Called after every programmatic positioning: refresh anchor, FAB, seen id and the store's view state. */
  afterPosition() {
    this.stuck = this.distanceFromBottom() <= STICK_PX;
    this.anchor = this.captureAnchor();
    this.paintFab();
    this.updateSeen();
    this.report();
    this.maybeLoadMore();
  }

  onScroll() {
    if (!this.ready) return;
    if (this.progPending && Math.abs(this.scroller.scrollTop - this.progTop) < 1.5) {
      this.progPending = false;
      this.anchor = this.captureAnchor();
      this.paintFab();
      this.updateSeen();
      return;
    }
    this.progPending = false;
    this.stuck = this.distanceFromBottom() <= STICK_PX;
    this.anchor = this.captureAnchor();
    this.paintFab();
    this.updateSeen();
    this.report();
    this.maybeLoadMore();
  }

  /** ResizeObserver: re-pin when stuck, otherwise keep the anchor in place (SPEC 9.6 rule 4). */
  onResize() {
    if (!this.ready || this.destroyed) return;
    if (this.stuck) {
      this.setTop(this.scroller.scrollHeight);
    } else if (this.anchor) {
      const row = this.rows.get(this.anchor.id);
      if (row && row.isConnected) {
        const want = row.offsetTop - this.anchor.offset;
        if (Math.abs(want - this.scroller.scrollTop) > 0.5) this.setTop(want);
      }
    }
    this.paintFab();
  }

  /** Report stuck / anchor to the store when they changed. */
  report() {
    const anchorId = this.anchor ? this.anchor.id : null;
    const r = this.reported;
    if (r.stuck === this.stuck && r.anchorId === anchorId) return;
    this.reported = { stuck: this.stuck, anchorId };
    store.setViewState(this.chatId, { stuck: this.stuck, anchorId });
  }

  /** `store.markSeen`: the newest message that is at least half inside the viewport (only counted while visible and focused). */
  updateSeen() {
    if (!this.ready || !store.ui.visible || !store.ui.focused) return;
    const items = this.items();
    if (!items.length) return;
    const s = this.scroller;
    const top = s.scrollTop;
    const bottom = top + s.clientHeight;
    let lo = 0;
    let hi = items.length - 1;
    let idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      const row = this.rows.get(items[mid].id);
      if (row && row.offsetTop + row.offsetHeight / 2 <= bottom) {
        idx = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    for (let k = idx; k >= 0 && k > idx - 3; k -= 1) {
      const row = this.rows.get(items[k].id);
      if (!row) continue;
      const visible = Math.min(row.offsetTop + row.offsetHeight, bottom) - Math.max(row.offsetTop, top);
      if (visible >= Math.min(row.offsetHeight / 2, s.clientHeight / 2)) {
        store.markSeen(this.chatId, items[k].id);
        return;
      }
    }
  }

  /** Infinite scroll: older pages near the top, newer pages near the bottom while the window lacks them. */
  maybeLoadMore() {
    if (!this.ready || this.suppressLoad || Date.now() < this.retryAt) return;
    const w = store.getWindow(this.chatId);
    if (!w) return;
    if (w.has_more_before && !this.loadingOlder && this.scroller.scrollTop < LOAD_MORE_PX) {
      this.loadingOlder = true;
      this.paintTop();
      store.loadOlder(this.chatId).catch((err) => this.onLoadError(err)).finally(() => {
        this.loadingOlder = false;
        this.paintTop();
        this.maybeLoadMore();
      });
    } else if (w.has_more_after && !this.loadingNewer && this.distanceFromBottom() < LOAD_MORE_PX) {
      this.loadingNewer = true;
      store.loadNewer(this.chatId).catch((err) => this.onLoadError(err)).finally(() => {
        this.loadingNewer = false;
        this.maybeLoadMore();
      });
    }
  }

  /** @param {any} err */
  onLoadError(err) {
    if (err && err.code === 'cancelled') return;
    this.retryAt = Date.now() + 3000;
    ui.toast(errorText(err, 'Could not load more messages.'), { type: 'error', key: 'history-error' });
  }

  /* -------------------------------------------------------------------------------------- */
  /* FAB                                                                                    */
  /* -------------------------------------------------------------------------------------- */

  paintFab() {
    if (!this.ready) return;
    const chat = store.getChat(this.chatId);
    const w = store.getWindow(this.chatId);
    const away = (w && w.has_more_after) || this.distanceFromBottom() > FAB_PX;
    this.fab.hidden = !away;
    const unread = chat ? chat.me.unread : 0;
    this.fabBadge.hidden = !(away && unread > 0);
    this.fabBadge.textContent = unread > 999 ? '999+' : String(unread);
    this.fab.setAttribute('aria-label', unread > 0 ? `Scroll to the newest message, ${unread} unread` : 'Scroll to the newest message');
    const mentions = chat ? chat.me.unread_mentions : 0;
    this.mentionFab.hidden = !(mentions > 0 && chat && chat.me.first_unread_mention_id);
  }

  async onFab() {
    const w = store.getWindow(this.chatId);
    if (w && w.has_more_after) {
      this.pendingBottom = true;
      try {
        await store.jumpToLatest(this.chatId);
      } catch (err) {
        this.pendingBottom = false;
        if (!err || err.code !== 'cancelled') ui.toast(errorText(err, 'Could not load the newest messages.'), { type: 'error' });
      }
    } else {
      this.scrollToBottom(true);
    }
  }

  onMentionFab() {
    const chat = store.getChat(this.chatId);
    if (chat && chat.me.first_unread_mention_id) this.jumpTo(chat.me.first_unread_mention_id);
  }

  /* -------------------------------------------------------------------------------------- */
  /* Jumping                                                                                */
  /* -------------------------------------------------------------------------------------- */

  /**
   * Scroll to a message (loading its neighbourhood when it is outside the window) and flash it.
   * @param {number} id
   */
  async jumpTo(id) {
    if (!this.ready) return;
    const row = this.rows.get(id);
    if (row) {
      this.centerOn(id);
      this.afterPosition();
      return;
    }
    const token = this.token;
    this.pendingCenter = id;
    try {
      await store.jumpTo(this.chatId, id);
      if (token === this.token && this.pendingCenter === id && this.rows.has(id)) {
        this.pendingCenter = 0;
        this.centerOn(id);
        this.afterPosition();
      }
    } catch (err) {
      this.pendingCenter = 0;
      if (token !== this.token || (err && err.code === 'cancelled')) return;
      if (err && err.code === 'not_found') ui.toast('Original message is no longer available', { type: 'error', key: 'jump-missing' });
      else ui.toast(errorText(err, 'Could not go to that message.'), { type: 'error', key: 'jump-error' });
    }
  }

  /* -------------------------------------------------------------------------------------- */
  /* Row interaction                                                                        */
  /* -------------------------------------------------------------------------------------- */

  /**
   * @param {{message?: any, item?: any}} subject
   * @param {HTMLElement|null} anchor
   * @param {{x: number, y: number}} [point]
   */
  onRowMenu(subject, anchor, point) {
    if (subject.item) {
      openPendingMenu(subject.item, { anchor: anchor || undefined, x: point ? point.x : undefined, y: point ? point.y : undefined });
      return;
    }
    const message = subject.message;
    if (!message || message.kind === 'system') return;
    openMessageMenu(message, {
      context: 'chat',
      anchor: anchor || undefined,
      x: point ? point.x : undefined,
      y: point ? point.y : undefined,
      onReply: (m) => this.composer.setReply(m),
      onEdit: (m) => this.composer.startEdit(m),
    });
  }

  /** @param {any} message the clicked image/video message */
  openMedia(message) {
    const entries = this.items().map((m) => ({ m, item: lightboxItemFromMessage(m) })).filter((e) => e.item);
    const index = entries.findIndex((e) => e.m.id === message.id);
    if (index < 0) return;
    openLightbox({ items: entries.map((e) => /** @type {any} */ (e.item)), index });
  }

  /* -------------------------------------------------------------------------------------- */
  /* Keyboard: roving tabindex                                                              */
  /* -------------------------------------------------------------------------------------- */

  /** Give the newest row the tab stop unless a row already has it. */
  markRoving() {
    if (this.msgs.querySelector('.msg-row[tabindex="0"]')) return;
    const last = this.msgs.lastElementChild;
    if (last instanceof HTMLElement && last.classList.contains('msg-row')) last.tabIndex = 0;
  }

  /** @param {FocusEvent} e */
  onRowFocus(e) {
    const t = e.target instanceof Element ? /** @type {HTMLElement|null} */ (e.target.closest('.msg-row')) : null;
    if (!t) return;
    for (const r of /** @type {HTMLElement[]} */ (Array.from(this.list.querySelectorAll('.msg-row[tabindex="0"]')))) if (r !== t) r.tabIndex = -1;
    t.tabIndex = 0;
  }

  /** @param {KeyboardEvent} e */
  onListKey(e) {
    const t = e.target;
    if (!(t instanceof HTMLElement) || !t.classList.contains('msg-row')) return;
    /** @type {HTMLElement[]} */
    const rows = Array.from(this.list.querySelectorAll('.msg-row'));
    const i = rows.indexOf(t);
    let next = null;
    if (e.key === 'ArrowUp') next = rows[i - 1];
    else if (e.key === 'ArrowDown') next = rows[i + 1];
    else if (e.key === 'Home') next = rows[0];
    else if (e.key === 'End') next = rows[rows.length - 1];
    else if (e.key === 'PageUp') next = rows[Math.max(0, i - 8)];
    else if (e.key === 'PageDown') next = rows[Math.min(rows.length - 1, i + 8)];
    if (next) {
      e.preventDefault();
      next.focus();
    }
  }

  /* -------------------------------------------------------------------------------------- */
  /* Drag and drop                                                                          */
  /* -------------------------------------------------------------------------------------- */

  /**
   * @param {DragEvent} e
   * @param {'enter'|'over'|'leave'|'drop'} phase
   */
  onDrag(e, phase) {
    const dt = e.dataTransfer;
    if (!dt || !Array.from(dt.types || []).includes('Files')) return;
    e.preventDefault();
    if (phase === 'enter') this.dragDepth += 1;
    else if (phase === 'leave') this.dragDepth = Math.max(0, this.dragDepth - 1);
    else if (phase === 'drop') this.dragDepth = 0;
    if (phase === 'over') dt.dropEffect = 'copy';
    this.dropEl.hidden = this.dragDepth === 0;
    if (phase === 'drop' && dt.files && dt.files.length) this.composer.addFiles(Array.from(dt.files));
  }

  /* -------------------------------------------------------------------------------------- */
  /* In-chat search                                                                         */
  /* -------------------------------------------------------------------------------------- */

  /**
   * @returns {{el: HTMLElement, input: HTMLInputElement, count: HTMLElement, up: HTMLElement, down: HTMLElement}}
   */
  buildSearchBar() {
    const input = /** @type {HTMLInputElement} */ (h('input.input.conv-search-input', {
      type: 'search', placeholder: 'Search in this chat', 'aria-label': 'Search in this chat', autocomplete: 'off', dir: 'auto',
      onInput: () => this.onSearchInput(),
      onKeydown: (e) => {
        if (e.isComposing) return;
        if (e.key === 'Enter') {
          e.preventDefault();
          this.stepSearch(e.shiftKey ? -1 : 1);
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          this.stepSearch(1);
        } else if (e.key === 'ArrowDown') {
          e.preventDefault();
          this.stepSearch(-1);
        }
      },
    }));
    const count = h('span.conv-search-count.muted', { 'aria-live': 'polite' });
    const up = h('button.btn-icon', { type: 'button', 'aria-label': 'Previous result (older)', title: 'Older', disabled: true, onClick: () => this.stepSearch(1) }, icon('chevron-up'));
    const down = h('button.btn-icon', { type: 'button', 'aria-label': 'Next result (newer)', title: 'Newer', disabled: true, onClick: () => this.stepSearch(-1) }, icon('chevron-down'));
    const el = h('div.conv-search', { hidden: true, role: 'search' }, icon('search', { size: 20 }), input, count, up, down,
      h('button.btn-icon', { type: 'button', 'aria-label': 'Close search', title: 'Close', onClick: () => this.closeSearch(true) }, icon('close')));
    return { el, input, count, up, down };
  }

  openSearch() {
    if (this.search || !this.ready) {
      if (this.search) this.searchBar.input.focus();
      return;
    }
    const searcher = createSearcher({ chat_id: this.chatId });
    this.search = { searcher, hits: /** @type {any[]} */ ([]), index: -1, q: '', layer: null, wantMore: false };
    this.search.off = searcher.on('update', (st) => this.onSearchState(st));
    this.search.layer = router.pushLayer({ id: 'chat-search', priority: 10, close: () => this.closeSearch(false) });
    this.searchBar.el.hidden = false;
    this.searchBar.input.value = '';
    this.paintSearchCount();
    this.searchBar.input.focus();
  }

  /**
   * @param {boolean} release the owner closes it (not the router layer)
   */
  closeSearch(release) {
    const s = this.search;
    if (!s) return;
    this.search = null;
    if (s.off) s.off();
    s.searcher.destroy();
    if (release && s.layer) s.layer.release();
    this.searchBar.el.hidden = true;
    this.setHighlight('');
    if (release && this.composer) this.composer.focus();
  }

  onSearchInput() {
    const s = this.search;
    if (!s) return;
    const q = this.searchBar.input.value;
    s.searcher.setQuery(q);
    if (q.trim().length < 2) {
      s.hits = [];
      s.index = -1;
      this.setHighlight('');
    }
    this.paintSearchCount();
  }

  /**
   * @param {{q: string, results: Array<{message: any}>, hasMore: boolean, total: number|null, totalCapped: boolean, loading: boolean, error: any}} st
   */
  onSearchState(st) {
    const s = this.search;
    if (!s) return;
    const hits = st.results.map((r) => r.message);
    const newQuery = st.q.trim() !== s.q;
    s.hits = hits;
    s.total = st.total;
    s.capped = st.totalCapped;
    s.hasMore = st.hasMore;
    s.loading = st.loading;
    s.error = st.error;
    if (newQuery) {
      s.q = st.q.trim();
      s.index = hits.length ? 0 : -1;
      this.setHighlight(s.q.length >= 2 ? s.q : '');
      if (s.index === 0) this.jumpTo(hits[0].id);
    } else if (s.wantMore && s.index + 1 < hits.length) {
      s.wantMore = false;
      s.index += 1;
      this.jumpTo(hits[s.index].id);
    }
    this.paintSearchCount();
  }

  /** @param {number} delta +1 = older result, -1 = newer result */
  stepSearch(delta) {
    const s = this.search;
    if (!s || !s.hits.length) return;
    const n = s.index + delta;
    if (n < 0) return;
    if (n >= s.hits.length) {
      if (s.hasMore) {
        s.wantMore = true;
        s.searcher.loadMore();
      }
      return;
    }
    s.index = n;
    this.jumpTo(s.hits[n].id);
    this.paintSearchCount();
  }

  paintSearchCount() {
    const s = this.search;
    const bar = this.searchBar;
    if (!s) return;
    let text = '';
    const q = bar.input.value.trim();
    if (q.length < 2) text = '';
    else if (s.error) text = 'Search failed';
    else if (s.hits.length === 0) text = s.loading || s.q !== q ? 'Searching…' : 'No results';
    else {
      const total = s.capped ? '1000+' : typeof s.total === 'number' ? String(s.total) : `${s.hits.length}${s.hasMore ? '+' : ''}`;
      text = `${s.index + 1} of ${total}`;
    }
    bar.count.textContent = text;
    bar.up.disabled = !s.hits.length || (s.index + 1 >= s.hits.length && !s.hasMore);
    bar.down.disabled = !s.hits.length || s.index <= 0;
  }

  /**
   * Highlight a search term in every rendered message.
   * @param {string} q
   */
  setHighlight(q) {
    if (this.highlight === q) return;
    this.highlight = q;
    this.viewEnv.highlight = q;
    for (const [id, row] of this.rows) {
      const m = store.getMessage(this.chatId, id);
      if (m) patchRow(row, m, this.viewEnv);
    }
  }

  /* -------------------------------------------------------------------------------------- */
  /* Info drawer                                                                            */
  /* -------------------------------------------------------------------------------------- */

  toggleDrawer() {
    if (this.drawer) this.closeDrawer();
    else this.openDrawer();
  }

  openDrawer() {
    if (this.drawer || !store.getChat(this.chatId)) return;
    const slot = this.ctx.slots.drawer;
    this.drawer = createInfoDrawer(slot, {
      chatId: this.chatId,
      onClose: () => this.closeDrawer(),
      onJump: (id) => {
        if (router.isNarrow() && window.innerWidth < 600) this.closeDrawer();
        this.jumpTo(id);
      },
    });
    ui.setDrawerOpen(true);
    this.drawer.focus();
    this.drawerLayer = router.pushLayer({ id: 'drawer', priority: 20, close: () => this.closeDrawer(false) });
  }

  /**
   * @param {boolean} [release=true] release the router layer (false when the router itself closed it)
   */
  closeDrawer(release = true) {
    const d = this.drawer;
    if (!d) return;
    this.drawer = null;
    const layer = this.drawerLayer;
    this.drawerLayer = null;
    if (release && layer) layer.release();
    d.destroy();
    ui.setDrawerOpen(false);
    this.ctx.slots.drawer.replaceChildren();
  }
}

/**
 * Mount the conversation view (core view contract, docs/ui-core-api.md section 1.2).
 * @param {HTMLElement} container
 * @param {any} ctx
 * @returns {{update: (route: any) => void, unmount: () => void}}
 */
export function mount(container, ctx) {
  const view = new ConversationView(container, ctx);
  view.start(ctx.route);
  return { update: (route) => view.update(route), unmount: () => view.destroy() };
}
