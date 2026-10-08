/**
 * views/messagemenu.js - message context menu (popover on pointer devices, bottom sheet on touch), the
 * quick-reaction bar, the outbox-bubble menu, the delete dialog and the message Info sheet.
 *
 * Exported API (docs/ui-conv-api.md section 2): openMessageMenu, openPendingMenu, openMessageInfo,
 * reactTo (used by the reaction chips of messageview.js).
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { ui } from '../core/ui.js';
import { outbox } from '../core/outbox.js';
import { copyToClipboard, errorText, serverNow, formatDateTime, formatTime } from '../core/util.js';
import { userAvatar } from '../core/avatar.js';
import { QUICK_REACTIONS, openEmojiPicker } from '../lib/emoji.js';
import { openForward } from './forward.js';

/** Milliseconds to wait after a menu closed before an action may open another layer (see afterMenuClose). */
const LAYER_SETTLE_MS = 150;

/**
 * Run `fn` once the history entry of the menu that was just closed has been popped. A layer that is released
 * calls `history.back()` asynchronously; pushing the next layer's entry before that traversal finished makes the
 * browser drop it and leaves the router one entry short (the hash would leave the chat). Used by every action
 * that opens a dialog, the composer's reply/edit bar, the drawer or the emoji picker straight after a menu.
 * @param {() => void} fn
 */
export function afterMenuClose(fn) {
  setTimeout(fn, LAYER_SETTLE_MS);
}

/**
 * Show a failed request as a toast.
 * @param {any} err
 * @param {string} fallback
 */
function failed(err, fallback) {
  ui.toast(errorText(err, fallback), { type: 'error', key: 'msg-action-error' });
}

/**
 * Set my single reaction to `emoji`, or remove it when it already is my reaction (SPEC 7.4 SET semantics).
 * @param {any} message
 * @param {string} emoji
 * @returns {Promise<any>} the updated message, or undefined after an error (already toasted)
 */
export async function reactTo(message, emoji) {
  const meId = store.me ? store.me.id : -1;
  const mine = (message.reactions || []).find((r) => r.user_ids.includes(meId));
  try {
    const res = await store.request('msg.react', { message_id: message.id, emoji: mine && mine.emoji === emoji ? null : emoji });
    return res.message;
  } catch (err) {
    failed(err, 'Could not react to the message.');
    return undefined;
  }
}

/**
 * @param {any} message
 * @param {any} chat
 * @returns {{own: boolean, canEdit: boolean, canDeleteAll: boolean}}
 */
function rightsOf(message, chat) {
  const me = store.me;
  const own = Boolean(me && message.sender_id === me.id);
  const age = serverNow() - message.created_at;
  const canEdit = own && message.kind === 'text' && !message.forwarded && !message.deleted && age <= store.limits.edit_window_s;
  const admin = Boolean(chat && chat.kind === 'group' && store.isGroupAdmin(chat));
  const canDeleteAll = !message.deleted && ((own && age <= store.limits.delete_window_s) || admin);
  return { own, canEdit, canDeleteAll };
}

/**
 * Ask how to delete a message and do it.
 * @param {any} message
 * @param {boolean} everyoneAllowed
 * @returns {Promise<any>} the updated message (null when it was only hidden), undefined when cancelled or failed
 */
async function deleteMessage(message, everyoneAllowed) {
  const actions = [{ label: 'Cancel', value: 'cancel', id: 'cancel' }, { label: 'Delete for me', value: 'me', id: 'me' }];
  if (everyoneAllowed) actions.push({ label: 'Delete for everyone', value: 'everyone', id: 'everyone', danger: true });
  const handle = ui.dialog({
    title: 'Delete message?',
    size: 'sm',
    role: 'alertdialog',
    content: h('p.dialog-message', everyoneAllowed ? 'Delete it only for you, or for everyone in this chat.' : 'The message will be removed from your view only.'),
    actions,
    initialFocus: '[data-action="cancel"]',
  });
  const scope = await handle.closed;
  if (scope !== 'me' && scope !== 'everyone') return undefined;
  try {
    const res = await store.request('msg.delete', { message_id: message.id, scope });
    return res.message || null;
  } catch (err) {
    failed(err, 'Could not delete the message.');
    return undefined;
  }
}

/**
 * @param {any} message
 * @returns {Promise<any>} the updated message, or undefined after an error
 */
async function toggleStar(message) {
  try {
    const res = await store.request('msg.star', { message_id: message.id, starred: !message.starred });
    return res.message;
  } catch (err) {
    failed(err, 'Could not update the star.');
    return undefined;
  }
}

/**
 * @param {any} message
 * @returns {Promise<any>} the message with its new pin flag, or undefined after an error
 */
async function togglePin(message) {
  try {
    await store.request('msg.pin', { chat_id: message.chat_id, message_id: message.id, pinned: !message.pinned });
    return { ...message, pinned: !message.pinned };
  } catch (err) {
    if (err && err.code === 'invalid_state' && !message.pinned) {
      ui.toast(`You can pin up to ${store.limits.max_pinned_messages} messages in a chat. Unpin one first.`, { type: 'error', key: 'pin-limit' });
    } else {
      failed(err, 'Could not update the pin.');
    }
    return undefined;
  }
}

/**
 * @param {string} body
 * @returns {Promise<void>}
 */
async function copyBody(body) {
  const ok = await copyToClipboard(body);
  ui.toast(ok ? 'Copied' : "Couldn't copy - select the text and copy it manually", { type: ok ? 'success' : 'error', timeout: ok ? 1500 : 4000, key: 'copied' });
}

/* ------------------------------------------------------------------------------------------ */
/* Menu                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * Menu entries for a message.
 * @param {any} message
 * @param {any} chat
 * @param {{context?: string, onReply?: Function, onEdit?: Function, onGoTo?: Function, onSelectMode?: Function, onChanged?: Function}} opts
 * @returns {Array<{label: string, icon: string, danger?: boolean, onSelect: () => void}>}
 */
function menuItems(message, chat, opts) {
  /** Report a finished mutation (undefined = failed or cancelled). */
  const changed = (promise) => promise.then((result) => {
    if (result !== undefined && opts.onChanged) opts.onChanged(result);
  });
  const { own, canEdit, canDeleteAll } = rightsOf(message, chat);
  const deleted = Boolean(message.deleted);
  const canPost = chat ? store.canPost(chat) : { ok: false };
  const items = [];
  if (opts.onGoTo) items.push({ label: 'Go to message', icon: 'arrow-right', onSelect: () => opts.onGoTo(message) });
  if (opts.context === 'chat' && opts.onReply && !deleted && canPost.ok) items.push({ label: 'Reply', icon: 'reply', onSelect: () => opts.onReply(message) });
  if (!deleted && String(message.body || '').trim() !== '') items.push({ label: 'Copy', icon: 'copy', onSelect: () => copyBody(message.body) });
  if (!deleted) items.push({ label: 'Forward', icon: 'forward', onSelect: () => openForward({ messages: [message], fromChatId: message.chat_id }) });
  if (!deleted) items.push({ label: message.starred ? 'Unstar' : 'Star', icon: message.starred ? 'star' : 'star-outline', onSelect: () => changed(toggleStar(message)) });
  if (!deleted && canPost.ok && chat) items.push({ label: message.pinned ? 'Unpin' : 'Pin', icon: 'pin', onSelect: () => changed(togglePin(message)) });
  if (own && !deleted && chat && !store.isSelfChat(chat)) items.push({ label: 'Info', icon: 'info', onSelect: () => openMessageInfo(message, chat) });
  if (opts.context === 'chat' && opts.onEdit && canEdit && canPost.ok) items.push({ label: 'Edit', icon: 'edit', onSelect: () => opts.onEdit(message) });
  if (opts.onSelectMode) items.push({ label: 'Select', icon: 'check-circle', onSelect: () => opts.onSelectMode() });
  items.push({ label: 'Delete', icon: 'trash', danger: true, onSelect: () => changed(deleteMessage(message, canDeleteAll)) });
  return items;
}

/**
 * Open the message menu next to an anchor / point (popover) or as a bottom sheet on touch devices.
 * @param {any} message a held Message (not a system message)
 * @param {{context?: 'chat'|'starred'|'pinned', anchor?: Element, x?: number, y?: number, chat?: any,
 *          onReply?: (m: any) => void, onEdit?: (m: any) => void, onGoTo?: (m: any) => void,
 *          onSelectMode?: () => void, onChanged?: (message: any|null) => void}} [opts]
 * @returns {{close: () => void}}
 */
export function openMessageMenu(message, opts = {}) {
  const chat = opts.chat || store.getChat(message.chat_id);
  const items = menuItems(message, chat, { ...opts, context: opts.context || 'chat' });
  const canReact = !message.deleted && chat && store.canPost(chat).reason !== 'peer_disabled';
  /** @type {{close: () => void}|null} */
  let host = null;
  const close = () => {
    if (host) host.close();
  };

  const quick = canReact
    ? h('div.msgmenu-quick', { role: 'toolbar', 'aria-label': 'Reactions' },
      QUICK_REACTIONS.map((e) => h('button.msgmenu-react', {
        type: 'button',
        'aria-label': `React with ${e}`,
        onClick: () => {
          close();
          reactTo(message, e).then((m) => m !== undefined && opts.onChanged && opts.onChanged(m));
        },
      }, e)),
      h('button.msgmenu-react.more', {
        type: 'button',
        'aria-label': 'More reactions',
        onClick: (ev) => {
          const r = /** @type {HTMLElement} */ (ev.currentTarget).getBoundingClientRect();
          close();
          afterMenuClose(() => openEmojiPicker({ x: r.left, y: r.bottom, onPick: (e) => reactTo(message, e).then((m) => m !== undefined && opts.onChanged && opts.onChanged(m)) }));
        },
      }, icon('plus', { size: 20 })))
    : null;

  const list = h('ul.menu', { role: 'menu', 'aria-label': 'Message actions' }, items.map((it) => h('li', { role: 'none' },
    h(`button.menu-item${it.danger ? '.danger' : ''}`, {
      type: 'button',
      role: 'menuitem',
      onClick: () => {
        close();
        afterMenuClose(it.onSelect);
      },
    }, h('span.menu-icon', icon(it.icon, { size: 20 })), h('span.menu-label', it.label)))));
  const content = h('div.msgmenu', quick, list);
  content.addEventListener('keydown', (e) => {
    const btns = /** @type {HTMLElement[]} */ (Array.from(content.querySelectorAll('button:not(:disabled)')));
    const i = btns.indexOf(/** @type {HTMLElement} */ (document.activeElement));
    let next = null;
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') next = btns[(i + 1) % btns.length];
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') next = btns[(i - 1 + btns.length) % btns.length];
    else if (e.key === 'Home') next = btns[0];
    else if (e.key === 'End') next = btns[btns.length - 1];
    else if (e.key === 'Tab') {
      e.preventDefault();
      close();
      return;
    }
    if (next) {
      e.preventDefault();
      next.focus();
    }
  });

  if (ui.prefersSheet()) {
    host = ui.sheet(content, { label: 'Message actions' });
  } else {
    host = ui.popover(content, { anchor: opts.anchor, x: opts.x, y: opts.y, placement: 'bottom-start', label: 'Message actions', role: 'dialog', className: 'popover-menu msgmenu-pop' });
  }
  const first = content.querySelector('.menu-item');
  if (first) /** @type {HTMLElement} */ (first).focus({ preventScroll: true });
  return { close };
}

/**
 * Menu of a pending (outbox) bubble: Retry (failed ones), Copy text, Discard.
 * @param {any} item OutboxItem
 * @param {{anchor?: Element, x?: number, y?: number}} [opts]
 * @returns {{close: () => void}}
 */
export function openPendingMenu(item, opts = {}) {
  const entries = [];
  if (item.state === 'failed') entries.push({ label: 'Retry', icon: 'refresh', onSelect: () => outbox.retry(item.client_id) });
  if (item.body && item.body.trim() !== '') entries.push({ label: 'Copy text', icon: 'copy', onSelect: () => copyBody(item.body) });
  entries.push({ label: 'Discard', icon: 'trash', danger: true, onSelect: () => outbox.discard(item.client_id) });
  return ui.menu(entries, { anchor: opts.anchor, x: opts.x, y: opts.y, label: 'Unsent message actions' });
}

/* ------------------------------------------------------------------------------------------ */
/* Info                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string} title
 * @param {Array<{user_id: number, at: number|null}>} rows
 * @returns {HTMLElement|null}
 */
function infoSection(title, rows) {
  if (!rows.length) return null;
  return h('section.msginfo-section', { 'aria-label': title },
    h('h3.msginfo-title', title),
    h('ul.msginfo-list', rows.map((r) => h('li.msginfo-row',
      userAvatar(r.user_id, { size: 'sm', online: null }),
      h('span.msginfo-name.truncate', { dir: 'auto' }, r.user_id === (store.me ? store.me.id : -1) ? 'You' : store.userName(r.user_id)),
      r.at ? h('time.msginfo-time.muted', { title: formatDateTime(r.at) }, formatDateTime(r.at)) : null))));
}

/**
 * Reactions section: names per emoji (the touch replacement of the chip tooltip).
 * @param {any} message
 * @returns {HTMLElement|null}
 */
function reactionsSection(message) {
  const rs = message.reactions || [];
  if (!rs.length) return null;
  const meId = store.me ? store.me.id : -1;
  return h('section.msginfo-section', { 'aria-label': 'Reactions' },
    h('h3.msginfo-title', 'Reactions'),
    h('ul.msginfo-list', rs.flatMap((r) => r.user_ids.map((id) => h('li.msginfo-row',
      h('span.msginfo-emoji', r.emoji),
      h('span.msginfo-name.truncate', { dir: 'auto' }, id === meId ? 'You' : store.userName(id)))))));
}

/**
 * Info sheet / dialog: who received and who read an own message (msg.info), with times, plus reactions.
 * @param {any} message
 * @param {any} [chat]
 * @returns {{close: () => void}}
 */
export function openMessageInfo(message, chat) {
  const body = h('div.msginfo',
    h('p.msginfo-preview', { dir: 'auto' }, store.summarize(message), h('span.muted', ` · ${formatTime(message.created_at)}`)),
    h('div.msginfo-body', ui.spinner(22)));
  /** @type {{close: () => void}} */
  let host;
  if (ui.prefersSheet()) host = ui.sheet(body, { title: 'Message info' });
  else {
    const d = ui.dialog({ title: 'Message info', size: 'sm', content: body, actions: [{ label: 'Close' }] });
    host = { close: () => d.close() };
  }
  const target = /** @type {HTMLElement} */ (body.querySelector('.msginfo-body'));
  const meChat = chat || store.getChat(message.chat_id);
  store.request('msg.info', { message_id: message.id }).then((res) => {
    const recipients = Array.isArray(res.recipients) ? res.recipients : [];
    const read = recipients.filter((r) => r.read).map((r) => ({ user_id: r.user_id, at: r.read_at }));
    const delivered = recipients.filter((r) => r.delivered && !r.read).map((r) => ({ user_id: r.user_id, at: r.delivered_at }));
    const pending = recipients.filter((r) => !r.delivered).map((r) => ({ user_id: r.user_id, at: null }));
    const direct = meChat && meChat.kind === 'direct';
    const sections = [
      infoSection(direct ? 'Read' : 'Read by', read),
      infoSection('Delivered to', delivered),
      infoSection('Not delivered yet', pending),
      reactionsSection(res.message || message),
    ].filter(Boolean);
    target.replaceChildren(...(sections.length ? sections : [h('p.muted', 'No recipients yet.')]));
  }, (err) => {
    target.replaceChildren(h('p.error-text', { role: 'alert' }, errorText(err, 'Could not load the message info.')));
  });
  return host;
}
