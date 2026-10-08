/**
 * views/composer.js - the message composer (SPEC 9.2, 9.7, 9.9, 8.3, 9.10):
 * auto-growing textarea (Enter / Shift+Enter / touch rules, IME-safe), drafts per chat, reply and edit bars,
 * emoji panel, @mention autocomplete, attachment tray (pick / paste / drop / camera), voice notes (only where
 * `getUserMedia` + `MediaRecorder` exist), typing emission, the character counter and the disabled / offline notes.
 *
 * Exported API (docs/ui-conv-api.md section 7): createComposer(container, { chatId }) -> handle
 *   { el, setChat(chatId), setReply(message), startEdit(message), resolveContext(), addFiles(files), focus(), destroy() }
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store, prefs } from '../core/store.js';
import { outbox } from '../core/outbox.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { enqueueFiles, precheck } from '../core/upload.js';
import { userAvatar, senderClass } from '../core/avatar.js';
import { cpLength, errorText, fold, formatBytes, formatDuration, isTouchDevice, serverNow, supportsRecording } from '../core/util.js';
import { createEmojiPicker, pushRecentEmoji } from '../lib/emoji.js';

const MAX_FILES = 10;
const MAX_VOICE_S = 300;
const MIN_VOICE_S = 0.6;
const MENTION_ROWS = 8;
const COUNTER_RATIO = 0.9;
const VOICE_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/ogg'];

/**
 * @param {string} s
 * @returns {string} first non-empty line
 */
function firstLine(s) {
  const line = String(s || '').split('\n').find((l) => l.trim() !== '');
  return (line || '').trim();
}

/**
 * Create the composer inside `container`.
 * @param {HTMLElement} container
 * @param {{chatId: number}} opts
 * @returns {{el: HTMLElement, setChat: (chatId: number) => void, setReply: (message: any) => void, startEdit: (message: any) => void,
 *            resolveContext: () => void, addFiles: (files: File[]) => void, focus: () => void, destroy: () => void}}
 */
export function createComposer(container, opts) {
  let chatId = 0;
  let destroyed = false;
  /** @type {any} */
  let reply = null;
  /** @type {null|{id: number, original: string, stash: string}} */
  let edit = null;
  /** @type {null|{text: string, reply_to_id?: number, edit_id?: number}} */
  let pendingDraft = null;
  /** @type {Array<{id: number, file: File, kind: string, url: string|null}>} */
  let files = [];
  let fileSeq = 0;
  let asFile = false;
  /** @type {Array<() => void>} */
  let chatOffs = [];
  /** @type {any} */
  let ctxLayer = null;
  /** @type {any} */
  let emojiLayer = null;
  /** @type {any} */
  let picker = null;
  /** @type {any} */
  let recording = null;
  /** @type {null|{at: number, end: number, items: any[], active: number}} */
  let mention = null;

  /* ---------------------------------------------------------------------------------------- */
  /* DOM                                                                                      */
  /* ---------------------------------------------------------------------------------------- */

  const textarea = /** @type {HTMLTextAreaElement} */ (h('textarea.composer-input', {
    rows: 1,
    dir: 'auto',
    placeholder: 'Type a message',
    'aria-label': 'Message',
    'aria-autocomplete': 'list',
    autocomplete: 'off',
    enterkeyhint: prefs.enterSends() ? 'send' : 'enter',
    onInput: () => onInput(),
    onKeydown: (e) => onKeydown(e),
    onKeyup: (e) => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) updateMention();
    },
    onClick: () => updateMention(),
    onPaste: (e) => onPaste(e),
    onBlur: () => closeMention(),
  }));
  const counter = h('span.composer-counter', { hidden: true, 'aria-live': 'off' });
  const emojiBtn = h('button.btn-icon.composer-btn', { type: 'button', 'aria-label': 'Emoji', 'aria-expanded': 'false', title: 'Emoji', onClick: () => toggleEmoji() }, icon('emoji', { size: 24 }));
  const attachBtn = h('button.btn-icon.composer-btn', { type: 'button', 'aria-label': 'Attach', title: 'Attach', onClick: () => openAttachMenu() }, icon('attach', { size: 24 }));
  const sendBtn = h('button.btn-icon.composer-send', { type: 'button', 'aria-label': 'Send', title: 'Send', onClick: () => send() }, icon('send', { size: 22 }));
  const micBtn = h('button.btn-icon.composer-send.mic', { type: 'button', 'aria-label': 'Record a voice message', title: 'Record a voice message', onClick: () => startRecording() }, icon('mic', { size: 22 }));
  const row = h('div.composer-row', emojiBtn, attachBtn, h('div.composer-field', textarea, counter), sendBtn, micBtn);

  const ctxBar = h('div.composer-context', { hidden: true });
  const tray = h('div.composer-tray', { hidden: true });
  const mentions = h('ul.composer-mentions', { id: 'composer-mentions', hidden: true, role: 'listbox', 'aria-label': 'Mention a member' });
  const emojiPanel = h('div.composer-emoji', { hidden: true });
  const note = h('p.composer-note', { role: 'status', hidden: true });
  const disabledNote = h('p.composer-disabled', { role: 'status', hidden: true });
  const recBar = h('div.composer-recording', { hidden: true, role: 'group', 'aria-label': 'Recording a voice message' });
  const inputs = [
    fileInput('image/*,video/*', null, true, 'photos'),
    fileInput('', null, true, 'documents'),
    fileInput('image/*', 'environment', false, 'camera'),
    fileInput('video/*', 'environment', false, 'video'),
  ];
  const el = h('div.composer', { role: 'region', 'aria-label': 'Message composer' }, note, disabledNote, mentions, ctxBar, tray, row, recBar, emojiPanel, ...inputs.map((i) => i.el));
  container.replaceChildren(el);

  /**
   * Hidden native file input (works on insecure origins; `capture` opens the camera on phones).
   * @param {string} accept
   * @param {string|null} capture
   * @param {boolean} multiple
   * @param {string} name
   * @returns {{el: HTMLInputElement, name: string}}
   */
  function fileInput(accept, capture, multiple, name) {
    const input = /** @type {HTMLInputElement} */ (h('input.composer-file', {
      type: 'file', multiple, accept: accept || null, capture, tabIndex: -1, 'aria-hidden': 'true',
      onChange: () => {
        const list = Array.from(input.files || []);
        input.value = '';
        addFiles(list);
      },
    }));
    return { el: input, name };
  }

  /* ---------------------------------------------------------------------------------------- */
  /* State painting                                                                           */
  /* ---------------------------------------------------------------------------------------- */

  /** @returns {any} */
  const chat = () => store.getChat(chatId);
  /** @returns {boolean} the socket is ready */
  const connected = () => store.connection.state === 'ready';

  function paintState() {
    if (destroyed) return;
    const c = chat();
    const can = c ? store.canPost(c) : { ok: false, text: 'You are no longer a member of this chat.' };
    disabledNote.hidden = can.ok;
    if (!can.ok) disabledNote.textContent = /** @type {any} */ (can).text;
    row.hidden = !can.ok || Boolean(recording);
    ctxBar.hidden = !can.ok || !(reply || edit);
    tray.hidden = !can.ok || files.length === 0;
    const offline = !connected() && (store.connection.banner || !store.connection.online);
    note.hidden = !can.ok || !offline;
    note.textContent = offline ? 'Offline - messages will send when reconnected' : '';
    attachBtn.disabled = !connected();
    attachBtn.title = connected() ? 'Attach' : 'Reconnect to send files';
    attachBtn.setAttribute('aria-label', connected() ? 'Attach' : 'Attach (reconnect to send files)');
    paintButtons();
  }

  /** Send or microphone button; counter; placeholder. */
  function paintButtons() {
    const hasText = textarea.value.trim() !== '';
    const sendable = hasText || files.length > 0 || Boolean(edit);
    const mic = !sendable && supportsRecording() && !edit;
    sendBtn.hidden = mic;
    micBtn.hidden = !mic;
    micBtn.disabled = !connected();
    sendBtn.replaceChildren(icon(edit ? 'check' : 'send', { size: 22 }));
    sendBtn.setAttribute('aria-label', edit ? 'Save the edited message' : 'Send');
    sendBtn.title = edit ? 'Save' : 'Send';
    sendBtn.disabled = !sendable;
    textarea.placeholder = files.length ? 'Add a caption…' : edit ? 'Edit message' : 'Type a message';
    const max = store.limits.max_body_chars;
    const n = textarea.value.length >= max * COUNTER_RATIO * 0.5 ? cpLength(textarea.value) : 0;
    counter.hidden = n < max * COUNTER_RATIO;
    counter.textContent = `${n}/${max}`;
    counter.classList.toggle('over', n > max);
  }

  function autosize() {
    textarea.style.height = 'auto';
    textarea.style.height = `${textarea.scrollHeight + (textarea.offsetHeight - textarea.clientHeight)}px`;
  }

  /** Reply / edit bar. */
  function paintContext() {
    ctxBar.replaceChildren();
    if (edit) {
      ctxBar.append(
        h('span.composer-context-icon', icon('edit', { size: 20 })),
        h('span.composer-context-main', h('span.composer-context-title', 'Editing message'), h('span.composer-context-text.truncate', { dir: 'auto' }, firstLine(edit.original))),
        h('button.btn-icon', { type: 'button', 'aria-label': 'Cancel editing', onClick: () => cancelContext() }, icon('close', { size: 20 })));
    } else if (reply) {
      const meId = store.me ? store.me.id : -1;
      const who = reply.sender_id === meId ? 'yourself' : store.userName(reply.sender_id);
      ctxBar.append(
        h('span.composer-context-icon', icon('reply', { size: 20 })),
        h('span.composer-context-main',
          h(`span.composer-context-title.${reply.sender_id === meId || reply.sender_id === null ? 'me' : senderClass(reply.sender_id)}`, `Replying to ${who}`),
          h('span.composer-context-text.truncate', { dir: 'auto' }, store.summarize(reply))),
        h('button.btn-icon', { type: 'button', 'aria-label': 'Cancel reply', onClick: () => cancelContext() }, icon('close', { size: 20 })));
    }
    const active = Boolean(edit || reply);
    ctxBar.hidden = !active;
    if (active && !ctxLayer) ctxLayer = router.pushLayer({ id: 'composer-context', priority: 30, close: () => cancelContext(false) });
    if (!active && ctxLayer) {
      const layer = ctxLayer;
      ctxLayer = null;
      layer.release();
    }
    paintState();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Input handling                                                                           */
  /* ---------------------------------------------------------------------------------------- */

  function saveDraft() {
    if (!chatId) return;
    store.setDraft(chatId, { text: textarea.value, reply_to_id: reply ? reply.id : null, edit_id: edit ? edit.id : null });
  }

  function onInput() {
    autosize();
    paintButtons();
    saveDraft();
    updateMention();
    if (!edit && textarea.value.trim() !== '') store.emitTyping(chatId, 'typing');
    else store.emitTyping(chatId, 'stop');
  }

  /** @param {KeyboardEvent} e */
  function onKeydown(e) {
    if (mention && handleMentionKey(e)) return;
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
      const sends = prefs.enterSends() ? !e.shiftKey : e.ctrlKey || e.metaKey;
      if (sends) {
        e.preventDefault();
        send();
      }
    } else if (e.key === 'ArrowUp' && textarea.value === '' && !reply && !edit && !e.isComposing) {
      const m = lastOwnEditable();
      if (m) {
        e.preventDefault();
        startEdit(m);
      }
    }
  }

  /** @returns {any} my newest editable text message in the window */
  function lastOwnEditable() {
    const w = store.getWindow(chatId);
    if (!w || !store.me) return null;
    for (let i = w.items.length - 1; i >= 0; i -= 1) {
      const m = w.items[i];
      if (m.sender_id === store.me.id && m.kind === 'text' && !m.forwarded && !m.deleted) {
        return serverNow() - m.created_at <= store.limits.edit_window_s ? m : null;
      }
    }
    return null;
  }

  /** @param {ClipboardEvent} e */
  function onPaste(e) {
    const list = e.clipboardData ? Array.from(e.clipboardData.files || []) : [];
    if (!list.length) return;
    e.preventDefault();
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
    addFiles(list.map((f, i) => {
      if (!/^image\.(png|jpe?g|gif|webp)$/i.test(f.name || 'image.png') && f.name) return f;
      const ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
      return new File([f], `pasted-image-${stamp}${list.length > 1 ? `-${i + 1}` : ''}.${ext}`, { type: f.type });
    }));
  }

  /** @param {string} emoji */
  function insertText(emoji) {
    const start = textarea.selectionStart;
    textarea.setRangeText(emoji, start, textarea.selectionEnd, 'end');
    onInput();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Sending                                                                                  */
  /* ---------------------------------------------------------------------------------------- */

  function clearInput() {
    textarea.value = '';
    autosize();
    paintButtons();
    store.emitTyping(chatId, 'stop');
  }

  async function send() {
    const c = chat();
    if (!c || !store.canPost(c).ok || recording) return;
    const body = textarea.value.trim();
    const max = store.limits.max_body_chars;
    const n = cpLength(body);
    if (n > max) {
      ui.toast(`Message too long (${n}/${max})`, { type: 'error', key: 'too-long' });
      return;
    }
    if (edit) {
      await submitEdit(body);
      return;
    }
    if (files.length) {
      if (!connected()) {
        ui.toast('Reconnect to send files', { type: 'error', key: 'offline-files' });
        return;
      }
      const result = enqueueFiles(chatId, files.map((f) => f.file), body, { reply_to_id: reply ? reply.id : null, asFile });
      reportRejected(result.rejected.map((r) => r.reason));
      clearTray();
      clearInput();
      clearReply();
      saveDraft();
      return;
    }
    if (!body) return;
    outbox.add({ chat_id: chatId, body, reply_to_id: reply ? reply.id : null });
    clearInput();
    clearReply();
    saveDraft();
    closeMention();
    textarea.focus({ preventScroll: true });
  }

  /** @param {string} body */
  async function submitEdit(body) {
    const e = edit;
    if (!e) return;
    if (body === '') {
      ui.toast('A message cannot be empty. Delete it instead.', { type: 'error', key: 'edit-empty' });
      return;
    }
    if (body === e.original.trim()) {
      endEdit(true);
      return;
    }
    try {
      await store.request('msg.edit', { message_id: e.id, body });
      endEdit(true);
    } catch (err) {
      const fatal = err && ['window_expired', 'invalid_state', 'forbidden', 'not_found', 'not_member'].includes(err.code);
      ui.toast(errorText(err, 'Could not edit the message.'), { type: 'error', key: 'edit-error' });
      if (fatal) endEdit(true);
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Reply / edit                                                                             */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} message */
  function setReply(message) {
    if (!message || message.deleted || message.kind === 'system') return;
    if (edit) endEdit(true, false);
    reply = message;
    paintContext();
    saveDraft();
    textarea.focus({ preventScroll: true });
  }

  function clearReply() {
    if (!reply) return;
    reply = null;
    paintContext();
  }

  /**
   * @param {any} message own text message
   * @param {{text?: string}} [from] draft restore: the saved text replaces the message body
   */
  function startEdit(message, from) {
    if (!message || message.deleted) return;
    const stash = edit ? edit.stash : from ? '' : textarea.value;
    reply = null;
    edit = { id: message.id, original: String(message.body || ''), stash };
    textarea.value = from && typeof from.text === 'string' ? from.text : edit.original;
    paintContext();
    autosize();
    saveDraft();
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }

  /**
   * Leave edit mode.
   * @param {boolean} restore put the stashed draft text back
   * @param {boolean} [repaint=true] repaint the context bar (false when the caller sets a reply right after, so the
   *        bar's router layer is kept instead of being released and pushed again)
   */
  function endEdit(restore, repaint = true) {
    const e = edit;
    if (!e) return;
    edit = null;
    textarea.value = restore ? e.stash : '';
    autosize();
    if (repaint) paintContext();
    saveDraft();
  }

  /** @param {boolean} [release=true] */
  function cancelContext(release = true) {
    if (!release) ctxLayer = null;
    if (edit) endEdit(true);
    else clearReply();
    saveDraft();
  }

  /** Re-attach the draft's reply / edit target once the window is rendered (the message may not be loaded earlier). */
  function resolveContext() {
    const d = pendingDraft;
    pendingDraft = null;
    if (!d) return;
    if (d.edit_id) {
      const m = store.getMessage(chatId, d.edit_id);
      if (m && m.sender_id === (store.me && store.me.id) && !m.deleted && serverNow() - m.created_at <= store.limits.edit_window_s) startEdit(m, { text: d.text });
      else saveDraft();
    } else if (d.reply_to_id) {
      const m = store.getMessage(chatId, d.reply_to_id);
      if (m && !m.deleted) {
        reply = m;
        paintContext();
      } else {
        saveDraft();
      }
    }
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Mentions                                                                                 */
  /* ---------------------------------------------------------------------------------------- */

  /** @returns {{at: number, end: number, query: string}|null} the "@query" token at the caret (SPEC 9.10) */
  function mentionToken() {
    const c = chat();
    if (!c || c.kind !== 'group' || textarea.selectionStart !== textarea.selectionEnd) return null;
    const text = textarea.value;
    const end = textarea.selectionStart;
    let i = end;
    while (i > 0 && /[\p{L}\p{N}._-]/u.test(text[i - 1])) i -= 1;
    if (i === 0 || text[i - 1] !== '@') return null;
    const at = i - 1;
    if (at > 0 && !/[\s(]/.test(text[at - 1])) return null;
    return { at, end, query: text.slice(i, end) };
  }

  function updateMention() {
    const tok = mentionToken();
    const c = chat();
    if (!tok || !c) {
      closeMention();
      return;
    }
    const q = fold(tok.query);
    const meId = store.me ? store.me.id : -1;
    const items = (c.members || [])
      .map((m) => store.getUser(m.user_id))
      .filter((u) => u && u.id !== meId && !u.disabled)
      .filter((u) => !q || fold(u.display_name).split(/\s+/).some((w) => w.startsWith(q)) || fold(u.username).startsWith(q))
      .sort((a, b) => a.display_name.localeCompare(b.display_name))
      .slice(0, MENTION_ROWS);
    if (!items.length) {
      closeMention();
      return;
    }
    mention = { at: tok.at, end: tok.end, items, active: 0 };
    paintMention();
  }

  function paintMention() {
    if (!mention) return;
    const m = mention;
    mentions.replaceChildren(...m.items.map((u, i) => h('li.composer-mention', {
      role: 'option',
      id: `mention-opt-${u.id}`,
      'aria-selected': String(i === m.active),
      class: { active: i === m.active },
      onPointerdown: (e) => {
        e.preventDefault();
        pickMention(i);
      },
    }, userAvatar(u, { size: 'sm', online: null }), h('span.composer-mention-name.truncate', { dir: 'auto' }, u.display_name), h('span.composer-mention-user.muted.truncate', `@${u.username}`))));
    mentions.hidden = false;
    textarea.setAttribute('aria-activedescendant', `mention-opt-${m.items[m.active].id}`);
    textarea.setAttribute('aria-expanded', 'true');
    textarea.setAttribute('role', 'combobox');
    textarea.setAttribute('aria-controls', 'composer-mentions');
  }

  function closeMention() {
    if (!mention && mentions.hidden) return;
    mention = null;
    mentions.hidden = true;
    mentions.replaceChildren();
    textarea.removeAttribute('aria-activedescendant');
    textarea.removeAttribute('aria-expanded');
    textarea.removeAttribute('role');
    textarea.removeAttribute('aria-controls');
  }

  /**
   * @param {KeyboardEvent} e
   * @returns {boolean} the key was consumed
   */
  function handleMentionKey(e) {
    const m = mention;
    if (!m) return false;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      m.active = (m.active + (e.key === 'ArrowDown' ? 1 : -1) + m.items.length) % m.items.length;
      paintMention();
      return true;
    }
    if ((e.key === 'Enter' || e.key === 'Tab') && !e.isComposing) {
      e.preventDefault();
      pickMention(m.active);
      return true;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMention();
      return true;
    }
    return false;
  }

  /** @param {number} i */
  function pickMention(i) {
    const m = mention;
    if (!m) return;
    const u = m.items[i];
    textarea.setRangeText(`@${u.username} `, m.at, m.end, 'end');
    closeMention();
    onInput();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Emoji                                                                                    */
  /* ---------------------------------------------------------------------------------------- */

  function toggleEmoji() {
    if (picker) closeEmoji(true);
    else openEmoji();
  }

  function openEmoji() {
    if (picker || destroyed) return;
    picker = createEmojiPicker({
      onPick: (e) => {
        pushRecentEmoji(e);
        insertText(e);
      },
    });
    emojiPanel.replaceChildren(picker.el);
    emojiPanel.hidden = false;
    emojiBtn.setAttribute('aria-expanded', 'true');
    emojiLayer = router.pushLayer({ id: 'composer-emoji', priority: 40, close: () => closeEmoji(false) });
    if (!isTouchDevice()) picker.focusSearch();
  }

  /**
   * @param {boolean} release release the router layer (false when the router closed it)
   * @param {boolean} [refocus=release] give the focus back to the textarea
   */
  function closeEmoji(release, refocus = release) {
    if (!picker) return;
    picker.destroy();
    picker = null;
    emojiPanel.hidden = true;
    emojiPanel.replaceChildren();
    emojiBtn.setAttribute('aria-expanded', 'false');
    const layer = emojiLayer;
    emojiLayer = null;
    if (release && layer) layer.release();
    if (refocus) textarea.focus({ preventScroll: true });
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Attachments                                                                              */
  /* ---------------------------------------------------------------------------------------- */

  function openAttachMenu() {
    const byName = (n) => /** @type {HTMLInputElement} */ (inputs.find((i) => i.name === n)).el;
    const items = [
      { label: 'Photos & videos', icon: 'image', onSelect: () => byName('photos').click() },
      { label: 'Document', icon: 'file', onSelect: () => byName('documents').click() },
    ];
    if (isTouchDevice()) {
      items.push({ label: 'Camera', icon: 'camera', onSelect: () => byName('camera').click() });
      items.push({ label: 'Record video', icon: 'video', onSelect: () => byName('video').click() });
    }
    ui.menu(items, { anchor: attachBtn, label: 'Attach', sheet: 'auto' });
  }

  /**
   * @param {string[]} reasons
   */
  function reportRejected(reasons) {
    if (!reasons.length) return;
    const more = reasons.length > 1 ? ` (and ${reasons.length - 1} more)` : '';
    ui.toast(`${reasons[0]}${more}`, { type: 'error', key: 'file-rejected' });
  }

  /**
   * Add files to the tray (pick, paste, drop, camera).
   * @param {File[]} list
   */
  function addFiles(list) {
    const c = chat();
    if (!c || !store.canPost(c).ok || !list.length) return;
    if (!connected()) {
      ui.toast('Reconnect to send files', { type: 'error', key: 'offline-files' });
      return;
    }
    const rejected = [];
    for (const f of list) {
      if (files.length >= MAX_FILES) {
        rejected.push(`You can send up to ${MAX_FILES} files at once`);
        break;
      }
      const pre = precheck(f);
      if (!pre.ok) {
        rejected.push(/** @type {{reason: string}} */ (pre).reason);
        continue;
      }
      const type = f.type || '';
      const kind = type.startsWith('image/') ? 'image' : type.startsWith('video/') ? 'video' : type.startsWith('audio/') ? 'audio' : 'file';
      let url = null;
      if (kind === 'image') {
        try {
          url = URL.createObjectURL(f);
        } catch (_) {
          url = null;
        }
      }
      fileSeq += 1;
      files.push({ id: fileSeq, file: f, kind, url });
    }
    reportRejected(rejected);
    paintTray();
    textarea.focus({ preventScroll: true });
  }

  /** @param {number} id */
  function removeFile(id) {
    const i = files.findIndex((f) => f.id === id);
    if (i < 0) return;
    const [gone] = files.splice(i, 1);
    if (gone.url) URL.revokeObjectURL(gone.url);
    paintTray();
  }

  function clearTray() {
    for (const f of files) if (f.url) URL.revokeObjectURL(f.url);
    files = [];
    asFile = false;
    paintTray();
  }

  function paintTray() {
    tray.replaceChildren();
    for (const f of files) {
      tray.appendChild(h('div.composer-file-item',
        f.url ? h('img.composer-thumb', { src: f.url, alt: '' }) : h('span.composer-thumb.icon', icon(f.kind === 'video' ? 'video' : f.kind === 'audio' ? 'volume' : 'file', { size: 26 })),
        h('span.composer-file-info', h('span.composer-file-name.truncate', { dir: 'auto' }, f.file.name || 'file'), h('span.composer-file-size.muted', formatBytes(f.file.size))),
        h('button.btn-icon', { type: 'button', 'aria-label': `Remove ${f.file.name || 'file'}`, onClick: () => removeFile(f.id) }, icon('close', { size: 18 }))));
    }
    if (files.some((f) => f.kind === 'image')) {
      tray.appendChild(h('label.composer-asfile', h('input', { type: 'checkbox', checked: asFile, onChange: (e) => { asFile = e.target.checked; } }), 'Send images as files (original quality)'));
    }
    paintState();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Voice notes                                                                              */
  /* ---------------------------------------------------------------------------------------- */

  async function startRecording() {
    if (recording || !supportsRecording() || !connected()) return;
    if (!store.canPost(chat()).ok) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const name = err && err.name;
      ui.toast(name === 'NotFoundError' ? 'No microphone was found.' : 'Microphone access was blocked. Allow it in your browser settings to record voice messages.', { type: 'error', key: 'mic' });
      return;
    }
    if (destroyed) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    const mime = VOICE_TYPES.find((t) => typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported(t)) || '';
    let recorder;
    try {
      recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (_) {
      stream.getTracks().forEach((t) => t.stop());
      ui.toast('Voice messages are not supported by this browser.', { type: 'error', key: 'mic' });
      return;
    }
    const r = { stream, recorder, chunks: /** @type {Blob[]} */ ([]), startedAt: Date.now(), mime: recorder.mimeType || mime, send: false, timer: 0, layer: /** @type {any} */ (null), chatId, replyId: reply ? reply.id : null };
    recording = r;
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) r.chunks.push(e.data);
    };
    recorder.onstop = () => finishRecording(r);
    recorder.start();
    r.layer = router.pushLayer({ id: 'composer-recording', priority: 35, close: () => stopRecording(false, false) });
    r.timer = window.setInterval(() => tickRecording(r), 250);
    paintRecording(r);
    paintState();
    store.emitTyping(chatId, 'recording');
  }

  /** @param {any} r */
  function tickRecording(r) {
    const s = (Date.now() - r.startedAt) / 1000;
    const timeEl = recBar.querySelector('.composer-rec-time');
    if (timeEl) timeEl.textContent = formatDuration(s);
    if (Math.floor(s * 4) % 8 === 0) store.emitTyping(r.chatId, 'recording');
    if (s >= MAX_VOICE_S) {
      ui.toast('Voice messages are limited to 5 minutes.', { type: 'info', key: 'voice-max' });
      stopRecording(true, true);
    }
  }

  /** @param {any} r */
  function paintRecording(r) {
    recBar.hidden = false;
    recBar.replaceChildren(
      h('button.btn-icon', { type: 'button', 'aria-label': 'Cancel recording', title: 'Cancel', onClick: () => stopRecording(false, true) }, icon('trash', { size: 22 })),
      h('span.composer-rec-dot', { 'aria-hidden': 'true' }),
      h('span.composer-rec-time', { role: 'timer' }, formatDuration((Date.now() - r.startedAt) / 1000)),
      h('span.composer-rec-label.muted', 'Recording…'),
      h('button.btn-icon.composer-send', { type: 'button', 'aria-label': 'Send the voice message', title: 'Send', onClick: () => stopRecording(true, true) }, icon('send', { size: 22 })));
    row.hidden = true;
  }

  /**
   * @param {boolean} send keep the recording and send it
   * @param {boolean} release release the router layer (false when the router closed it)
   */
  function stopRecording(send, release) {
    const r = recording;
    if (!r) return;
    recording = null;
    r.send = send;
    window.clearInterval(r.timer);
    if (release && r.layer) r.layer.release();
    recBar.hidden = true;
    recBar.replaceChildren();
    store.emitTyping(r.chatId, 'stop');
    paintState();
    try {
      if (r.recorder.state !== 'inactive') r.recorder.stop();
      else finishRecording(r);
    } catch (_) {
      finishRecording(r);
    }
  }

  /** @param {any} r the recording that ended (its data is complete) */
  function finishRecording(r) {
    r.stream.getTracks().forEach((t) => t.stop());
    if (!r.send) return;
    const seconds = (Date.now() - r.startedAt) / 1000;
    if (seconds < MIN_VOICE_S || r.chunks.length === 0) {
      ui.toast('Hold on a little longer to record a voice message.', { type: 'info', key: 'voice-short' });
      return;
    }
    const type = String(r.mime || 'audio/webm').split(';')[0];
    const ext = type.includes('mp4') ? 'm4a' : type.includes('ogg') ? 'ogg' : 'webm';
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
    const file = new File([new Blob(r.chunks, { type })], `voice-note-${stamp}.${ext}`, { type });
    const result = enqueueFiles(r.chatId, [file], '', { audioOnly: true, duration: Math.round(seconds * 100) / 100, reply_to_id: r.replyId });
    reportRejected(result.rejected.map((x) => x.reason));
    if (r.chatId === chatId && r.replyId) clearReply();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Chat switching                                                                           */
  /* ---------------------------------------------------------------------------------------- */

  /** Drop everything that belongs to the previous chat (the draft was saved on every change). */
  function resetTransient() {
    if (recording) stopRecording(false, true);
    closeEmoji(true, false);
    closeMention();
    clearTray();
    edit = null;
    reply = null;
    pendingDraft = null;
    if (ctxLayer) {
      const layer = ctxLayer;
      ctxLayer = null;
      layer.release();
    }
    ctxBar.hidden = true;
    ctxBar.replaceChildren();
  }

  /** @param {number} id */
  function setChat(id) {
    if (id === chatId) return;
    resetTransient();
    for (const off of chatOffs.splice(0)) off();
    chatId = id;
    chatOffs = [
      store.on(`chat:${id}`, () => paintState()),
      store.on('connection', () => paintState()),
      store.on('limits', () => paintButtons()),
      store.on('prefs', (p) => {
        if (p.key === 'enterSends') textarea.setAttribute('enterkeyhint', prefs.enterSends() ? 'send' : 'enter');
      }),
    ];
    const d = store.getDraft(id);
    pendingDraft = d;
    textarea.value = d ? d.text : '';
    autosize();
    paintState();
  }

  /** Focus the textarea (not while a recording or a disabled note replaces it). */
  function focus() {
    if (!row.hidden) textarea.focus({ preventScroll: true });
  }

  function destroy() {
    if (destroyed) return;
    resetTransient();
    destroyed = true;
    for (const off of chatOffs.splice(0)) off();
    container.replaceChildren();
  }

  setChat(opts.chatId);
  return { el, setChat, setReply, startEdit: (m) => startEdit(m), resolveContext, addFiles, focus, destroy };
}
