/**
 * core/avatar.js - initials avatars with colours hashed from the id, and sender-name colours
 * (12-colour palette defined in css/base.css as --av-N / --sc-N, SPEC 9.8).
 *
 * Exported API (docs/ui-core-api.md section 4): initials, colorIndex, avatar, userAvatar,
 * chatAvatar, setAvatarOnline, senderClass.
 */

import { h } from './dom.js';
import { icon } from './icons.js';
import { store } from './store.js';

const PALETTE_SIZE = 12;
const SIZES = { xs: 12, sm: 16, md: 20, lg: 28, xl: 44 };

/**
 * Up to two initials: the first letter of the first two words (code-point safe). Words without
 * letters or digits (emoji, punctuation) are skipped; an emoji-only name shows its first emoji.
 * @param {string} name
 * @returns {string} "?" when the name is empty
 */
export function initials(name) {
  const text = String(name == null ? '' : name).trim();
  // words that contain a letter or digit; their first letter/digit is the initial
  const letters = text.split(/\s+/)
    .filter((w) => /[\p{L}\p{N}]/u.test(w))
    .map((w) => Array.from(w.replace(/^[^\p{L}\p{N}]+/u, ''))[0]);
  if (letters.length) return (letters[0] + (letters.length > 1 ? letters[1] : '')).toUpperCase();
  return Array.from(text)[0] || '?'; // emoji-only names show the emoji
}

/**
 * Stable palette slot (0..11) for a numeric or string id.
 * @param {number|string} id
 * @returns {number}
 */
export function colorIndex(id) {
  if (typeof id === 'number' && Number.isFinite(id)) {
    return (((Math.abs(Math.trunc(id)) * 7 + 3) % PALETTE_SIZE) + PALETTE_SIZE) % PALETTE_SIZE;
  }
  // FNV-1a for strings
  let hash = 0x811c9dc5;
  for (const ch of String(id)) {
    hash ^= ch.codePointAt(0) || 0;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % PALETTE_SIZE;
}

/**
 * CSS class that colours a sender name.
 * @param {number|string} userId
 * @returns {string} e.g. "sender-c5"
 */
export function senderClass(userId) {
  return `sender-c${colorIndex(userId)}`;
}

/**
 * Build an avatar element.
 * @param {{id: number|string, name?: string, group?: boolean}} subject
 * @param {{size?: 'xs'|'sm'|'md'|'lg'|'xl', online?: boolean|null, className?: string}} [opts]
 *        online true/false adds the presence dot (CSS shows it only for `.on`); null/undefined: none
 * @returns {HTMLElement}
 */
export function avatar(subject, opts = {}) {
  const { size = 'md', online = null, className = '' } = opts;
  const content = subject.group ? icon('group', { size: SIZES[size] || 20 }) : initials(subject.name || '');
  return h(`span.avatar.av-${size}.av-c${colorIndex(subject.id)}${className ? '.' + className.split(/\s+/).join('.') : ''}`,
    { 'aria-hidden': 'true' }, content,
    online === null || online === undefined ? null : h(`span.av-dot${online ? '.on' : ''}`));
}

/**
 * Avatar of a directory user.
 * @param {object|number} userOrId a User or a user id
 * @param {{size?: string, online?: boolean|null, className?: string}} [opts]
 * @returns {HTMLElement}
 */
export function userAvatar(userOrId, opts = {}) {
  const u = typeof userOrId === 'number' ? store.getUser(userOrId) : userOrId;
  const id = typeof userOrId === 'number' ? userOrId : (u && u.id) || 0;
  const online = opts.online !== undefined ? opts.online : (u ? Boolean(u.online) : null);
  return avatar({ id, name: u ? u.display_name : '?' }, { ...opts, online: /** @type {any} */ (online) });
}

/**
 * Avatar of a chat: the peer for direct chats (yourself for the self-chat), a group glyph for groups.
 * @param {object|number} chatOrId a Chat or a chat id
 * @param {{size?: string, online?: boolean|null, className?: string}} [opts]
 * @returns {HTMLElement}
 */
export function chatAvatar(chatOrId, opts = {}) {
  const chat = typeof chatOrId === 'number' ? store.getChat(chatOrId) : chatOrId;
  if (!chat) return avatar({ id: typeof chatOrId === 'number' ? chatOrId : 0, name: '?' }, opts);
  if (chat.kind === 'group') return avatar({ id: chat.id, name: chat.title || '', group: true }, { ...opts, online: null });
  const peer = store.chatPeer(chat);
  return avatar({ id: chat.peer_id ?? chat.id, name: peer ? peer.display_name : '?' },
    { ...opts, online: opts.online !== undefined ? opts.online : (peer ? Boolean(peer.online) : null) });
}

/**
 * Toggle the presence dot of an avatar element without re-rendering it.
 * @param {HTMLElement} el an element created by avatar()
 * @param {boolean} online
 */
export function setAvatarOnline(el, online) {
  let dot = el.querySelector('.av-dot');
  if (!dot) {
    dot = h('span.av-dot');
    el.appendChild(dot);
  }
  dot.classList.toggle('on', Boolean(online));
}
