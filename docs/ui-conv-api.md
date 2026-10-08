# ui-conv API (contract for ui-shell)

Owner: ui-conv. Files: `web/js/lib/{richtext,emoji}.js`, `web/js/views/{conversation,messageview,messagemenu,composer,infodrawer,lightbox,forward}.js`,
`web/css/{conversation,composer}.css`. SPEC section numbers (`§`) refer to `docs/SPEC.md`; the core API is `docs/ui-core-api.md`.
Everything below is exported by name (no default exports). Every function is safe to call at any time after the first `ev.ready`; none of them throws on a
malformed message (they render what they can). All DOM is built with `core/dom.js:h()`; no HTML strings anywhere.

Import paths from `web/js/views/x.js`: `import { createMessageCard } from './messageview.js';`, `import { highlightText } from '../lib/richtext.js';`.

---------------------------------------------------------------------------------------------------

## 1. `views/messageview.js` (message rendering outside the conversation list)

The conversation list itself is private to `conversation.js`; ui-shell needs only the two renderers below (Starred view §9.2 and the global search panel).
Both render a message in a **context other than the live list**: no ticks animation, no reply/edit, attachments are shown (thumbnail / player / file card),
rich text is rendered (§9.3), deleted messages show the italic placeholder, system messages are never passed in (the server never returns them from `msg.starred`/`msg.search`).

```js
createMessageCard(message, opts?) → { el: HTMLElement, update(message): void, destroy(): void }
```
Starred-list item: a card with a header row (chat avatar, chat title, and for groups "Sender" before it, time) and the bubble below it.
* `opts.onOpen(message)` – called when the user activates the card (click / Enter) or the "Go to message" menu entry; ui-shell does `router.openChat(message.chat_id, { messageId: message.id })`.
  Clicks on links, images (which open the lightbox) and the "…" button do NOT call it.
* `opts.showChat = true` – show the chat header row (false inside a per-chat list).
* `opts.onChanged?(message)` – optional; called after the user changed the message through the card's own "…" menu (e.g. Unstar), after the store applied the response.
* The "…" button opens `messagemenu.openMessageMenu(message, { context: 'starred', onGoTo: onOpen })` (Forward, Copy, Star/Unstar, Go to message, Delete for me).
* `el` is `<article class="msg-card" tabindex="0" data-id data-chat-id>`; a keyboard user activates it with Enter, opens the menu with Shift+F10 / the Menu key.
* `update(message)` patches the card in place (call it from `store.on('message_update', m => …)` when `m.id` is held: `store.patchMessage(held, m)` first, then `card.update(held)`).
* `destroy()` removes listeners and pauses media (call it when the list is rebuilt or unmounted).

```js
createSearchResult(message, opts?) → HTMLElement
```
Compact result row for message search (global and in the sidebar): chat avatar, chat title, "Sender: snippet" with the query highlighted, time. A `<button class="sr-row">`.
* `opts.query = ''` – highlighted case-insensitively inside the snippet (the snippet is centred on the first hit, ≤ 100 code points).
* `opts.showChat = true` – show the chat title (false for chat-scoped results).
* `opts.onOpen(message)` – click / Enter (ui-shell routes to `#/c/<chat>/m/<id>`).
* Static: not patched after creation.

Styles for `.msg-card`, `.sr-row` and everything inside are in `css/conversation.css`; ui-shell only lays the rows out in its own scroll container (`.sr-row` and `.msg-card` are `display:block; width:100%`).

---------------------------------------------------------------------------------------------------

## 2. `views/messagemenu.js` (message context menu, bottom sheet, quick reactions, info)

```js
openMessageMenu(message, opts) → { close(): void }
```
Desktop (pointer): a popover next to `opts.anchor` or at `(opts.x, opts.y)` with the quick-reaction bar above the list. Touch / narrow: a bottom sheet (`ui.sheet`) with the same content.
Opens as a router layer (priority 40), so Escape and the hardware Back button close it.
* `message` – a held Message (must not be a system message or an outbox pseudo message; for those use `openPendingMenu`).
* `opts.context = 'chat' | 'starred' | 'pinned'` – `chat` offers Reply and Edit (via `opts.onReply(message)` / `opts.onEdit(message)`; entries are omitted when the callback is absent);
  `starred`/`pinned` offer "Go to message" (via `opts.onGoTo(message)`; omitted without it).
* `opts.anchor?: Element`, `opts.x?, opts.y?` – placement.
* `opts.chat?` – the Chat of the message (default: `store.getChat(message.chat_id)`).
* `opts.onSelectMode?()` – shows "Select" (multi-select) when given (only the conversation passes it).
* Entries and rules: Reply · React (quick bar 👍❤️😂😮😢🙏 + "more" → emoji picker; picking your own current emoji sends `emoji:null`) · Copy (text or attachment URL) · Forward (hidden for deleted messages) ·
  Star/Unstar · Pin/Unpin (not in `only_admins_post` chats for non-admins; limit 5 → toast) · Info (own, non-deleted messages) · Edit (own text, not forwarded, within `limits.edit_window_s`) ·
  Delete (dialog: "Delete for me", "Delete for everyone" when allowed: own within `limits.delete_window_s`, or group admin, and Cancel). Server errors become toasts (`errorText`).

```js
openPendingMenu(item /* OutboxItem */, opts?: { anchor?, x?, y? }) → { close() }     // failed/queued outbox bubble: Retry, Copy text, Discard
openMessageInfo(message, chat?) → { close() }                                          // Info sheet/dialog: recipients delivered/read with times (msg.info) + "Reactions" (names per emoji)
```

---------------------------------------------------------------------------------------------------

## 3. `views/forward.js`

```js
openForward({ messages: Message[], fromChatId?: number }) → Promise<boolean>
```
Opens the Forward dialog (§9.10): searchable list of chats AND people (a person without a DM gets `chat.open_direct` first), chips for the selected targets, the 6th target is disabled with a hint
(`limits.max_forward_chats`), Forward button → `msg.forward` (≤ `limits.max_forward_messages` messages; deleted, system and pending messages are filtered out; if nothing is left it toasts and resolves `false`).
Success toast "Forwarded to N chats" (tap opens the chat when N = 1). Resolves `true` after a successful forward, `false` when cancelled or failed.

```js
createTargetPicker({ people = true, chats = true, exclude = [], max = Infinity, onChange(selection) }) → { el, selection(): Array<{kind:'user'|'chat', id:number}>, focus(): void, destroy(): void }
```
The searchable list used by the dialog (also used by the info drawer's "Add members" with `chats:false`). `exclude` = user ids to hide. `selection` entries are `{kind:'user', id}` / `{kind:'chat', id}`.

---------------------------------------------------------------------------------------------------

## 4. `views/lightbox.js`

```js
openLightbox({ items: LightboxItem[], index = 0, opener?: Element }) → { close(): void }
LightboxItem = { url: string, name: string, kind: 'image'|'video', width?: number|null, height?: number|null, size?: number, created_at?: number, sender?: string }
lightboxItemFromMessage(message) → LightboxItem | null      // null unless message.attachment.kind is image|video and the message is not deleted
```
Full-screen viewer in `#overlay-root` (`role="dialog" aria-modal="true"`, focus trapped, router layer priority 50): previous/next (buttons, ←/→, swipe), image zoom toggle (click), video with controls,
Download (`?dl=1`) and Close. Focus returns to `opener` (default: the element focused when it opened).

---------------------------------------------------------------------------------------------------

## 5. `lib/richtext.js` (§9.3)

```js
renderRichText(text, opts?) → DocumentFragment      // blocks + inline formatting as DOM nodes; never HTML strings
   opts: { highlight?: string, resolveMention?: (username: string) => ({ user_id:number, name:string, self:boolean } | null), emojiScale?: boolean }
parseRichText(text, resolveMention?) → Block[]       // pure AST (unit-tested under node)
highlightText(text, query) → DocumentFragment        // plain text with <mark class="hl"> around case-insensitive matches of `query`
makeSnippet(text, query, max = 100) → string         // one line, ≤ max code points, centred on the first match, "…" at cut ends
findUrls(text) → Array<{ start:number, end:number, url:string }>     // linear-time; trailing punctuation trimmed; only http(s)
emojiOnlyCount(text) → number                        // > 0 when the text consists only of emoji (and spaces): the number of emoji clusters (≤ 30), else 0
```
Supported: `*bold*`, `_italic_`, `~strike~`, `` `mono` ``, ```` ```block``` ````, `> quote` lines, `- ` / `1. ` lists, auto-links (`<a class="rt-link" rel="noopener noreferrer" target="_blank">`), `@mention` chips
(`<span class="mention" data-user-id>` / `.mention.self` for me; resolved with the §7.4 greedy-then-trim rule, no regex lookbehind), search highlight (`<mark class="hl">`), emoji-only messages (the caller adds the size class from `emojiOnlyCount`).

---------------------------------------------------------------------------------------------------

## 6. `lib/emoji.js` (§9.8 emoji data and picker)

```js
QUICK_REACTIONS: string[]                                 // ['👍','❤️','😂','😮','😢','🙏']
getEmojiCategories() → Array<{ id, label, icon: string /* a representative emoji */, items: Array<{ e: string, n: string /* lower-case keywords */ }> }>   // canvas-filtered once per user agent (cached), no Flags category
searchEmoji(query, limit = 80) → string[]                 // keyword search over names (prefix of a word first, then substring)
getRecentEmoji() → string[]    pushRecentEmoji(emoji) → void                 // namespaced `emoji_recent` (≤ 32)
createEmojiPicker({ onPick(emoji), search = true }) → { el: HTMLElement, focusSearch(): void, destroy(): void }       // the component (category tabs, search, recents, grid)
openEmojiPicker({ anchor?, x?, y?, onPick(emoji), onClose?, keepOpen = false }) → { close(): void }                  // popover on pointer devices, bottom sheet on touch; router layer priority 40
```

---------------------------------------------------------------------------------------------------

## 7. `views/conversation.js`, `composer.js`, `infodrawer.js` (mounted by main.js / conversation.js; not called by ui-shell)

* `conversation.js` exports `mount(container, ctx)` per the core view contract and returns `{ update(route), unmount() }`. It owns `ctx.slots.drawer` (`#pane-drawer`) and fills it with the info drawer; it calls `ui.setDrawerOpen()` itself.
* `composer.js` exports `createComposer(container, opts) → ComposerHandle` and `infodrawer.js` exports `createInfoDrawer(container, opts) → DrawerHandle` (used only by `conversation.js`).
* ui-shell integration points: the sidebar opens a chat with `router.openChat(id[, {messageId}])`; "Draft:" text comes from `store.getDraft`; an unread mention button jumps with `router.openChat(id, { messageId: chat.me.first_unread_mention_id })`.
  The in-chat search bar, the pinned-message banner and the drawer are internal to `conversation.js`.

## 8. CSS contract (css/conversation.css, css/composer.css)

Class families defined by ui-conv: `.conv-*` (header, list, FAB, banners, search bar), `.msg-*` / `.bubble*` / `.rt-*` / `.mention` / `.hl` (messages), `.msg-card`, `.sr-row`, `.composer*`, `.emoji-*`, `.lightbox*`, `.drawer-*`, `.fwd-*`.
They use only the tokens of `docs/ui-core-api.md` §12 (both themes). ui-shell must not style these classes.
