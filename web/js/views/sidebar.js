/**
 * views/sidebar.js - the left pane (SPEC 9.2 "Sidebar"): my avatar (opens Settings), connection
 * indicator, "New chat", menu (New group, Starred, Settings, Administration), filter tabs
 * (All / Unread / Groups), the search box (views/searchpanel.js), the archived section and the chat rows
 * with preview, ticks, time, unread / mention badges, pin / mute flags, typing text, drafts and a
 * context menu. Also exports the chat actions that the info drawer reuses (docs/ui-shell-api.md).
 *
 * Mounted by main.js into #pane-sidebar once per session. The list is keyed by chat id and patched in place;
 * rendering is coalesced (requestAnimationFrame, or a timer while the tab is hidden).
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { chatAvatar, userAvatar, setAvatarOnline } from '../core/avatar.js';
import { store, prefs } from '../core/store.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { sound } from '../core/sound.js';
import { errorText, formatCount, formatListTime, isTouchDevice, serverNow } from '../core/util.js';
import * as newchat from './newchat.js';
import * as newgroup from './newgroup.js';
import { createSearchPanel } from './searchpanel.js';

const MUTE_FOREVER = 4102444800;
const MUTE_SECONDS = { '8h': 8 * 3600, '1w': 7 * 24 * 3600 };

/* ------------------------------------------------------------------------------------------ */
/* Chat actions (shared with the info drawer)                                                 */
/* ------------------------------------------------------------------------------------------ */

/**
 * Run a request and turn a failure into a toast.
 * @param {() => Promise<any>} fn
 * @param {string} fallback
 * @returns {Promise<boolean>}
 */
async function attempt(fn, fallback) {
  try {
    await fn();
    return true;
  } catch (err) {
    ui.toast(errorText(err, fallback), { type: 'error' });
    return false;
  }
}

/**
 * @param {any} chat
 * @param {{muted_until?: number, pinned?: boolean, archived?: boolean}} patch
 * @returns {Promise<boolean>}
 */
function sendPrefs(chat, patch) {
  return attempt(() => store.request('chat.prefs', { chat_id: chat.id, ...patch }), 'Could not update this chat.');
}

/**
 * Mute a chat for a while, always, or unmute it.
 * @param {any} chat
 * @param {'8h'|'1w'|'always'|'off'} choice
 * @returns {Promise<boolean>}
 */
export function setMuted(chat, choice) {
  let until = 0;
  if (choice === 'always') until = MUTE_FOREVER;
  else if (choice === '8h' || choice === '1w') until = Math.ceil(serverNow() + MUTE_SECONDS[choice]);
  return sendPrefs(chat, { muted_until: until });
}

/**
 * Pin or unpin a chat (at most `limits.max_pinned_chats` pinned chats).
 * @param {any} chat
 * @param {boolean} pinned
 * @returns {Promise<boolean>}
 */
export function setPinned(chat, pinned) {
  const max = store.limits.max_pinned_chats;
  if (pinned && !chat.me.pinned && store.chats().filter((c) => c.me.pinned).length >= max) {
    ui.toast(`You can pin up to ${max} chats. Unpin one first.`, { type: 'error' });
    return Promise.resolve(false);
  }
  return sendPrefs(chat, chat.me.archived && pinned ? { pinned, archived: false } : { pinned });
}

/**
 * @param {any} chat
 * @param {boolean} archived
 * @returns {Promise<boolean>}
 */
export function setArchived(chat, archived) {
  return sendPrefs(chat, { archived });
}

/**
 * "Clear chat": removes the messages for me only (confirm dialog first).
 * @param {any} chat
 * @returns {Promise<boolean>}
 */
export async function clearChat(chat) {
  const ok = await ui.confirm({
    title: 'Clear this chat?',
    message: `All messages in ${store.chatTitle(chat)} will be removed for you. Other members keep their copy.`,
    confirmLabel: 'Clear chat',
    danger: true,
  });
  if (!ok) return false;
  return attempt(() => store.request('chat.clear', { chat_id: chat.id }), 'Could not clear this chat.');
}

/**
 * Leave a group (confirm dialog first). The default group cannot be left.
 * @param {any} chat
 * @returns {Promise<boolean>}
 */
export async function leaveChat(chat) {
  if (chat.kind !== 'group' || chat.is_default) return false;
  const ok = await ui.confirm({
    title: `Leave "${store.chatTitle(chat)}"?`,
    message: 'You will stop receiving its messages. Only a group admin can add you back.',
    confirmLabel: 'Leave group',
    danger: true,
  });
  if (!ok) return false;
  return attempt(() => store.request('chat.leave', { chat_id: chat.id }), 'Could not leave this group.');
}

/**
 * The mute entries of a chat menu.
 * @param {any} chat
 * @returns {Array<object>} ui.menu items
 */
export function muteItems(chat) {
  if (store.isMuted(chat)) {
    return [{ label: 'Unmute', icon: 'bell', onSelect: () => setMuted(chat, 'off') }];
  }
  return [
    { label: 'Mute for 8 hours', icon: 'bell-off', onSelect: () => setMuted(chat, '8h') },
    { label: 'Mute for 1 week', icon: 'bell-off', onSelect: () => setMuted(chat, '1w') },
    { label: 'Mute always', icon: 'bell-off', onSelect: () => setMuted(chat, 'always') },
  ];
}

/**
 * The context menu of a chat row.
 * @param {any} chat
 * @returns {Array<object>} ui.menu items
 */
export function chatMenuItems(chat) {
  const items = [];
  if (!chat.me.archived) {
    items.push({ label: chat.me.pinned ? 'Unpin chat' : 'Pin chat', icon: 'pin', onSelect: () => setPinned(chat, !chat.me.pinned) });
  }
  items.push(...muteItems(chat));
  items.push({
    label: chat.me.archived ? 'Unarchive chat' : 'Archive chat',
    icon: 'archive',
    onSelect: () => setArchived(chat, !chat.me.archived),
  });
  items.push({ separator: true });
  items.push({ label: 'Clear chat', icon: 'trash', onSelect: () => clearChat(chat) });
  if (chat.kind === 'group' && !chat.is_default) {
    items.push({ label: 'Leave group', icon: 'logout', danger: true, onSelect: () => leaveChat(chat) });
  }
  return items;
}

/**
 * Open the context menu of a chat (a popover, or a bottom sheet on touch).
 * @param {any} chat
 * @param {{anchor?: Element, x?: number, y?: number}} [where]
 * @returns {{close: () => void}}
 */
export function openChatMenu(chat, where = {}) {
  const title = store.chatTitle(chat);
  return ui.menu(chatMenuItems(chat), { ...where, label: `Actions for ${title}`, title });
}

/* ------------------------------------------------------------------------------------------ */
/* Chat rows                                                                                  */
/* ------------------------------------------------------------------------------------------ */

/**
 * Plain-data description of everything a row displays (compared as JSON to skip no-op patches).
 * @param {any} chat
 * @param {boolean} open the chat is the one shown in the main pane
 * @returns {object}
 */
function rowModel(chat, open) {
  const typing = store.typingLabel(chat.id);
  const draft = open ? null : store.getDraft(chat.id);
  const draftText = draft && typeof draft.text === 'string' ? draft.text.trim().split('\n')[0] : '';
  const lp = store.lastPreview(chat);
  const model = {
    title: store.chatTitle(chat),
    kind: chat.kind,
    time: lp ? formatListTime(lp.message.created_at) : '',
    unread: chat.me.unread || 0,
    mentions: chat.me.unread_mentions || 0,
    firstMention: chat.me.first_unread_mention_id || null,
    muted: store.isMuted(chat),
    pinned: Boolean(chat.me.pinned),
    preview: null,
  };
  if (typing) model.preview = { type: 'typing', text: typing.text };
  else if (draftText) model.preview = { type: 'draft', text: draftText };
  else if (lp) {
    const m = lp.message;
    model.preview = {
      type: 'last',
      prefix: lp.prefix,
      text: lp.text,
      deleted: lp.deleted,
      system: m.kind === 'system',
      status: lp.own && !lp.deleted && m.kind !== 'system' ? m.status || null : null,
    };
  }
  return model;
}

/**
 * @param {string|null} status
 * @returns {HTMLElement|null} the delivery tick of an own message
 */
function tickFor(status) {
  if (status !== 'sent' && status !== 'delivered' && status !== 'read') return null;
  const label = status === 'sent' ? 'Sent' : status === 'delivered' ? 'Delivered' : 'Read';
  return h(`span.tick.tick-${status}.chat-tick`, { role: 'img', 'aria-label': label }, icon(status === 'sent' ? 'check' : 'checks', { size: 16 }));
}

/**
 * @param {any} model
 * @returns {Node[]} the preview line
 */
function previewNodes(model) {
  const p = model.preview;
  if (!p) return [];
  if (p.type === 'typing') return [h('span.chat-typing', p.text)];
  if (p.type === 'draft') return [h('span.chat-draft', 'Draft:'), ' ', h('span.chat-preview-text', { dir: 'auto' }, p.text)];
  return [
    tickFor(p.status),
    p.prefix ? h('span.chat-prefix', p.prefix) : null,
    h(`span.chat-preview-text${p.deleted ? '.chat-deleted' : ''}${p.system ? '.chat-system' : ''}`, { dir: 'auto' }, p.text),
  ].filter(Boolean);
}

/**
 * A list of chat rows, keyed by chat id and patched in place. Used for the main list and, through
 * dependency injection, for the "Chats" section of the search panel.
 * @param {{onOpen?: (chat: any) => void}} [opts]
 * @returns {{el: HTMLElement, setIds: (ids: number[]) => void, refresh: () => void, focusFirst: () => boolean, first: () => any, destroy: () => void}}
 */
function createChatList(opts = {}) {
  const list = h('ul.chat-list', { role: 'list' });
  /** @type {Map<number, any>} */
  const rows = new Map();
  /** @type {number[]} */
  let ids = [];
  /** @type {number|null} */
  let rovingId = null;

  const open = (id) => {
    const chat = store.getChat(id);
    if (!chat) return;
    if (opts.onOpen) opts.onOpen(chat);
    else router.openChat(chat.id);
  };

  const menuFor = (rec, where) => {
    const chat = store.getChat(rec.id);
    if (chat) openChatMenu(chat, where);
  };

  const applyTab = (rec) => {
    const idx = rec.tab ? 0 : -1;
    rec.main.tabIndex = idx;
    rec.more.tabIndex = idx;
    for (const b of rec.badgesEl.querySelectorAll('button')) b.tabIndex = idx;
  };

  function createRow(id) {
    const rec = {
      id,
      sig: '',
      avatarKey: '',
      avatarEl: /** @type {HTMLElement|null} */ (null),
      active: false,
      tab: false,
      offs: /** @type {Array<() => void>} */ ([]),
    };
    rec.avatarHost = h('span.chat-avatar');
    rec.nameEl = h('span.chat-name.truncate', { dir: 'auto' });
    rec.previewEl = h('span.chat-preview.truncate');
    rec.timeEl = h('span.chat-time');
    rec.badgesEl = h('span.chat-badges');
    rec.main = h('button.chat-main', { type: 'button', onClick: () => open(id) },
      rec.avatarHost, h('span.chat-text', rec.nameEl, rec.previewEl));
    rec.more = h('button.btn-icon.chat-more', {
      type: 'button',
      'aria-label': 'Chat actions',
      'aria-haspopup': 'menu',
      onClick: () => menuFor(rec, { anchor: rec.more }),
    }, icon('more', { size: 18 }));
    rec.el = h('li.chat-row', {
      dataset: { chatId: id },
      onClick: (e) => {
        if (!(e.target instanceof Element) || !e.target.closest('button')) open(id);
      },
      onContextmenu: (e) => {
        e.preventDefault();
        if (isTouchDevice()) return;
        menuFor(rec, e.clientX === 0 && e.clientY === 0 ? { anchor: rec.main } : { x: e.clientX, y: e.clientY });
      },
    }, rec.main, h('span.chat-side', rec.timeEl, h('span.chat-side-bottom', rec.badgesEl, rec.more)));
    rec.offs.push(ui.onLongPress(rec.el, ({ x, y }) => menuFor(rec, { x, y })));
    rec.offs.push(store.on(`typing:${id}`, () => patchRow(rec)));
    rows.set(id, rec);
    applyTab(rec);
    return rec;
  }

  function patchBadges(rec, model) {
    const nodes = [];
    if (model.muted) nodes.push(h('span.chat-flag', { role: 'img', 'aria-label': 'Muted' }, icon('bell-off', { size: 16 })));
    if (model.pinned && !model.unread) nodes.push(h('span.chat-flag', { role: 'img', 'aria-label': 'Pinned' }, icon('pin', { size: 16 })));
    if (model.mentions > 0) {
      const target = model.firstMention;
      nodes.push(h('button.badge.badge-mention.chat-mention', {
        type: 'button',
        'aria-label': 'Jump to your first unread mention',
        onClick: () => router.openChat(rec.id, target ? { messageId: target } : {}),
      }, '@'));
    }
    if (model.unread > 0) {
      const text = formatCount(model.unread);
      nodes.push(h(`span.badge.${model.muted ? 'badge-muted' : 'badge-unread'}`, {
        'aria-label': model.unread > 999 ? 'More than 999 unread messages' : `${model.unread} unread ${model.unread === 1 ? 'message' : 'messages'}`,
      }, text));
    }
    rec.badgesEl.replaceChildren(...nodes);
    applyTab(rec);
  }

  function patchRow(rec) {
    const chat = store.getChat(rec.id);
    if (!chat) return;
    const route = router.current;
    const isOpen = route.name === 'chat' && route.chatId === chat.id;
    if (isOpen !== rec.active) {
      rec.active = isOpen;
      rec.el.classList.toggle('active', isOpen);
      if (isOpen) rec.main.setAttribute('aria-current', 'true');
      else rec.main.removeAttribute('aria-current');
    }
    const model = rowModel(chat, isOpen);
    const sig = JSON.stringify(model);
    if (sig !== rec.sig) {
      rec.sig = sig;
      const avatarKey = `${chat.kind}:${model.title}`;
      if (avatarKey !== rec.avatarKey) {
        rec.avatarKey = avatarKey;
        rec.avatarEl = chatAvatar(chat, { size: 'md' });
        rec.avatarHost.replaceChildren(rec.avatarEl);
      }
      rec.nameEl.textContent = model.title;
      rec.timeEl.textContent = model.time;
      rec.timeEl.classList.toggle('unread', model.unread > 0 && !model.muted);
      rec.previewEl.replaceChildren(...previewNodes(model));
      patchBadges(rec, model);
    }
    if (rec.avatarEl && chat.kind === 'direct') {
      const peer = store.chatPeer(chat);
      setAvatarOnline(rec.avatarEl, Boolean(peer && peer.online));
    }
  }

  function dropRow(rec) {
    for (const off of rec.offs) off();
    rec.el.remove();
    rows.delete(rec.id);
  }

  function updateRoving() {
    if (rovingId === null || !rows.has(rovingId) || !ids.includes(rovingId)) {
      const route = router.current;
      rovingId = route.name === 'chat' && ids.includes(route.chatId) ? route.chatId : ids.length ? ids[0] : null;
    }
    for (const rec of rows.values()) {
      const tab = rec.id === rovingId;
      if (tab !== rec.tab) {
        rec.tab = tab;
        applyTab(rec);
      }
    }
  }

  function refresh() {
    const focused = document.activeElement && list.contains(document.activeElement) ? /** @type {HTMLElement} */ (document.activeElement) : null;
    const wanted = new Set(ids);
    let prev = null;
    for (const id of ids) {
      if (!store.getChat(id)) continue;
      const rec = rows.get(id) || createRow(id);
      patchRow(rec);
      const ref = prev ? prev.nextSibling : list.firstChild;
      if (rec.el !== ref) list.insertBefore(rec.el, ref);
      prev = rec.el;
    }
    for (const rec of Array.from(rows.values())) if (!wanted.has(rec.id) || !store.getChat(rec.id)) dropRow(rec);
    updateRoving();
    if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  }

  list.addEventListener('focusin', (e) => {
    const li = e.target instanceof Element ? e.target.closest('.chat-row') : null;
    if (!li) return;
    const id = Number(/** @type {HTMLElement} */ (li).dataset.chatId);
    if (id !== rovingId) {
      rovingId = id;
      updateRoving();
    }
  });

  list.addEventListener('keydown', (e) => {
    const main = e.target instanceof Element ? e.target.closest('.chat-main') : null;
    if (!main) return;
    const li = main.closest('li');
    if (!li) return;
    let target = null;
    if (e.key === 'ArrowDown') target = li.nextElementSibling;
    else if (e.key === 'ArrowUp') target = li.previousElementSibling;
    else if (e.key === 'Home') target = list.firstElementChild;
    else if (e.key === 'End') target = list.lastElementChild;
    if (!target) return;
    e.preventDefault();
    const btn = /** @type {HTMLElement|null} */ (target.querySelector('.chat-main'));
    if (btn) btn.focus();
  });

  return {
    el: list,
    setIds(next) {
      ids = next;
      refresh();
    },
    refresh,
    focusFirst() {
      const first = /** @type {HTMLElement|null} */ (list.querySelector('.chat-main'));
      if (first) first.focus();
      return Boolean(first);
    },
    first() {
      const li = /** @type {HTMLElement|null} */ (list.firstElementChild);
      return li ? store.getChat(Number(li.dataset.chatId)) : null;
    },
    destroy() {
      for (const rec of Array.from(rows.values())) dropRow(rec);
    },
  };
}

/* ------------------------------------------------------------------------------------------ */
/* The view                                                                                   */
/* ------------------------------------------------------------------------------------------ */

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'unread', label: 'Unread' },
  { id: 'groups', label: 'Groups' },
];

/**
 * Mount the sidebar.
 * @param {HTMLElement} container #pane-sidebar
 * @param {{signal: AbortSignal}} ctx
 * @returns {{unmount: () => void}}
 */
export function mount(container, ctx) {
  const offs = /** @type {Array<() => void>} */ ([]);
  const state = { filter: 'all', archived: false, query: '' };
  let raf = 0;
  let timer = 0;

  /* ---- header ---- */
  const meBtn = h('button.sb-me', { type: 'button', 'aria-label': 'Your profile and settings', onClick: () => router.openSettings() });
  const wsName = h('span.sb-ws.truncate');
  const connDot = h('span.sb-dot', { 'aria-hidden': 'true' });
  const connText = h('span.sb-conn-text');
  const conn = h('span.sb-conn', { role: 'status' }, connText);
  const soundBtn = h('button.btn-icon.sb-sound', {
    type: 'button',
    hidden: true,
    'aria-label': 'Sound is off - click anywhere to enable',
    title: 'Sound is off - click anywhere to enable',
    onClick: () => sound.unlock(),
  }, icon('volume-off'));
  const newBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'New chat', title: 'New chat', onClick: () => newchat.open() }, icon('plus'));
  const menuBtn = h('button.btn-icon', {
    type: 'button',
    'aria-label': 'Menu',
    title: 'Menu',
    'aria-haspopup': 'menu',
    onClick: () => openMainMenu(menuBtn),
  }, icon('more'));
  const header = h('header.pane-header.sb-header', meBtn, h('div.sb-title.grow', wsName, conn), soundBtn, newBtn, menuBtn);

  function openMainMenu(anchor) {
    const items = [
      { label: 'New group', icon: 'group-add', onSelect: () => newgroup.open() },
      { label: 'Starred messages', icon: 'star', onSelect: () => router.openStarred() },
      { label: 'Settings', icon: 'settings', onSelect: () => router.openSettings() },
    ];
    if (store.me && store.me.role === 'admin') items.push({ label: 'Administration', icon: 'shield', onSelect: () => router.openAdmin() });
    ui.menu(items, { anchor, label: 'Main menu' });
  }

  /* ---- search box ---- */
  const input = /** @type {HTMLInputElement} */ (h('input.input.sb-search-input', {
    type: 'search',
    placeholder: 'Search chats, people and messages',
    'aria-label': 'Search chats, people and messages',
    autocomplete: 'off',
    autocapitalize: 'none',
    spellcheck: 'false',
    enterkeyhint: 'search',
    'data-shortcut': 'search',
    dir: 'auto',
  }));
  const clearBtn = h('button.btn-icon.sb-search-clear', { type: 'button', hidden: true, 'aria-label': 'Clear search', onClick: () => { input.value = ''; setQuery(''); input.focus(); } }, icon('close', { size: 18 }));
  const searchBox = h('div.sb-search', h('span.sb-search-icon', { 'aria-hidden': 'true' }, icon('search', { size: 18 })), input, clearBtn);

  /* ---- filters ---- */
  const filterBtns = FILTERS.map((f) => h('button.sb-filter', {
    type: 'button',
    role: 'tab',
    id: `sb-filter-${f.id}`,
    'aria-controls': 'sb-chats',
    dataset: { filter: f.id },
    onClick: () => setFilter(f.id),
  }, f.label));
  const filters = h('div.sb-filters', { role: 'tablist', 'aria-label': 'Filter chats' }, ...filterBtns);
  filters.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    const i = FILTERS.findIndex((f) => f.id === state.filter);
    const next = FILTERS[(i + (e.key === 'ArrowRight' ? 1 : FILTERS.length - 1)) % FILTERS.length];
    e.preventDefault();
    setFilter(next.id);
    const btn = filterBtns[FILTERS.indexOf(next)];
    btn.focus();
  });

  /* ---- body ---- */
  const list = createChatList();
  const archivedEntry = h('button.sb-archived', { type: 'button', hidden: true, onClick: () => { state.archived = true; renderAll(); } },
    icon('archive', { size: 20 }), h('span.grow.truncate', 'Archived'), h('span.sb-archived-count'));
  const archivedBack = h('button.sb-archived-back', { type: 'button', hidden: true, onClick: () => { state.archived = false; renderAll(); } },
    icon('back', { size: 20 }), h('span', 'Archived chats'));
  const empty = h('div.empty-state.sb-empty', { hidden: true });
  const chatsView = h('div.sb-chats', { id: 'sb-chats', role: 'tabpanel', 'aria-label': 'Chats' }, archivedEntry, archivedBack, list.el, empty);
  const panel = createSearchPanel({ createChatList });
  panel.el.hidden = true;
  const body = h('div.pane-body.sb-body', chatsView, panel.el);

  clear(container);
  container.append(header, searchBox, filters, body);

  /* ---- rendering ---- */

  function renderHeader() {
    const me = store.me;
    wsName.textContent = store.workspace.name;
    meBtn.replaceChildren(me ? userAvatar(me, { size: 'md', online: null }) : '', connDot);
    const c = store.connection;
    let status = 'ready';
    let text = '';
    if (c.state !== 'ready') {
      status = c.online ? 'connecting' : 'offline';
      text = c.online ? (c.banner ? 'Reconnecting…' : 'Connecting…') : 'Offline';
    }
    connDot.dataset.status = status;
    connText.textContent = text;
    conn.setAttribute('aria-label', text || 'Connected');
    soundBtn.hidden = !(store.ui.soundBlocked && prefs.get('sound'));
  }

  function renderFilters() {
    for (const btn of filterBtns) {
      const on = btn.dataset.filter === state.filter;
      btn.setAttribute('aria-selected', String(on));
      btn.tabIndex = on ? 0 : -1;
    }
  }

  function emptyText() {
    if (state.archived) return { title: 'No archived chats', hint: '' };
    if (store.chats().length === 0) return { title: 'No chats yet', hint: 'Start one with the + button.' };
    if (state.filter === 'unread') return { title: 'No unread chats', hint: '' };
    if (state.filter === 'groups') return { title: 'No group chats yet', hint: '' };
    return { title: 'No chats', hint: '' };
  }

  function renderChats() {
    const chats = store.chatList({ filter: state.filter, archived: state.archived });
    list.setIds(chats.map((c) => c.id));
    const archivedCount = state.archived ? 0 : store.chatList({ archived: true }).length;
    archivedEntry.hidden = archivedCount === 0;
    archivedEntry.querySelector('.sb-archived-count').textContent = archivedCount ? String(archivedCount) : '';
    archivedBack.hidden = !state.archived;
    empty.hidden = chats.length > 0;
    if (!chats.length) {
      const t = emptyText();
      empty.replaceChildren(h('p', t.title), t.hint ? h('p.muted', t.hint) : null,
        store.chats().length === 0 ? h('button.btn.btn-primary', { type: 'button', onClick: () => newchat.open() }, 'New chat') : null);
    }
  }

  function renderAll() {
    renderHeader();
    renderFilters();
    if (state.query.trim()) {
      panel.refresh();
    } else {
      renderChats();
    }
  }

  function flush() {
    raf = 0;
    if (timer) clearTimeout(timer);
    timer = 0;
    try {
      renderAll();
    } catch (err) {
      console.error('[sidebar] render failed', err);
    }
  }

  function schedule() {
    if (raf || timer) return;
    if (document.visibilityState === 'hidden') timer = setTimeout(flush, 300);
    else raf = requestAnimationFrame(flush);
  }

  /* ---- interaction ---- */

  function setFilter(id) {
    if (state.filter === id) return;
    state.filter = id;
    renderAll();
  }

  function setQuery(q) {
    state.query = q;
    const searching = q.trim().length > 0;
    clearBtn.hidden = q.length === 0;
    filters.hidden = searching;
    chatsView.hidden = searching;
    panel.el.hidden = !searching;
    panel.setQuery(q);
    if (!searching) renderChats();
  }

  input.addEventListener('input', () => setQuery(input.value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (input.value) {
        e.preventDefault();
        input.value = '';
        setQuery('');
      } else {
        input.blur();
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      (state.query.trim() ? panel.focusFirst() : list.focusFirst());
    } else if (e.key === 'Enter' && state.query.trim() && !e.isComposing) {
      e.preventDefault();
      panel.activateFirst();
    }
  });

  /* ---- subscriptions ---- */
  for (const name of ['chats', 'drafts', 'users', 'presence', 'me', 'workspace', 'connection', 'prefs', 'ready']) offs.push(store.on(name, schedule));
  offs.push(store.on('ui', schedule));
  offs.push(router.on('route', schedule));
  const onVisible = () => {
    if (document.visibilityState === 'visible') flush();
  };
  document.addEventListener('visibilitychange', onVisible, { signal: ctx.signal });

  renderAll();

  return {
    unmount() {
      for (const off of offs) off();
      if (raf) cancelAnimationFrame(raf);
      if (timer) clearTimeout(timer);
      raf = 0;
      timer = 0;
      list.destroy();
      panel.destroy();
    },
  };
}
