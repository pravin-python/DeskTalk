/**
 * views/messageview.js - renders ONE message: the live-list bubble (patched in place), the system row,
 * date separators and the unread divider, the outbox (pending) bubble, plus the two renderers other
 * screens use: the Starred card and the search-result row (docs/ui-conv-api.md section 1).
 *
 * Rows carry no per-row listeners: `bindMessageActions(root, env)` installs ONE set of delegated handlers
 * (click, context menu, long press, keyboard) on a container, so a 400-row window stays cheap.
 *
 * Patching: every row keeps a signature per part (quote, media, text, footer, status, reactions); a patch
 * rebuilds only the parts whose signature changed, so an audio/video player is never recreated by a tick.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { ui } from '../core/ui.js';
import { senderClass, userAvatar, chatAvatar } from '../core/avatar.js';
import { formatTime, formatDateTime, formatDayLabel, formatBytes, formatDuration, cpLength } from '../core/util.js';
import { renderRichText, highlightText, makeSnippet, emojiOnlyCount } from '../lib/richtext.js';
import { openMessageMenu, reactTo } from './messagemenu.js';
import { openLightbox, lightboxItemFromMessage } from './lightbox.js';

const MEDIA_MAX_W = 320;
const MEDIA_MAX_H = 360;
const MEDIA_MIN_W = 48;
const PLACEHOLDER_W = 240;
const PLACEHOLDER_H = 180;
const COLLAPSE_CHARS = 1200;
const COLLAPSE_LINES = 20;
const RUN_GAP_S = 300;
const PART_ORDER = ['sender', 'forwarded', 'quote', 'media', 'text', 'pend', 'footer', 'reactions'];

/**
 * Per-row bookkeeping.
 * @typedef {object} RowState
 * @property {Record<string, string>} sig signature per part
 * @property {Record<string, HTMLElement|null>} parts
 * @property {HTMLElement} bubble
 * @property {boolean} expanded the "Read more" state survives patches
 * @property {Array<() => void>} dispose
 */

/** @type {WeakMap<HTMLElement, RowState>} */
const states = new WeakMap();
/** @type {HTMLAudioElement|null} */
let playingAudio = null;

/**
 * Environment of a rendering surface.
 * @typedef {object} ViewEnv
 * @property {'list'|'card'} context
 * @property {() => any} getChat the Chat the messages belong to (may be a different object over time)
 * @property {string} [highlight] in-chat search term
 * @property {(id: number) => boolean} [isEager] load images immediately (newest messages)
 */

/* ------------------------------------------------------------------------------------------ */
/* Small helpers                                                                              */
/* ------------------------------------------------------------------------------------------ */

/**
 * Size an image/video box: longest sides clamped to 320 x 360 CSS px (SPEC 9.6 rule 5).
 * @param {number|null|undefined} w
 * @param {number|null|undefined} hgt
 * @returns {{w: number, h: number, known: boolean}}
 */
export function fitSize(w, hgt) {
  if (!(Number(w) > 0 && Number(hgt) > 0)) return { w: PLACEHOLDER_W, h: PLACEHOLDER_H, known: false };
  const s = Math.min(1, MEDIA_MAX_W / Number(w), MEDIA_MAX_H / Number(hgt));
  const width = Math.max(MEDIA_MIN_W, Math.round(Number(w) * s));
  return { w: width, h: Math.round(width * (Number(hgt) / Number(w))), known: true };
}

/**
 * Apply a box size to a media wrapper (dynamic values go through the style object, never markup).
 * @param {HTMLElement} el
 * @param {{w: number, h: number}} size
 */
function applySize(el, size) {
  el.style.width = `${size.w}px`;
  el.style.aspectRatio = `${size.w} / ${size.h}`;
}

/**
 * Resolver for `@username` tokens: a current member of the chat (or any directory user without a chat).
 * @param {any} chat
 * @returns {(username: string) => ({user_id: number, name: string, self: boolean}|null)}
 */
export function makeMentionResolver(chat) {
  /** @type {Map<string, any>|null} */
  let map = null;
  return (username) => {
    if (!map) {
      map = new Map();
      const ids = chat && Array.isArray(chat.members) ? chat.members.map((m) => m.user_id) : store.users().map((u) => u.id);
      for (const id of ids) {
        const u = store.getUser(id);
        if (u && u.username) map.set(String(u.username).toLowerCase(), u);
      }
    }
    const u = map.get(String(username).toLowerCase());
    return u ? { user_id: u.id, name: u.display_name, self: Boolean(store.me && u.id === store.me.id) } : null;
  };
}

/**
 * Text of a system message composed from its structured payload (names follow profile changes).
 * @param {any} m a system Message
 * @returns {string}
 */
export function systemText(m) {
  const s = m.system;
  const meId = store.me ? store.me.id : -1;
  if (!s) return m.body || '';
  const who = (id, subject) => (id === meId ? (subject ? 'You' : 'you') : store.userName(id));
  const actor = s.actor_id === null || s.actor_id === undefined ? null : who(s.actor_id, true);
  const targets = (s.target_ids || []).map((id) => who(id, false));
  const list = targets.length > 1 ? `${targets.slice(0, -1).join(', ')} and ${targets[targets.length - 1]}` : targets[0] || '';
  const first = (t) => t.charAt(0).toUpperCase() + t.slice(1);
  switch (s.event) {
    case 'added': return actor ? `${actor} added ${list}` : `${first(list)} was added`;
    case 'removed': return actor ? `${actor} removed ${list}` : `${first(list)} was removed`;
    case 'left': return `${actor || first(list)} left`;
    case 'joined': return `${first(list || actor || 'Someone')} joined`;
    case 'renamed': return `${actor || 'Someone'} changed the group name to "${s.title || ''}"`;
    case 'promoted': return `${first(list)} ${targets[0] === 'you' ? 'are' : 'is'} now an admin`;
    case 'demoted': return `${first(list)} ${targets[0] === 'you' ? 'are' : 'is'} no longer an admin`;
    case 'pinned': return `${actor || 'Someone'} pinned a message`;
    case 'created': return m.body || `${actor || 'Someone'} created this group`;
    default: return m.body || '';
  }
}

/**
 * Two messages belong to one "run" (no repeated sender name / avatar, tight spacing) when the same
 * person wrote both within five minutes on the same day.
 * @param {any} a earlier message (or pending pseudo message)
 * @param {any} b later message
 * @returns {boolean}
 */
export function isSameRun(a, b) {
  if (!a || !b || a.kind === 'system' || b.kind === 'system' || a.sender_id !== b.sender_id) return false;
  if (Math.abs(b.created_at - a.created_at) > RUN_GAP_S) return false;
  const da = new Date(a.created_at * 1000);
  const db = new Date(b.created_at * 1000);
  return da.getFullYear() === db.getFullYear() && da.getMonth() === db.getMonth() && da.getDate() === db.getDate();
}

/**
 * @param {HTMLElement} row
 * @param {boolean} cont continues the previous message's run
 */
export function setRunContinuation(row, cont) {
  row.classList.toggle('run-cont', cont);
  row.classList.toggle('run-first', !cont);
}

/**
 * @param {number} ts
 * @returns {HTMLElement} the date separator that precedes the first message of a day
 */
export function createDateSeparator(ts) {
  return h('div.msg-date', { role: 'separator', dataset: { ts: String(ts) } }, h('span', formatDayLabel(ts)));
}

/**
 * @param {number} count unread messages from the divider on
 * @returns {HTMLElement}
 */
export function createUnreadDivider(count) {
  return h('div.msg-unread', { role: 'separator', id: 'msg-unread-divider' }, h('span', unreadLabel(count)));
}

/**
 * @param {number} count
 * @returns {string}
 */
export function unreadLabel(count) {
  return `${count > 999 ? '999+' : count} unread ${count === 1 ? 'message' : 'messages'}`;
}

/* ------------------------------------------------------------------------------------------ */
/* Parts                                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string} kind
 * @returns {string} icon name for an attachment kind
 */
function kindIcon(kind) {
  return kind === 'image' ? 'image' : kind === 'video' ? 'video' : kind === 'audio' ? 'volume' : 'file';
}

/**
 * Preview line of a quoted message.
 * @param {any} q a reply_to object
 * @returns {{text: string, italic: boolean, icon: string|null}}
 */
function quotePreview(q) {
  if (q.unavailable) return { text: 'Message unavailable', italic: true, icon: null };
  if (q.deleted) return { text: 'This message was deleted', italic: true, icon: null };
  const body = String(q.body || '').trim();
  const att = q.kind && q.kind !== 'text' && q.kind !== 'system';
  if (att) {
    const label = body || (q.kind === 'image' ? 'Photo' : q.kind === 'video' ? 'Video' : q.kind === 'audio' ? 'Audio' : q.attachment_name || 'File');
    return { text: label, italic: false, icon: kindIcon(q.kind) };
  }
  return { text: body, italic: false, icon: null };
}

/**
 * @param {any} m
 * @param {ViewEnv} env
 * @returns {HTMLElement|null}
 */
function buildQuote(m, env) {
  const q = m.reply_to;
  if (!q || m.deleted) return null;
  const prev = quotePreview(q);
  const meId = store.me ? store.me.id : -1;
  const name = q.unavailable ? '' : q.sender_id === meId ? 'You' : q.sender_id === null ? '' : store.userName(q.sender_id);
  return h('button.msg-quote', {
    type: 'button',
    disabled: Boolean(q.unavailable),
    dataset: { replyTo: String(q.id) },
    'aria-label': q.unavailable ? 'Quoted message is unavailable' : 'Go to the original message',
  },
  h(`span.msg-quote-bar${q.sender_id !== null && !q.unavailable ? '.' + senderClass(q.sender_id) : ''}`),
  h('span.msg-quote-body',
    name ? h(`span.msg-quote-sender.${q.sender_id === meId ? 'me' : senderClass(q.sender_id)}`, name) : null,
    h(`span.msg-quote-text${prev.italic ? '.italic' : ''}`, { dir: 'auto' }, prev.icon ? icon(prev.icon, { size: 14 }) : null, prev.text)));
}

/**
 * Circular progress overlay of an uploading attachment.
 * @param {any} item OutboxItem
 * @returns {HTMLElement}
 */
function buildProgress(item) {
  const el = h('span.msg-progress', { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' },
    h('button.msg-cancel', { type: 'button', 'aria-label': 'Cancel upload', dataset: { cid: item.client_id } }, icon('close', { size: 18 })));
  updateProgress(el, item);
  return el;
}

/**
 * @param {HTMLElement} el
 * @param {any} item
 */
function updateProgress(el, item) {
  const pct = Math.max(0, Math.min(100, Math.round((item.progress || 0) * 100)));
  el.style.setProperty('--p', `${pct}%`);
  el.setAttribute('aria-valuenow', String(pct));
  el.setAttribute('aria-label', `Uploading ${pct}%`);
}

/**
 * File card with a download link (also the fallback of a failed player).
 * @param {any} m
 * @param {any} att
 * @param {any} item pending OutboxItem or null
 * @returns {HTMLElement}
 */
function buildFileCard(m, att, item) {
  const isPdf = /\.pdf$/i.test(att.name || '') || att.mime === 'application/pdf';
  const url = att.id ? `/files/${att.id}?dl=1` : null;
  const ext = (/\.([A-Za-z0-9]{1,5})$/.exec(att.name || '') || [])[1];
  return h('div.msg-file',
    h('span.msg-file-icon', icon(kindIcon(att.kind), { size: 28 }), ext ? h('span.msg-file-ext', ext.toUpperCase()) : null),
    h('span.msg-file-info',
      h('span.msg-file-name', { dir: 'auto' }, att.name || 'File'),
      h('span.msg-file-meta', [formatBytes(att.size), att.duration ? formatDuration(att.duration) : ''].filter(Boolean).join(' · '))),
    url
      ? h('a.msg-file-dl.btn-icon', { href: url, download: att.name || '', 'aria-label': `${isPdf ? 'Download PDF' : 'Download'} ${att.name || 'file'}`, title: 'Download' }, icon('download', { size: 22 }))
      : null,
    item && item.state === 'uploading' ? buildProgress(item) : null);
}

/**
 * Voice-note / audio player (SPEC 9.2): play/pause, seek bar, time.
 * @param {any} att
 * @param {RowState} st
 * @param {() => void} onError
 * @returns {HTMLElement}
 */
function buildAudio(att, st, onError) {
  const audio = h('audio', { preload: 'none', src: att.url });
  const fill = h('span.msg-audio-fill');
  const time = h('span.msg-audio-time', formatDuration(att.duration || 0));
  const playIcon = () => icon('play', { size: 22 });
  const btn = h('button.msg-audio-btn', { type: 'button', 'aria-label': 'Play' }, playIcon());
  const track = h('span.msg-audio-track', {
    role: 'slider', tabIndex: 0, 'aria-label': 'Seek', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': '0',
  }, fill);
  const duration = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : Number(att.duration) || 0);
  const paint = () => {
    const d = duration();
    const ratio = d > 0 ? Math.min(1, audio.currentTime / d) : 0;
    fill.style.width = `${(ratio * 100).toFixed(1)}%`;
    track.setAttribute('aria-valuenow', String(Math.round(ratio * 100)));
    time.textContent = formatDuration(audio.paused && audio.currentTime === 0 ? d : audio.currentTime);
  };
  const setPlaying = (playing) => {
    btn.replaceChildren(playing ? icon('pause', { size: 22 }) : playIcon());
    btn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
  };
  btn.addEventListener('click', () => {
    if (audio.paused) {
      if (playingAudio && playingAudio !== audio) playingAudio.pause();
      const p = audio.play();
      if (p && typeof p.catch === 'function') p.catch(() => onError());
    } else {
      audio.pause();
    }
  });
  audio.addEventListener('play', () => {
    playingAudio = audio;
    setPlaying(true);
  });
  audio.addEventListener('pause', () => {
    if (playingAudio === audio) playingAudio = null;
    setPlaying(false);
    paint();
  });
  audio.addEventListener('ended', () => {
    audio.currentTime = 0;
    setPlaying(false);
    paint();
  });
  audio.addEventListener('timeupdate', paint);
  audio.addEventListener('loadedmetadata', paint);
  audio.addEventListener('error', onError);
  const seekTo = (ratio) => {
    const d = duration();
    if (d > 0) {
      audio.currentTime = Math.max(0, Math.min(d, ratio * d));
      paint();
    }
  };
  track.addEventListener('pointerdown', (e) => {
    const r = track.getBoundingClientRect();
    if (r.width > 0) seekTo((e.clientX - r.left) / r.width);
  });
  track.addEventListener('keydown', (e) => {
    const delta = e.key === 'ArrowRight' ? 5 : e.key === 'ArrowLeft' ? -5 : 0;
    if (!delta) return;
    e.preventDefault();
    const d = duration();
    const next = audio.currentTime + delta;
    audio.currentTime = Math.max(0, d > 0 ? Math.min(d, next) : next);
    paint();
  });
  st.dispose.push(() => {
    audio.pause();
    audio.removeAttribute('src');
  });
  return h('div.msg-audio', btn, h('span.msg-audio-main', track, h('span.msg-audio-name.muted', { dir: 'auto' }, /^voice[-_ ]?note/i.test(att.name || '') ? '' : att.name || '')), time, audio);
}

/**
 * Media slot of a message: image, video, audio or file card. A player that fails falls back to the
 * file card with a Download link (SPEC 9.9(6)).
 * @param {any} m
 * @param {ViewEnv} env
 * @param {RowState} st
 * @param {any} item pending OutboxItem or null
 * @returns {HTMLElement|null}
 */
function buildMedia(m, env, st, item) {
  const att = m.attachment;
  if (m.deleted) return null;
  if (!att) {
    return m.kind !== 'text' && m.kind !== 'system' ? h('div.msg-media-error.muted', 'Attachment unavailable') : null;
  }
  const holder = h('div.msg-media', { dataset: { kind: att.kind } });
  const fallback = () => holder.replaceChildren(buildFileCard(m, att, item));
  if (att.kind === 'image') {
    const size = fitSize(att.width, att.height);
    const eager = env.context === 'list' && env.isEager ? env.isEager(m.id) || Boolean(item) : false;
    const img = h('img', { src: att.url, alt: att.name || 'Image', decoding: 'async', loading: eager ? 'eager' : 'lazy', draggable: false });
    const box = h('button.msg-image', { type: 'button', 'aria-label': `Open ${att.name || 'image'}`, dataset: { mediaId: String(m.id) } }, img);
    applySize(box, size);
    img.addEventListener('load', () => {
      if (!size.known && img.naturalWidth > 0) applySize(box, fitSize(img.naturalWidth, img.naturalHeight));
    });
    img.addEventListener('error', () => {
      holder.replaceChildren(h('div.msg-media-error', icon('image', { size: 20 }), h('span', 'Image unavailable'),
        att.id ? h('a.msg-link', { href: `/files/${att.id}?dl=1`, download: att.name || '' }, 'Download') : null));
    });
    holder.appendChild(box);
    if (item && item.state === 'uploading') box.appendChild(buildProgress(item));
  } else if (att.kind === 'video') {
    const size = fitSize(att.width, att.height);
    const video = h('video', { controls: true, preload: 'metadata', playsinline: true, src: att.url ? `${att.url}#t=0.1` : null });
    const box = h('div.msg-video', video);
    applySize(box, size);
    video.addEventListener('error', fallback);
    video.addEventListener('loadedmetadata', () => {
      if (!size.known && video.videoWidth > 0) applySize(box, fitSize(video.videoWidth, video.videoHeight));
    });
    st.dispose.push(() => {
      video.pause();
      video.removeAttribute('src');
    });
    holder.appendChild(box);
    if (item && item.state === 'uploading') box.appendChild(buildProgress(item));
  } else if (att.kind === 'audio' && att.url) {
    holder.appendChild(buildAudio(att, st, fallback));
    if (item && item.state === 'uploading') holder.appendChild(buildProgress(item));
  } else {
    holder.appendChild(buildFileCard(m, att, item));
  }
  return holder;
}

/**
 * @param {any} m
 * @param {ViewEnv} env
 * @param {RowState} st
 * @returns {HTMLElement|null}
 */
function buildText(m, env, st) {
  if (m.deleted) {
    const own = store.me && m.sender_id === store.me.id;
    return h('div.msg-text.msg-deleted', icon('info', { size: 16 }), own ? 'You deleted this message' : 'This message was deleted');
  }
  const body = String(m.body || '');
  if (body === '') return null;
  const emoji = emojiOnlyCount(body);
  const long = cpLength(body) > COLLAPSE_CHARS || body.split('\n').length > COLLAPSE_LINES;
  const el = h('div.msg-text', {
    dir: 'auto',
    class: [emoji > 0 && emoji <= 3 ? 'emoji-big' : emoji > 3 && emoji <= 6 ? 'emoji-mid' : '', long && !st.expanded ? 'collapsed' : ''],
  }, renderRichText(body, { highlight: env.highlight || '', resolveMention: makeMentionResolver(env.getChat()) }));
  if (!long) return el;
  const wrap = h('div.msg-text-wrap', el, h('button.msg-readmore', { type: 'button', 'aria-expanded': String(st.expanded) }, st.expanded ? 'Show less' : 'Read more'));
  return wrap;
}

/**
 * Tick / clock / failed marker of an own message.
 * @param {any} m
 * @param {any} item pending OutboxItem or null
 * @returns {HTMLElement|null}
 */
function buildTick(m, item) {
  if (item) {
    if (item.state === 'failed') return h('span.tick.tick-failed', { role: 'img', 'aria-label': 'Failed to send' }, icon('error', { size: 16 }));
    return h('span.tick.tick-pending', { role: 'img', 'aria-label': 'Sending' }, icon('clock', { size: 14 }));
  }
  if (!m.status || !store.me || m.sender_id !== store.me.id) return null;
  const label = m.status === 'read' ? 'Read' : m.status === 'delivered' ? 'Delivered' : 'Sent';
  return h(`span.tick.tick-${m.status}`, { role: 'img', 'aria-label': label }, icon(m.status === 'sent' ? 'check' : 'checks', { size: 16 }));
}

/**
 * Footer: star / pin markers, "edited", time and ticks.
 * @param {any} m
 * @param {any} item
 * @returns {HTMLElement}
 */
function buildFooter(m, item) {
  const ts = item ? item.created_at : m.created_at;
  const parts = [];
  if (m.starred && !m.deleted) parts.push(h('span.msg-flag', { title: 'Starred' }, icon('star', { size: 12 }), h('span.sr-only', 'Starred')));
  if (m.pinned && !m.deleted) parts.push(h('span.msg-flag', { title: 'Pinned' }, icon('pin', { size: 12 }), h('span.sr-only', 'Pinned')));
  if (m.edited_at && !m.deleted) parts.push(h('span.msg-edited', 'edited'));
  parts.push(h('time', { dateTime: new Date(ts * 1000).toISOString(), title: formatDateTime(ts) }, formatTime(ts)));
  const tick = buildTick(m, item);
  if (tick) parts.push(tick);
  return h('div.msg-footer', parts);
}

/**
 * Tooltip text of a reaction chip: who reacted.
 * @param {{emoji: string, user_ids: number[]}} r
 * @returns {string}
 */
function reactionTitle(r) {
  const meId = store.me ? store.me.id : -1;
  const names = r.user_ids.map((id) => (id === meId ? 'You' : store.userName(id)));
  const shown = names.slice(0, 8).join(', ');
  return `${r.emoji} ${shown}${names.length > 8 ? ` and ${names.length - 8} more` : ''}`;
}

/**
 * @param {any} m
 * @returns {HTMLElement|null}
 */
function buildReactions(m) {
  if (m.deleted || !Array.isArray(m.reactions) || m.reactions.length === 0) return null;
  const meId = store.me ? store.me.id : -1;
  return h('div.msg-reactions', { role: 'group', 'aria-label': 'Reactions' }, m.reactions.map((r) => {
    const mine = r.user_ids.includes(meId);
    return h(`button.msg-reaction${mine ? '.mine' : ''}`, {
      type: 'button',
      title: reactionTitle(r),
      'aria-pressed': String(mine),
      'aria-label': `${r.emoji} ${r.user_ids.length} ${r.user_ids.length === 1 ? 'reaction' : 'reactions'}${mine ? ', including yours' : ''}`,
      dataset: { emoji: r.emoji },
    }, h('span.msg-reaction-emoji', r.emoji), r.user_ids.length > 1 ? h('span.msg-reaction-count', String(r.user_ids.length)) : null);
  }));
}

/**
 * @param {any} m
 * @param {ViewEnv} env
 * @returns {HTMLElement|null}
 */
function buildSender(m, env) {
  const chat = env.getChat();
  if (env.context !== 'list' || !chat || chat.kind !== 'group' || m.kind === 'system') return null;
  if (!store.me || m.sender_id === store.me.id || m.sender_id === null) return null;
  return h(`div.msg-sender.${senderClass(m.sender_id)}`, { dir: 'auto' }, store.userName(m.sender_id),
    h('span.msg-sender-user.muted', `@${(store.getUser(m.sender_id) || {}).username || ''}`));
}

/**
 * Pending-state part: only the failed note (the clock lives in the footer, the ring on the media).
 * @param {any} item
 * @returns {HTMLElement|null}
 */
function buildPend(item) {
  if (!item || item.state !== 'failed') return null;
  return h('div.msg-failed', { role: 'alert' },
    h('span', item.error && item.error.message ? item.error.message : 'Could not send the message.'),
    h('button.msg-retry', { type: 'button', dataset: { cid: item.client_id } }, icon('refresh', { size: 14 }), 'Retry'));
}

/* ------------------------------------------------------------------------------------------ */
/* Signatures                                                                                 */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {any} m
 * @param {ViewEnv} env
 * @param {any} item pending OutboxItem or null
 * @returns {Record<string, string>}
 */
function signatures(m, env, item) {
  const q = m.reply_to;
  const a = m.attachment;
  const meId = store.me ? store.me.id : -1;
  const chat = env.getChat();
  return {
    sender: `${chat ? chat.kind : ''}|${m.sender_id}|${m.sender_id !== null ? (store.getUser(m.sender_id) || {}).display_name : ''}`,
    forwarded: m.forwarded && !m.deleted ? '1' : '',
    quote: q && !m.deleted ? [q.id, q.sender_id, q.kind, q.body, q.attachment_name, q.deleted, q.unavailable, store.userName(q.sender_id)].join('\u0001') : '',
    media: m.deleted ? 'deleted' : a ? [a.id, a.url, a.kind, a.width, a.height, a.size, a.name, item ? item.state === 'uploading' : false].join('\u0001') : `none:${m.kind}`,
    text: m.deleted ? `deleted:${meId === m.sender_id}` : [m.body, (m.mentions || []).join(','), env.highlight || '', chat ? chat.id : 0].join('\u0001'),
    pend: item && item.state === 'failed' ? `failed:${item.client_id}:${item.error ? item.error.message : ''}` : '',
    footer: [m.edited_at, m.starred, m.pinned, m.created_at, item ? item.state : '', m.status, m.deleted].join('\u0001'),
    reactions: m.deleted ? '' : JSON.stringify(m.reactions || []),
  };
}

/**
 * Build one part by name.
 * @param {string} name
 * @param {any} m
 * @param {ViewEnv} env
 * @param {RowState} st
 * @param {any} item
 * @returns {HTMLElement|null}
 */
function buildPart(name, m, env, st, item) {
  switch (name) {
    case 'sender': return buildSender(m, env);
    case 'forwarded': return m.forwarded && !m.deleted ? h('div.msg-forwarded', icon('forward', { size: 14 }), 'Forwarded') : null;
    case 'quote': return buildQuote(m, env);
    case 'media': return buildMedia(m, env, st, item);
    case 'text': return buildText(m, env, st);
    case 'pend': return buildPend(item);
    case 'footer': return buildFooter(m, item);
    case 'reactions': return buildReactions(m);
    default: return null;
  }
}

/**
 * Insert/replace/remove one part keeping the fixed order.
 * @param {RowState} st
 * @param {string} name
 * @param {HTMLElement|null} el
 */
function placePart(st, name, el) {
  const old = st.parts[name];
  if (old && el) {
    old.replaceWith(el);
  } else if (old) {
    old.remove();
  } else if (el) {
    let before = null;
    for (let i = PART_ORDER.indexOf(name) + 1; i < PART_ORDER.length && !before; i += 1) before = st.parts[PART_ORDER[i]] || null;
    st.bubble.insertBefore(el, before);
  }
  st.parts[name] = el;
}

/* ------------------------------------------------------------------------------------------ */
/* Rows                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * Build the row of a server message (or of an outbox pseudo message when `item` is given).
 * @param {any} m
 * @param {ViewEnv} env
 * @param {any} [item] OutboxItem for pending rows
 * @returns {HTMLElement}
 */
export function createRow(m, env, item = null) {
  if (m.kind === 'system') return createSystemRow(m);
  const mine = Boolean(store.me && m.sender_id === store.me.id);
  const st = /** @type {RowState} */ ({ sig: {}, parts: {}, bubble: h('div.bubble'), expanded: false, dispose: [] });
  const chat = env.getChat();
  const avatarSlot = env.context === 'list' && chat && chat.kind === 'group' && !mine && m.sender_id !== null
    ? h('span.msg-avatar', userAvatar(m.sender_id, { size: 'sm', online: null }))
    : null;
  const row = h(`div.msg-row.${mine ? 'out' : 'in'}`, {
    tabIndex: -1,
    dataset: item ? { cid: item.client_id } : { id: String(m.id) },
    class: [item ? 'pending' : '', item && item.state === 'failed' ? 'failed' : '', m.deleted ? 'deleted' : ''],
  }, avatarSlot, st.bubble, h('button.msg-more.btn-icon', { type: 'button', 'aria-label': 'Message options', tabIndex: -1 }, icon('chevron-down', { size: 18 })));
  states.set(row, st);
  const sig = signatures(m, env, item);
  for (const name of PART_ORDER) {
    const el = buildPart(name, m, env, st, item);
    st.parts[name] = el;
    if (el) st.bubble.appendChild(el);
    st.sig[name] = sig[name];
  }
  if (item && item.state === 'uploading' && st.parts.media) updateMediaProgress(st, item);
  return row;
}

/**
 * @param {RowState} st
 * @param {any} item
 */
function updateMediaProgress(st, item) {
  const ring = st.parts.media && st.parts.media.querySelector('.msg-progress');
  if (ring) updateProgress(/** @type {HTMLElement} */ (ring), item);
}

/**
 * Bring an existing row up to date with `m`, rebuilding only the parts that changed.
 * @param {HTMLElement} row
 * @param {any} m
 * @param {ViewEnv} env
 * @param {any} [item]
 * @returns {HTMLElement} the same row (or a fresh one when its kind changed)
 */
export function patchRow(row, m, env, item = null) {
  if (row.classList.contains('system')) {
    patchSystemRow(row, m);
    return row;
  }
  const st = states.get(row);
  if (!st) return row;
  const sig = signatures(m, env, item);
  const mediaChanged = sig.media !== st.sig.media;
  for (const name of PART_ORDER) {
    if (sig[name] === st.sig[name]) continue;
    if (name === 'media') for (const fn of st.dispose.splice(0)) fn();
    placePart(st, name, buildPart(name, m, env, st, item));
    st.sig[name] = sig[name];
  }
  if (!mediaChanged && item && item.state === 'uploading') updateMediaProgress(st, item);
  row.classList.toggle('deleted', Boolean(m.deleted));
  row.classList.toggle('failed', Boolean(item && item.state === 'failed'));
  return row;
}

/**
 * Release media resources of a row that is leaving the DOM.
 * @param {HTMLElement} row
 */
export function disposeRow(row) {
  const st = states.get(row);
  if (!st) return;
  for (const fn of st.dispose.splice(0)) fn();
}

/**
 * Centred system message. A "pinned" notice jumps to the pinned message when it is clicked.
 * @param {any} m
 * @returns {HTMLElement}
 */
export function createSystemRow(m) {
  const pinned = m.system && m.system.event === 'pinned' && m.system.message_id;
  const label = systemText(m);
  const inner = pinned
    ? h('button.msg-system', { type: 'button', dataset: { jump: String(m.system.message_id) } }, label)
    : h('span.msg-system', label);
  return h('div.msg-row.system', { dataset: { id: String(m.id) }, tabIndex: -1, role: 'note' }, inner);
}

/**
 * Re-label a system row (names may have changed).
 * @param {HTMLElement} row
 * @param {any} m
 */
function patchSystemRow(row, m) {
  const inner = row.firstElementChild;
  const label = systemText(m);
  if (inner && inner.textContent !== label) inner.textContent = label;
}

/**
 * Pseudo message of an outbox item so the same renderer can draw it.
 * @param {any} item OutboxItem
 * @returns {any}
 */
export function pendingToMessage(item) {
  const me = store.me;
  const quoted = item.reply_to_id ? store.getMessage(item.chat_id, item.reply_to_id) : null;
  const att = item.attachment;
  return {
    id: 0,
    chat_id: item.chat_id,
    sender_id: me ? me.id : null,
    client_id: item.client_id,
    kind: att ? att.kind : 'text',
    body: item.body || '',
    created_at: item.created_at,
    edited_at: null,
    deleted: false,
    forwarded: false,
    mentions: [],
    reply_to: quoted
      ? { id: quoted.id, sender_id: quoted.sender_id, kind: quoted.kind, body: String(quoted.body || '').slice(0, 200), attachment_name: quoted.attachment ? quoted.attachment.name : null, deleted: Boolean(quoted.deleted), unavailable: false }
      : null,
    attachment: att ? { id: null, name: att.name, mime: att.mime, size: att.size, kind: att.kind, url: att.url, width: att.width, height: att.height, duration: att.duration } : null,
    reactions: [],
    starred: false,
    pinned: false,
    status: null,
    system: null,
  };
}

/* ------------------------------------------------------------------------------------------ */
/* Delegated actions                                                                          */
/* ------------------------------------------------------------------------------------------ */

/**
 * Callbacks of `bindMessageActions`.
 * @typedef {object} ActionEnv
 * @property {(id: number) => any} getMessage message by id (server messages)
 * @property {(menuFor: {message?: any, item?: any}, anchor: HTMLElement|null, point?: {x: number, y: number}) => void} onMenu
 * @property {(replyTo: any) => void} [onQuote]
 * @property {(message: any) => void} [onMedia]
 * @property {(message: any, emoji: string) => void} [onReact]
 * @property {(clientId: string) => any} [getPending]
 * @property {(clientId: string) => void} [onRetry]
 * @property {(clientId: string) => void} [onCancel]
 * @property {(messageId: number) => void} [onJump] system "pinned" notices
 */

/**
 * @param {HTMLElement|null} el
 * @param {ActionEnv} env
 * @returns {{message?: any, item?: any}|null}
 */
function subjectOf(el, env) {
  const row = el ? /** @type {HTMLElement|null} */ (el.closest('.msg-row')) : null;
  if (!row) return null;
  if (row.dataset.cid) {
    const item = env.getPending ? env.getPending(row.dataset.cid) : null;
    return item ? { item } : null;
  }
  const message = env.getMessage(Number(row.dataset.id));
  return message ? { message } : null;
}

/**
 * Install the delegated handlers of a rendering surface on `root`.
 * @param {HTMLElement} root
 * @param {ActionEnv} env
 * @returns {() => void} unbind
 */
export function bindMessageActions(root, env) {
  const target = (e) => (e.target instanceof Element ? /** @type {HTMLElement} */ (e.target) : null);
  let lastMenuAt = 0;
  /** One menu per gesture: a touch long press may fire both the timer and `contextmenu`. */
  const openMenu = (subject, anchor, point) => {
    const now = Date.now();
    if (now - lastMenuAt < 800) return;
    lastMenuAt = now;
    env.onMenu(subject, anchor, point);
  };
  const onClick = (e) => {
    const t = target(e);
    if (!t) return;
    const hit = (sel) => /** @type {HTMLElement|null} */ (t.closest(sel));
    let el;
    if ((el = hit('.msg-more'))) {
      const s = subjectOf(el, env);
      if (s) env.onMenu(s, el);
    } else if ((el = hit('.msg-cancel'))) {
      if (env.onCancel && el.dataset.cid) env.onCancel(el.dataset.cid);
    } else if ((el = hit('.msg-retry'))) {
      if (env.onRetry && el.dataset.cid) env.onRetry(el.dataset.cid);
    } else if ((el = hit('.msg-readmore'))) {
      toggleExpanded(el);
    } else if ((el = hit('.msg-quote'))) {
      const s = subjectOf(el, env);
      if (s && s.message && s.message.reply_to && env.onQuote) env.onQuote(s.message.reply_to);
    } else if ((el = hit('.msg-reaction'))) {
      const s = subjectOf(el, env);
      if (s && s.message && env.onReact && el.dataset.emoji) env.onReact(s.message, el.dataset.emoji);
    } else if ((el = hit('.msg-image'))) {
      const s = subjectOf(el, env);
      if (s && s.message && env.onMedia) env.onMedia(s.message);
    } else if ((el = hit('[data-jump]'))) {
      if (env.onJump) env.onJump(Number(el.dataset.jump));
    }
  };
  const onContext = (e) => {
    const t = target(e);
    if (!t || t.closest('a, input, textarea')) return;
    const s = subjectOf(t, env);
    if (!s) return;
    e.preventDefault();
    openMenu(s, null, { x: e.clientX, y: e.clientY });
  };
  const onKey = (e) => {
    const t = target(e);
    if (!t || !t.classList.contains('msg-row')) return;
    const menuKey = e.key === 'ContextMenu' || (e.key === 'F10' && e.shiftKey);
    if (e.key !== 'Enter' && !menuKey) return;
    const s = subjectOf(t, env);
    if (!s) return;
    e.preventDefault();
    if (e.key === 'Enter' && s.item && s.item.state === 'failed' && env.onRetry) {
      env.onRetry(s.item.client_id);
      return;
    }
    const r = t.getBoundingClientRect();
    openMenu(s, null, { x: Math.round(r.left + Math.min(r.width, 160)), y: Math.round(r.top + Math.min(r.height, 40)) });
  };
  root.addEventListener('click', onClick);
  root.addEventListener('contextmenu', onContext);
  root.addEventListener('keydown', onKey);
  const offLong = ui.onLongPress(root, ({ target: tg, x, y }) => {
    const el = tg instanceof Element ? tg : null;
    if (!el || el.closest('a, .msg-audio, video')) return;
    const s = subjectOf(/** @type {HTMLElement} */ (el), env);
    if (s) openMenu(s, null, { x, y });
  });
  return () => {
    root.removeEventListener('click', onClick);
    root.removeEventListener('contextmenu', onContext);
    root.removeEventListener('keydown', onKey);
    offLong();
  };
}

/**
 * "Read more" / "Show less": expands in place (the list's ResizeObserver compensates the scroll).
 * @param {HTMLElement} button
 */
function toggleExpanded(button) {
  const wrap = button.closest('.msg-text-wrap');
  const row = button.closest('.msg-row');
  const body = wrap ? wrap.querySelector('.msg-text') : null;
  const st = row ? states.get(/** @type {HTMLElement} */ (row)) : null;
  if (!body || !st) return;
  st.expanded = !st.expanded;
  body.classList.toggle('collapsed', !st.expanded);
  button.textContent = st.expanded ? 'Show less' : 'Read more';
  button.setAttribute('aria-expanded', String(st.expanded));
}

/* ------------------------------------------------------------------------------------------ */
/* Starred card and search result (used by ui-shell)                                          */
/* ------------------------------------------------------------------------------------------ */

/**
 * Starred-list item (docs/ui-conv-api.md section 1).
 * @param {any} message
 * @param {{onOpen?: (message: any) => void, showChat?: boolean}} [opts]
 * @returns {{el: HTMLElement, update: (message: any) => void, destroy: () => void}}
 */
export function createMessageCard(message, opts = {}) {
  const { onOpen, showChat = true } = opts;
  let current = message;
  const env = /** @type {ViewEnv} */ ({ context: 'card', getChat: () => store.getChat(current.chat_id) });
  let row = createRow(current, env);
  const head = h('div.msg-card-head');
  const more = h('button.msg-card-more.btn-icon', { type: 'button', 'aria-label': 'Message options' }, icon('more', { size: 20 }));
  const el = h('article.msg-card', { tabIndex: 0, dataset: { id: String(message.id), chatId: String(message.chat_id) } }, head, row);
  const open = () => {
    if (onOpen) onOpen(current);
  };
  const paintHead = () => {
    const chat = store.getChat(current.chat_id);
    const sender = current.sender_id === null ? '' : store.me && current.sender_id === store.me.id ? 'You' : store.userName(current.sender_id);
    head.replaceChildren(
      showChat && chat ? chatAvatar(chat, { size: 'sm', online: null }) : userAvatar(current.sender_id === null ? 0 : current.sender_id, { size: 'sm', online: null }),
      h('span.msg-card-title', h('span.msg-card-sender', sender), showChat && chat ? h('span.msg-card-chat.muted', ` › ${store.chatTitle(chat)}`) : null),
      h('time.msg-card-time.muted', { title: formatDateTime(current.created_at) }, formatDayLabel(current.created_at)),
      more);
  };
  paintHead();
  const showMenu = (subject, anchor, point) => {
    if (!subject.message) return;
    openMessageMenu(subject.message, {
      context: 'starred',
      anchor: anchor || undefined,
      x: point ? point.x : undefined,
      y: point ? point.y : undefined,
      onGoTo: open,
    });
  };
  const unbind = bindMessageActions(el, {
    getMessage: () => current,
    onMenu: showMenu,
    onQuote: open,
    onMedia: (m) => {
      const item = lightboxItemFromMessage(m);
      if (item) openLightbox({ items: [item], index: 0 });
    },
    onReact: (m, emoji) => {
      reactTo(m, emoji);
    },
  });
  more.addEventListener('click', () => showMenu({ message: current }, more));
  el.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (t && !t.closest('a, button, video, audio, .msg-audio, .msg-image')) open();
  });
  el.addEventListener('keydown', (e) => {
    if (e.target === el && e.key === 'Enter') {
      e.preventDefault();
      open();
    }
  });
  return {
    el,
    update: (m) => {
      current = m;
      patchRow(row, m, env);
      paintHead();
    },
    destroy: () => {
      unbind();
      disposeRow(row);
      el.remove();
    },
  };
}

/**
 * Compact search-result row.
 * @param {any} message
 * @param {{query?: string, showChat?: boolean, onOpen?: (message: any) => void}} [opts]
 * @returns {HTMLElement}
 */
export function createSearchResult(message, opts = {}) {
  const { query = '', showChat = true, onOpen } = opts;
  const chat = store.getChat(message.chat_id);
  const own = store.me && message.sender_id === store.me.id;
  const sender = message.sender_id === null ? '' : own ? 'You' : store.userName(message.sender_id);
  const snippet = makeSnippet(message.body || store.summarize(message), query, 100);
  const title = showChat && chat ? store.chatTitle(chat) : sender;
  return h('button.sr-row', { type: 'button', onClick: () => { if (onOpen) onOpen(message); } },
    showChat && chat ? chatAvatar(chat, { size: 'md', online: null }) : userAvatar(message.sender_id === null ? 0 : message.sender_id, { size: 'md', online: null }),
    h('span.sr-main',
      h('span.sr-top', h('span.sr-title.truncate', { dir: 'auto' }, title), h('time.sr-time.muted', { title: formatDateTime(message.created_at) }, formatDayLabel(message.created_at))),
      h('span.sr-snippet', { dir: 'auto' }, showChat && chat && chat.kind === 'group' && sender ? h('span.sr-sender', `${sender}: `) : null, highlightText(snippet, query))));
}
