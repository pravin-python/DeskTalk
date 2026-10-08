# ui-core API (contract for ui-shell and ui-conv)

Owner: ui-core. Everything below is implemented in `web/js/core/*.js`, `web/js/main.js` and `web/css/base.css`. Code against this
document only; if you need something that is not here, ask ui-core (report it as a request) instead of reaching into internals.
SPEC section numbers (`§`) refer to `docs/SPEC.md`. Views import core modules by relative path, e.g. from `web/js/views/x.js`:
`import { store } from '../core/store.js';`  from `web/js/lib/x.js`: `import { h } from '../core/dom.js';`.

**Hard rules for every view/lib file** (checked by the static UI lint, §12): never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
`eval`, `new Function`, `setAttribute('style', …)`, `srcdoc`, `DOMParser`, `createContextualFragment`, `document.cookie`; no `<style>` creation, no `insertRule`,
no external URLs (also not inside strings: write "localhost", not a URL), no `requestAnimationFrame` in receipt/notification paths, **no regex lookbehind (`(?<=`, `(?<!`) and no `v`-flag regex literal anywhere under `web/js`** (Safari < 16.4 cannot even parse them, SPEC 9.8; use `util.findMentions` for @mentions). Build DOM with `h()`; style with CSS classes (`el.style.x = …` and `style.setProperty()` are allowed for dynamic values).

---------------------------------------------------------------------------------------------------

## 1. Boot, shell DOM and the view-mounting contract (`main.js`)

### 1.1 Shell DOM (static, in `index.html`)
```
<div id="boot" class="boot">…</div>                       splash until the first ev.ready
<div id="auth-root" class="auth-root" hidden></div>       auth view slot (full screen)
<div id="app" class="app" data-view="list" data-drawer="closed" hidden>
  <aside id="pane-sidebar" class="pane pane-sidebar"></aside>   sidebar view slot
  <main  id="pane-main"    class="pane pane-main"></main>       route view slot (conversation | starred | settings | admin | empty state)
  <aside id="pane-drawer"  class="pane pane-drawer" hidden></aside>   info drawer slot (owned by conversation.js / infodrawer.js)
</div>
<div id="conn-banner" class="conn-banner" role="status" hidden></div>   "Reconnecting…" (main.js)
<div id="overlay-root" class="overlay-root"></div>        dialogs, popovers, sheets, lightbox (use ui.* or append here)
<div id="toast-root" class="toast-root" role="region" aria-live="polite"></div>
```
`#app[data-view]` is one of `list | chat | starred | settings | admin` (set by main.js on every route change, drives which pane is visible on narrow screens, §9 of this doc).
`#app[data-drawer]` is `open|closed`; call `ui.setDrawerOpen(bool)` (it also toggles `#pane-drawer[hidden]`).

### 1.2 View module contract (ONE scheme: named export `mount`)
Every view module under `web/js/views/` that main.js mounts exports

```js
export function mount(container, ctx)  // may be async. Returns undefined or a handle { update?(route), unmount?() }.
```
* `container` – the slot element (see table). main.js empties it before `mount` and again after `unmount`.
* `handle.update(route)` – optional. When the route changes but the same view stays mounted (e.g. chat 5 → chat 7, settings tab change, `#/c/5` → `#/c/5/m/10`) main.js calls `update(route)`; if the handle has no `update`, main.js calls `unmount()` and `mount()` again.
* `handle.unmount()` – optional; remove listeners (`store.on(...)` return `off` functions), timers, observers. `ctx.signal` is aborted right before `unmount()` – pass it to `fetch`/`addEventListener`.
* A thrown/rejected `mount` is caught, logged, and replaced by a small "The \"x\" screen failed to load" notice (`.view-missing`); one broken view never takes the shell down. Route changes are serialised: only the latest route is mounted; an earlier mount that is still loading is unmounted when it finishes.
* **A missing module is tolerated**: main.js loads views with dynamic `import()`; on a 404/syntax error it logs and shows "The \"x\" screen is not available yet" in the slot. Pieces can therefore be integrated one by one.

| view file | exports | slot | mounted when | `ctx` extras |
|---|---|---|---|---|
| `views/auth.js` | `mount` | `#auth-root` | not authenticated, or `me.must_change_password` | `mode`, `info`, `notice`, `me`, `done(me)` |
| `views/sidebar.js` | `mount` | `#pane-sidebar` | once per session, after the first `ev.ready` | – |
| `views/conversation.js` | `mount` (+`update`) | `#pane-main` | route `chat` (`#/c/<id>[/m/<mid>]`) | `route` |
| `views/starred.js` | `mount` | `#pane-main` | route `starred` | `route` |
| `views/settings.js` | `mount` (+`update` for tab) | `#pane-main` | route `settings` (`#/settings[/<tab>]`) | `route` |
| `views/admin.js` | `mount` (+`update` for tab) | `#pane-main` | route `admin` (`#/admin[/<tab>]`) | `route` |

All other view files (`newchat.js newgroup.js searchpanel.js messageview.js messagemenu.js composer.js infodrawer.js lightbox.js forward.js`, `lib/*`) are **not** mounted by
main.js: they are imported by the owner views above; their exports are agreed between ui-shell and ui-conv. Suggested convention for dialog-like modules: `export function open(opts) → Promise|handle`.
The empty state of the main pane (route `list` on wide screens: workspace name, "Select a chat to start messaging", status chips) is rendered by main.js itself.

`ctx` (always): `{ slots:{root, auth, sidebar, main, drawer, overlay}, signal:AbortSignal, logout(opts?) }`.
* `ctx.logout({ clearDevice = true } = {})` → Promise<void>: asks "N unsent messages will be lost" (only when `clearDevice` and the outbox is non-empty), calls `api.logout()`, stops the socket, wipes this user's namespaced storage when `clearDevice`, resets the store, shows the login screen with notice "You signed out.". Resolves `false`-ish (nothing happens) when the user cancels.
* Auth view extras: `ctx.mode` = `'auth'` (the view chooses setup / login / register itself from `ctx.info`) or `'change_password'` (forced change, §9.2); `ctx.info` = `/api/info` payload `{name, registration_open, needs_setup, tls}`; `ctx.notice` = string|null to show on top (e.g. "You signed out."); `ctx.me` (only in `change_password`); `ctx.done(me)` must be called after a successful login/register/password change with the `me` object (`/api/login|register` return `{me}`; after the forced change call `api.getMe()` and pass its `me`). If `me.must_change_password` is still true main.js switches to `change_password` mode by itself.
* Route view extras: `ctx.route` (the route at mount time; later changes arrive through `handle.update(route)` or `router.on('route', …)`).

### 1.3 Session life cycle (what main.js does)
1. Feature gate (WebSocket, fetch, `CSS.supports('height','1dvh')`, `\p{L}` regexes) → "Please update your browser" otherwise. 2. `GET /api/info`, `GET /api/me`: 401 → auth view; `must_change_password` → auth view in `change_password` mode.
3. Authenticated: `store.start()`, `outbox.start()`, `notify.start()`, `sound.start()`, `router.start()`, `socket.start()`; splash until the first `ev.ready`, then shell + sidebar + route view are mounted.
4. `socket` state changes: `unauthorized` → login ("Your session expired. Please sign in again."), `kicked` → login with the §8.5 text for the kick reason (storage of that user wiped), `password_change` → forced change screen, `toomany` → full-screen "open in too many windows" with a "Use this window" button, `outdated` → "reload the page".
5. `api` 401 `unauthorized` anywhere → same as `unauthorized` above.
6. Route guard: a chat route whose chat is not in the store after `ev.ready` → `#/` + toast "Chat not found". `chat_removed` for the open chat → `#/` + toast "You were removed from <title>".
7. Global shortcuts: `Ctrl+K` focuses the element `[data-shortcut="search"]` (the sidebar search input MUST carry `data-shortcut="search"`), `Alt+ArrowUp/Down` previous/next chat of `store.chatList()`, `Escape` → `router.closeTopLayer()`. A view that wants Escape must register a layer (`router.pushLayer`) instead of its own global listener.
8. Prefs: `html[data-theme]` (`light|dark`, absent = system) and `html[data-font]` (`small|large`, absent = normal) follow `prefs`; `--app-h` / `--app-top` follow `visualViewport.height` / `offsetTop` (iOS keyboard).
9. Every end of session (logout, kick, 401) wipes the user's namespaced storage (`util.storage.wipe()`), resets the outbox and the store, and sends the hash back to `#/`.

---------------------------------------------------------------------------------------------------

## 2. `core/dom.js`
```js
h(tag, props?, ...children) → HTMLElement
```
`tag`: `'div'`, or with CSS-like shorthand `'button.btn.primary#save'` (classes and one id). `props` (object or null; may be omitted – a non-plain-object second argument is treated as the first child):
* `class` / `className`: string | array | `{name: bool}` map. `dataset: {k: v}`. `style`: object (`{color:'red', '--x':'1px'}` – custom properties via `setProperty`) or string (assigned with `style.cssText`).
* `onClick`, `onInput`, `onKeydown`, `onPointerdown`, … : listener (`'on'` + event name in any case, lower-cased). `onTouchstart: [fn, {passive:true}]` passes options. `ref: el => …` is called with the element after props/children are applied.
* `text`: sets `textContent`. DOM properties are set as properties for `value checked selected disabled indeterminate readOnly multiple hidden tabIndex required controls autoplay loop muted open` (also `for` → `htmlFor`); every other prop is an attribute (`true` → empty attribute, `false/null/undefined` → skipped, numbers stringified).
* `href` / `src` / `poster` / `action` are validated by `isSafeUrl` (only `http:`, `https:`, `blob:`, same-origin relative paths, `#fragment`, and `data:image/(png|jpeg|gif|webp)`); anything else is dropped (and warned in the console). Attribute names starting with `on`, `srcdoc`, `formaction` are refused.
* children: strings/numbers → text nodes (never parsed as HTML), Nodes, arrays (flattened), `null/undefined/false/true` ignored.
```js
text(str) → Text                       frag(...children) → DocumentFragment
clear(el) → el                         setChildren(el, ...children) → el      // clear + append
$(sel, root = document) → Element|null $$(sel, root = document) → Element[]
on(target, type, fn, opts?) → off()    // addEventListener returning the remover
toggleClass(el, cls, force?) → el      setText(el, str) → el                  // writes only if different
isSafeUrl(url, kind = 'link'|'media') → boolean
focusEl(el, opts?) → boolean           // focus() that never throws, preventScroll aware
isFocusable(el) → boolean
```

## 3. `core/util.js`
```js
class Emitter { on(name, fn) → off(); once(name, fn) → off(); off(name, fn); emit(name, ...args); listenerCount(name) }   // listeners run synchronously, in registration order, each in its own try/catch
newClientId() → string               // 8..64 chars of [A-Za-z0-9_-]
randomHex(bytes = 8) → string        uuid() → string          // crypto.randomUUID when present, else getRandomValues
cpLength(s) → number                 // Unicode code points (Array.from(s).length) – use for every length limit
cpSlice(s, n) → string               // first n code points
clamp(n, lo, hi)   debounce(fn, ms) → fn with .cancel() .flush()   throttle(fn, ms)   sleep(ms) → Promise   randomBetween(lo, hi)
fold(s) → string                     // NFKC + casefold, for client-side filtering
pluralize(n, one, many = one + 's') → string
findMentions(text, isKnown) → [{ start, end, username }]   // the §7.4 `@username` grammar WITHOUT lookbehind (§9.3): `isKnown(lowerCaseUsername)` says whether a current member has exactly that username; greedy token, trailing `.`/`-` trimmed one by one, adjacent tokens (`@a @b`) both match; `start` = index of "@", `end` exclusive, `username` as written. richtext.js and the composer must use it (or the same regex without lookbehind)
runPaced(items, worker, { gapMs = 2100, onRow?, signal? } = {}) → Promise<[{ item, status:'ok'|'exists'|'failed', value?, error?:{code,message} }]>   // "Add employees" (§9.10): strictly sequential, next call ≥ gapMs after the previous response, rate_limited/server_busy wait err.retry_after and retry the SAME item, conflict ⇒ 'exists', other errors ⇒ 'failed' and the loop continues
// clock skew (§8.5): set by the store on every ev.ready
setSkew(seconds)  getSkew() → seconds  serverNow() → seconds (float, = Date.now()/1000 + skew)
// formatting (all timestamps are server epoch seconds; "now" is serverNow())
formatTime(ts) → "10:42"                        // Intl hour:minute
formatDayLabel(ts) → "Today" | "Yesterday" | "Monday" (<7 days) | "12 Mar 2025"        // date separators
formatListTime(ts) → "10:42" | "Yesterday" | "Monday" | "12/03/2025"                      // chat list (§9.10)
formatDateTime(ts) → "12 Mar 2025, 10:42"                                                // tooltips
formatLastSeen(user) → "online" | "last seen today at 10:42" | "last seen yesterday at 18:05" | "last seen 12 Mar at 09:00" | ""   // "" when hidden/unknown
formatDuration(seconds) → "m:ss" | "h:mm:ss"    formatBytes(n) → "1.2 MB"    formatCount(n, cap = 999) → "999+"
errorText(err, fallback?) → string             // human text for ApiError/SocketError/plain {code,msg,retry_after} (rate_limited → "Too many requests, try again in 5 s", not_member, forbidden, window_expired, request_timeout, …); server-written texts (bad_request, invalid_state, conflict, weak_password, forbidden, …) are used as they are
// environment
isLoopbackHost(hostname) → bool    isInsecureRemote() → bool    // http: to a non-loopback host → show the "not encrypted" banner (§0)
isTouchDevice() → bool  (pointer: coarse)   isIOS() → bool   prefersReducedMotion() → bool   supportsDesktopNotifications() → bool
supportsRecording() → bool                      // getUserMedia + MediaRecorder present: hide the mic / voice-note button when false (insecure origins)
copyToClipboard(text) → Promise<boolean>        // navigator.clipboard, else execCommand('copy') fallback (insecure origins)
```
**Namespaced storage** (`util.storage`; §9.7). Keys are `fc:v1:<instance_id>:<user_id>:<name>`; every access is try/catch'd with an in-memory fallback; another user's keys are never read.
```js
storage.setNamespace(instanceId, userId)   storage.clearNamespace()   storage.ready → bool
storage.get(name, fallback = null)   storage.set(name, value)   storage.remove(name)      // value is JSON-serialisable
storage.keys(namePrefix = '') → string[]            // names (without the namespace) of the current namespace, e.g. keys('draft:')
storage.wipe()                                      // remove every key of the current namespace (logout / kick / 401)
storage.getGlobal(name, fallback)  storage.setGlobal(name, value)  storage.removeGlobal(name)   // key `fc:v1:<name>`, device-level data only (prefs, emoji support cache) – never user content
```
Names in use: `outbox`, `draft:<chatId>`, `emoji_recent` (ui-conv; use `storage.get/set('emoji_recent')`), global `prefs`, `emoji_ok:<hash>` (ui-conv's canvas support cache).

## 4. `core/icons.js`, `core/avatar.js`
```js
icon(name, { size = 20, className = '', label = '' } = {}) → SVGElement   // createElementNS only; aria-hidden unless `label` is given (then role="img" aria-label=label)
ICON_NAMES → string[]   hasIcon(name) → bool
```
Names: `search close more back arrow-right send attach emoji mic stop check checks clock error reply forward star star-outline pin trash edit copy info group group-add person person-add settings lock logout plus download play pause image file video volume volume-off bell bell-off archive chevron-down chevron-up chevron-left chevron-right at link eye eye-off refresh shield moon sun camera menu open-external chat chat-outline warning check-circle devices key print cloud-off record`.
(66 names. `check` = single tick, `checks` = double tick, coloured via the `.tick-*` classes. `forward` is the "forward message" arrow, `arrow-right` a plain arrow.) Unknown names render an empty box and warn.

```js
initials(name) → "RK"                    // first letter of the first two words that contain a letter/digit (code-point safe); emoji-only names show the emoji; "?" when empty
colorIndex(id) → 0..11                    // stable hash of a number|string id → palette slot
avatar(subject, { size = 'md', online = null, className = '' } = {}) → HTMLElement
    // subject = { id, name, group?:bool }  size: 'xs'|'sm'|'md'|'lg'|'xl'  → <span class="avatar av-md av-c3">RK</span>; group → group glyph; online true/false adds .av-dot[.on]
userAvatar(userOrId, opts) → HTMLElement  // from the store directory (display_name); online defaults to user.online
chatAvatar(chatOrId, opts) → HTMLElement  // direct → the peer's avatar (self-chat: me), group → group avatar with the title's colour
setAvatarOnline(el, online) → void        // toggles the dot without re-rendering
senderClass(userId) → "sender-c5"         // CSS class colouring a sender name (12-colour palette, §9.8)
```

## 5. `core/api.js` (REST, §4.2)
All calls send `X-Requested-With: desktalk`, `Content-Type: application/json` (POST bodies, `{}` when empty), `credentials: same-origin`, `cache: no-store`, default timeout 15 s.
```js
class ApiError extends Error { status:number; code:string; message:string; retryAfter:number|null; reason:string|null }
//  status 0 → code 'network' (fetch failed) or 'timeout'; otherwise code/message come from {"error":{code,msg,retry_after}} (fallback code 'server_error')
api.request(method, path, body?, { timeout = 15000, signal } = {}) → Promise<object|null>   // 204 → null
api.getInfo() → {name, registration_open, needs_setup, tls}
api.getMe({ timeout?, silent401? }) → {me}                        // 401 → ApiError code 'unauthorized' (silent401: do not notify onUnauthorized listeners)
api.login(username, password) → {me}                              // 401 'bad_credentials', 403 'disabled', 429 'rate_limited' (err.retryAfter)
api.register({ username, display_name, password, setup_code?, join_code? }) → {me}
api.logout() → null                                               // never throws
api.changePassword(oldPassword, newPassword) → null          // errors: 403 'forbidden' with err.reason 'bad_old_password' (wrong old password; NEVER 401), 400 'weak_password' (+ reason 'same_as_old'), 429. Show err.message inline
api.listSessions() → {sessions:[{id, created_at, last_used_at, ip, user_agent, current}]}
api.revokeSession({ id } | { all_others: true }) → null
api.onUnauthorized(fn) → off()      // fn(err) is called for every ApiError with code 'unauthorized' (expired session; NOT for bad_credentials); main.js uses it to show the login screen
api.reportUnauthorized(err)         // used by upload.js for XHR 401s
createSearcher({ chat_id?, limit?, debounceMs? }) → Searcher   // the ONE msg.search client (§9.6): chat_id = in-chat search (pages of 50, reports `total`), omitted = global search (pages of 30)
searcher.setQuery(text)             // call on every keystroke: 400 ms debounce, < 2 characters (trimmed) is never sent and clears the results at once, stale responses ignored, one request in flight (a newer query waits), rate_limited waits err.retry_after silently and keeps the previous results
searcher.loadMore()                 // next page (before_id = oldest result id), appended without duplicates
searcher.on('update', state) → off()   // state = { q, results:[{message, chat_id}], hasMore, total:number|null, totalCapped:bool /* total ≥ 1000 → show "1000+" */, loading, error:{code,message}|null }; `searcher.state` is the same object
searcher.destroy()                  // stop timers/events (call when the search bar closes)
serialSearch(fn) → Promise          // lane that lets only ONE msg.search/msg.shared run at a time (the server answers a concurrent one with rate_limited): wrap your own `msg.shared` request in it
```
`ApiError` codes added by the round-3 spec: `request_timeout` (408), `range_not_satisfiable` (416). `onUnauthorized` fires ONLY for code `unauthorized` (a session that is gone): `bad_credentials` (login form) and `403 forbidden` (e.g. wrong old password) never sign the user out or wipe local data (§9.7).

## 6. `core/socket.js` (WebSocket, §6, §7.1, §8.5)
```js
socket.start()                       // connect and keep connected (idempotent). Called by main.js after login.
socket.stop()                        // permanent close, no reconnect (logout / kicked); state → 'stopped'
socket.reconnect()                   // reset back-off and connect NOW (4003 "Use this window" button, manual retry)
socket.request(type, d = {}, { timeout = 15000 } = {}) → Promise<object>   // `type` MUST be one of the §7.4 request names. Resolves `res.d`; rejects SocketError.
socket.send(type, d = {}) → boolean  // fire-and-forget (no id, no res), e.g. 'typing'. false when the socket is not open.
socket.on(name, fn) → off()          // events: see below
socket.state → 'idle'|'connecting'|'open'|'ready'|'waiting'|'unauthorized'|'password_change'|'kicked'|'toomany'|'outdated'|'stopped'
socket.probe(reason?)                // liveness check, ONLY while live (state 'ready'): ping, 2.5 s, else drop the socket and reconnect at once (the socket calls it itself on visibilitychange/online/pageshow/clock jumps)
socket.nudge()                       // reconnect now when waiting for a back-off and the last attempt was > 3 s ago
socket.isReady → bool                // state === 'ready' (ev.ready processed on the current socket)
socket.latency → ms|null             socket.info → { kickReason, nextRetryAt (ms epoch)|null, attempt, error }
class SocketError extends Error { code, message, reason?, retry_after?, chat_id?, message_id? }
//  local codes: 'offline' (not connected), 'connection_lost' (socket closed before the res), 'timeout' (15 s). Server codes: §7.1.1.
isTransientError(err) → bool         // outbox taxonomy §7.1.1: offline, connection_lost, timeout, rate_limited, server_busy, server_error
PROTOCOL_VERSION = 1
```
Socket events (`socket.on`): `'state'` `(state, prev)`; `'ev.<name>'` `(d)` for every server event, e.g. `'ev.message'`, `'ev.ready'` (frames are dispatched strictly in arrival order; the store has already handled `ev.ready` before `state` becomes `'ready'`); `'open'`, `'close'` `(code)`.
You normally do **not** subscribe to `ev.*` yourself: the store does and re-emits state-level events (§7). Requests may be issued in state `'open'` or `'ready'` (the server holds them until `ev.ready` was queued); otherwise they reject with `offline`.
Behaviour implemented here (nothing for views to do): request ids + 15 s timeout, a socket that is open/connecting but gets no `ev.ready` within 30 s is closed and retried with the normal back-off (requests held before `ev.ready` never count as liveness failures), ≤ 60 requests in flight (the rest wait locally), ping every 20 s, half-open detection (`visibilitychange`/`online`/`pageshow`/clock jump → ping, 2.5 s, else reconnect), full-jitter back-off, ≤ 12 attempts/min, the §8.5 close-code table, `/api/me` probe after a failure before `ev.ready`, `ws:`/`wss:` from `location.protocol`.

## 7. `core/store.js` (all server-derived state, §7.6(9), §8, §9.1, §9.6)
```js
import { store, prefs } from '../core/store.js';
```
### 7.1 State (read-only – never mutate these objects; use the functions)
```js
store.me            // User + {show_last_seen, must_change_password}   (null before the first ev.ready)
store.workspace     // {name, registration_open}
store.limits        // §7.3 limits {max_upload_bytes, max_body_chars, edit_window_s, delete_window_s, max_pinned_chats, max_pinned_messages, max_group_members, max_forward_messages, max_forward_chats}
store.instanceId    // string|null      store.epoch → number (increments on every ev.ready)      store.isReady → bool (an ev.ready was applied in this session)
store.connection    // { state: socket.state, banner: bool /* true once disconnected > 2 s */, online: bool /* navigator.onLine */, outdated: bool }
store.ui            // { activeChatId:int|null, visible:bool, focused:bool, soundBlocked:bool, …anything set through setUi }
```
Chat objects are the §7.2 `Chat` verbatim (`id kind title description is_default only_admins_post created_by created_at last_activity_at peer_id members pinned_message_ids pinned_messages last_message_id last_message me{…}`) after the client merge rules; message objects are §7.2 `Message` where `status` is the client-derived value (§8.1) for own messages.

### 7.2 Lookups and derived helpers
```js
store.getUser(id) → User|undefined                  store.users() → User[] (directory incl. me)    store.userName(id) → display_name | "Unknown user"
store.getChat(id) → Chat|undefined                  store.chats() → Chat[] (unsorted)
store.chatList({ filter = 'all'|'unread'|'groups', query = '', archived = false } = {}) → Chat[]
      // sorted as §3.2(7): pinned (pinned_at desc) first, then last_activity_at desc; archived chats only when `archived:true` (then ONLY archived ones); `query` filters by title / peer name / @username (fold); archived pinned never exist
store.chatTitle(chat) → string                      // group title; direct → peer display_name; self-chat → "You"
store.chatPeerId(chat) → int|null    store.chatPeer(chat) → User|undefined    store.isSelfChat(chat) → bool
store.directChatWith(userId) → Chat|undefined       // existing listed direct chat with that user (own id → self-chat)
store.memberOf(chat, userId) → member|undefined     store.myMember(chat) → member|undefined      // {user_id, role, delivered_up_to, read_up_to}
store.isGroupAdmin(chat, userId = me.id) → bool     store.isMuted(chat) → bool       // me.muted_until > serverNow()
store.canPost(chat) → { ok:true } | { ok:false, reason:'admins_only'|'peer_disabled'|'not_member', text }   // §9.7 composer-disabled explanations
store.totalUnread() → { count, display }            // §9.4 N (unarchived unmuted unread + muted-unarchived mentions); display "99+" capped
store.messageStatus(chatId, message) → 'sent'|'delivered'|'read'|null    // pure derivation §8.1 (store keeps m.status in sync; use this when you hold your own copy)
store.summarize(message) → string                   // §9.10 one-line preview text: "Photo", "Audio (0:12)", file name, "This message was deleted"/"You deleted this message", "Forwarded: …", system body; text truncated to 100 code points, first line only
store.lastPreview(chat) → { prefix, text, deleted, own, message } | null     // for the chat row: prefix "You: " / "Ravi: " (groups) / "" ; null when the chat has no last_message
```
### 7.3 Chat window (the open conversation; §9.6)
Exactly one chat has a window: the active one. Window = `{ lo, hi, has_more_before, has_more_after, seen_hi, items:[Message ascending by id] }` (one contiguous id range; empty window: `lo = hi = 0`; `seen_hi` = see `markSeen`). Items are server messages sorted strictly by id; the chat's outbox bubbles come AFTER them in queue order (§9.6 "Item order"). An `ev.message` for a held id is an upsert; for a new id above `hi` it is appended, for a new id inside `(lo, hi)` it is inserted at its id position (`messages:<id>` type `'insert'`), below `lo` it only updates the chat metadata. Recompute date separators / sender grouping for the neighbours of every inserted or removed item.
```js
store.openChat(chatId) → Promise<OpenInfo>   // sets ui.activeChatId, loads the window (§9.6 "Opening a chat"), resolves
      // OpenInfo = { chatId, lastRead:int /* L, captured at open */, unreadAtOpen:int, dividerId:int|null /* first message after L that is not mine/system/deleted */, scroll:'bottom'|'divider' }
      // rejects SocketError('not_member' …), or SocketError('cancelled') when superseded by another openChat/closeChat/ev.ready (ignore that one); calling it again for the active chat returns the cached OpenInfo without refetching
store.closeChat(chatId)                      // drops the window, sends typing 'stop', clears ui.activeChatId (call from unmount / before opening another chat; openChat does it implicitly for the previous chat)
store.activeChatId → int|null     store.getOpenInfo(chatId) → OpenInfo|null
store.getWindow(chatId) → Window|null        store.getMessage(chatId, id) → Message|undefined   (window only)
store.loadOlder(chatId) → Promise<int>       // chat.history before_id=lo limit 50, prepends; resolves #added (0 when nothing more or a load is already running); rejects on error
store.loadNewer(chatId) → Promise<int>       // after_id=hi, appends
store.jumpTo(chatId, messageId) → Promise<void>      // the chat must be the open chat (else SocketError 'invalid_state'). No-op when held; else chat.history around_id (limit 100) replaces the window. Rejects SocketError('not_found') → toast "Original message is no longer available"
store.jumpToLatest(chatId) → Promise<void>   // no-op when has_more_after=false; else replaces the window with the newest page (the FAB, §9.6)
store.setViewState(chatId, { stuck?:bool, anchorId?:int|null })   // the conversation view reports §9.6 `stickToBottom` (≤ 40 px) and the id of the message anchored at the top of the viewport; used for read receipts, window trimming and the post-reconnect reload (§8.5(c))
store.markSeen(chatId, messageId)            // the scroll / IntersectionObserver handler calls this with the largest message id whose bubble is ≥ 50 % inside the scroll viewport. `win.seen_hi` only rises, ignores calls while the tab is not visible+focused, only accepts held ids, and is 0 after the window was replaced or the chat opened. `window.hi` is NEVER a substitute: a message that was loaded but not scrolled into view is not "seen"
store.seenUpTo(chatId) → int|undefined       // value for msg.send `seen_up_to_id` (used by the outbox): win.seen_hi when has_more_after=false and seen_hi > 0, else undefined (field omitted)
```
Event order during `openChat`: `messages:<id> {type:'reset'}` fires as soon as the first page is set (so the view can already render), for the unread flow a `prepend` event follows with the extra older page, and only then the promise resolves with the `OpenInfo` (a view may also ignore the intermediate events and render once from `store.getWindow(id)` after the promise resolved). `getWindow()` always returns the live object: re-rendering from it is always valid.
The window is capped at 400 items: `loadOlder` trims the newest end (sets `has_more_after`), `loadNewer` and live appends (only while `stuck`) trim the oldest end (sets `has_more_before`).
After `ev.ready` the store drops every window and, if a chat is open, reloads it itself (newest page when `stuck`, else `around_id = anchorId`, fallbacks per §8.5(c)); you receive `messages:<id> {type:'reset'}`.
`ev.chat_removed`, a `not_member` during reload, … reach you as the `chat_removed` event (below).

### 7.4 Writes: `store.request` and the apply functions
```js
store.request(type, d, opts?) → Promise<res.d>    // socket.request + automatic state application of the response:
      // res.message → applyMessage (msg.send/edit/delete(everyone)/react/star)   res.chat → applyChat (chat.*, msg.pin)   res.messages → applyMessage each (msg.forward)
      // receipt.read → applyCounters   profile.update → res.me → store.me      msg.history/search/starred/shared/info are NOT applied
store.applyMessage(message) → void       // idempotent upsert as if it had arrived as ev.message (own responses). Use `store.request`, rarely call directly
store.applyChat(chat) → void             // apply a Chat you got in a `res` (merge rules of §7.6(9): only last_message_id, me.last_read_id, me.cleared_before_id and per member delivered_up_to/read_up_to are max-merged, counters go through the as-of rule using the payload's OWN last_message_id, `last_message` is kept when the held one has a greater id, everything else replaces). `ev.chat_update` events use the same rules but always replace `last_message` (the store handles the event itself). Call it after `chat.open_direct`/`chat.create_group` if you use socket.request directly, BEFORE router.openChat
store.applyCounters(counters) → void     // §8.2 as-of rule
store.refreshChat(chatId) → Promise<Chat|null>   // chat.get (deduped); null on not_member
```
### 7.5 Typing, drafts, receipts
```js
store.typingUsers(chatId) → [{ user_id, state:'typing'|'recording' }]        store.typingLabel(chatId) → { text, state } | null     // "typing…", "Ravi is typing…", "Ravi and Amit are typing…", "3 people are typing…", "recording audio…" (§8.3)
store.emitTyping(chatId, state)          // state 'typing'|'recording'|'stop'. Implements §8.3 for you: ≤ 1 per 2.5 s, auto 'stop' after 5 s silence, no-op in self-chats/non-postable chats. Call on every composer input (state 'typing'), 'stop' when the composer empties / after send.
store.getDraft(chatId) → { text, reply_to_id?, edit_id? } | null       store.setDraft(chatId, draft | null)       // persisted (debounced) in namespaced storage; empty draft ⇒ removed
store.draftChatIds() → int[]
store.requestRead(chatId)                // optional nudge: re-evaluate the §8.2 read rule now (the store already does it on scroll state, focus, visibility, new messages and window changes)
```
Delivered/read receipts are fully automatic (§8.2): `receipt.delivered` carries `Chat.last_message_id` (never the window content) after every `ev.message` and once after `ev.ready`; `receipt.read` carries `window.hi` and is sent only when the chat is open, the tab visible and focused, `has_more_after` is false, `setViewState({stuck:true})` and `hi` is above `members[me].read_up_to` (read receipts on) or `me.last_read_id` (off); it is re-evaluated when `profile.update {read_receipts:true}` succeeds. You only keep `setViewState` and `markSeen` accurate.
Typing entries disappear when the typist's `ev.message` arrives, when that user goes offline (`ev.presence`), on `ev.ready` (`typing:<id>` fires with `[]`) and after the 8 s self-expiry.
`ev.chat_removed` (and a `not_member` found on refresh/reload) deletes the Chat, its window, held counters and typing entries, **keeps drafts**, ignores events for that id for 60 s, and makes the outbox mark that chat's queued items `failed` with `error.code === 'not_member'`; a later `ev.chat_update` for the id creates a fresh Chat with an empty window.

### 7.6 Preferences (device level, `util.storage.getGlobal('prefs')`)
```js
prefs.get(key) → value     prefs.set(key, value)     prefs.all() → object      // emits store event 'prefs' {key, value}
```
Keys and defaults: `theme: 'system'|'light'|'dark' ('system')`, `fontSize: 'small'|'normal'|'large' ('normal')`, `sound: true`, `volume: 0.7 (0..1)`, `previews: true` (show message text in notifications), `enterSends: null` (null = auto: on for desktop, off for touch; use `prefs.enterSends()`), `desktopNotifications: true`, `sentSound: false` (soft tick after a message was sent). Unknown keys are ignored, values are validated.
`prefs.enterSends() → bool` resolves the null default. Applying theme/font to `<html>` is done by main.js.

### 7.7 Events (`store.on(name, fn) → off()`; `once`, `off`, `emit` exist too; **`emit` is for core modules only**)
All handlers run synchronously inside the socket handler (§9.6 rule 8): do not do heavy work in them – schedule DOM patching with `requestAnimationFrame` yourself (never in receipt/notification code).
| event | payload | when |
|---|---|---|
| `ready` | `epoch` | after an `ev.ready` was applied (state replaced; windows dropped; reload of the open chat started) |
| `reset` | – | logout/kick: everything cleared |
| `me` | `me` | `ev.me`, `ev.ready`, `profile.update` response |
| `workspace` | `{name, registration_open}` | `ev.workspace`, `ev.ready` |
| `limits` | `limits` | `ev.ready` |
| `users` | – | any directory change (add/update/presence) – cheap coarse event |
| `user:<id>` | `User` | that user changed (profile, role, disabled, activated, online/last_seen) |
| `presence` | `{ user_id }` | `ev.presence` |
| `chats` | – | the set, order or any preview/unread of any chat changed (coarse; the sidebar subscribes to this + `chat:<id>`) |
| `chat:<id>` | `Chat` | that chat object changed (metadata, members, counters, last_message, derived status of the last message, prefs) |
| `chat_removed` | `{ chat_id, title, kind }` | `ev.chat_removed`, or a `not_member` discovered on reload/refresh. The chat is already gone from `store.chats()` |
| `messages:<chatId>` | `{ type, chatId, ids }` | window of the open chat changed: `type` `'reset'` (window replaced, re-render all; ids omitted) \| `'append'` (ids appended) \| `'insert'` (a message inserted at its id position inside the window; treat like append but at the position given by `store.getWindow(id).items`) \| `'prepend'` (ids prepended, ascending) \| `'update'` (items patched in place: edit, delete, reactions, pin, star, status/ticks, reply_to) \| `'remove'` (ids removed: delete-for-me, clear) \| `'trim'` (`side:'start'|'end'` + ids dropped by the 400 cap) \| `'divider'` (the tab was blurred/hidden while the chat was open and a new message arrived: `store.getOpenInfo(chatId).dividerId` was set to `ids[0]`; put the "N unread messages" divider before it, SPEC 9.6) |
| `message_update` | `Message` | every `ev.message_update` (also for messages the window does not hold): lets views that keep their own copies (Starred list, search results, info drawer pins) patch by id with `store.patchMessage(held, incoming)` |
| `message_removed` | `{ chat_id, message_ids }` | `ev.message_removed` |
| `own_message` | `Message` | an own message (with `client_id`) arrived via event/response/history; the outbox listens to this to drop its bubble. Emitted BEFORE the matching `messages:` event |
| `incoming` | `{ message, chat, notifiable, mention }` | every `ev.message` whose id is above the chat's known `last_message_id` (replayed duplicates and responses never fire it); drives chime/toast/OS notification in `notify.js` |
| `read_sync` | `{ chat_id, counters }` | counters of a chat were replaced by `ev.read_sync` / `receipt.read` response |
| `typing:<chatId>` | `[{user_id,state}]` | typing set changed or an entry expired (8 s self-expiry) |
| `connection` | `store.connection` | socket state / banner / online flag changed |
| `ui` | `{ keys:[…] }` | `store.setUi(patch)` |
| `active_chat` | `{ chatId, prev }` | `openChat`/`closeChat` |
| `prefs` | `{ key, value }` | `prefs.set` |
| `draft:<chatId>` | `draft|null` | `setDraft` |
| `drafts` | – | any draft changed, or the drafts were (re)loaded after `ev.ready` (coarse; the sidebar listens to this one) |
| `outbox:<chatId>` | `{ type:'add'|'update'|'remove', item }` | outbox item changes (§8) |
| `outbox` | – | any outbox change (coarse) |
| `badge` | `{ count, display }` | `totalUnread()` changed |

Other helpers: `store.setUi(patch)`; `store.patchMessage(held, incoming) → held` (mutates a copy you hold yourself, e.g. in the Starred list, the way the store patches its own: content from `incoming`, own `status` never lowers); `store.patchQuotes(messages, incoming) → n` (updates every `reply_to` in `messages` whose `id` equals `incoming.id`: `deleted`, `body` = first 200 code points, `unavailable:false`).
Lifecycle (main.js only): `store.start()`, `store.reset()`.

## 8. `core/outbox.js` and `core/upload.js` (§7.1.1, §9.7, §9.9)
```js
outbox.add({ chat_id, body = '', reply_to_id = null, client_id?, attachment_promise?, preview?, upload? }) → OutboxItem
outbox.items(chatId) → OutboxItem[]       // FIFO, includes failed ones (render AFTER the window items when window.has_more_after is false)
outbox.get(clientId) → OutboxItem|undefined     outbox.all() → OutboxItem[]     outbox.count() → number     outbox.hasPending() → bool
outbox.update(clientId, { progress?, attachment? })   // for upload.js only: progress 0..1 and preview metadata (width/height/duration)
outbox.retry(clientId) → void             // failed → queued (attachment items restart their upload)
outbox.discard(clientId) → void           // removes the item (cancels a running upload)
outbox.start()  outbox.reset({ wipe }) → void       // main.js only
```
`OutboxItem` = `{ client_id, chat_id, owner_id, body, reply_to_id, created_at /* serverNow() at add */, state:'queued'|'uploading'|'sending'|'failed', error:{code,message}|null, progress:number /* 0..1 while uploading */, attachment:{name,size,mime,kind,width,height,duration,url /* blob: preview or null */}|null, attachment_id:string|null, tries:int }`.
A removed chat (`chat_removed`) turns the chat's queued items into `failed` with `error.code 'not_member'` (a running upload is cancelled, the item stays failed). State machine: `queued` (clock icon) → [`uploading`] → `sending` → removed when the server message with the same `client_id` arrives (`own_message`) | `failed` (permanent error, tap to retry/discard). One `msg.send` in flight per chat, FIFO, items behind an `uploading` item wait, a `failed` item does not block. Retryable errors (§7.1.1) stay `queued` and are retried automatically (after reconnect, `retry_after`, 1/2/4 s for `server_error` ×3). `msg.send` carries `seen_up_to_id` computed at transmit time. `outbox.add` replaces a window that has `has_more_after` by the newest page first (§9.6).
Persisted (text items only) in namespaced storage `outbox`; restored after the first `ev.ready` of a page load (items of unknown chats / other users are dropped; unsent attachments are reported with the toast "N unsent attachments were discarded").
Events: `store.on('outbox:<chatId>')`, `store.on('outbox')`.

```js
upload.precheck(file, limits?) → { ok:true } | { ok:false, reason }  // size 0 (empty file or folder) / > limits.max_upload_bytes / a 4096-byte extension-less entry that looks like a folder
upload.enqueueFiles(chatId, files /* File[]|FileList */, caption = '', opts = {}) → { items:OutboxItem[], rejected:[{name, reason}] }
      // opts: { reply_to_id?, asFile?:bool /* skip image downscale */, audioOnly?:bool, duration?:number /* voice notes */ }
      // ≤ 10 files per call, rejects the rest; caption + reply go on the FIRST message only; N files → N outbox items in selection order; ≤ 2 parallel XHR uploads; progress/cancel via the item (`outbox.discard`)
upload.describeError({code,status,message}, limitBytes?) → string       // §9.9(4) texts
class UploadError extends Error { code, status }     // 'cancelled' | 'network' | 'too_large' | 'quota_exceeded' | 'insufficient_storage' | 'blocked_type' | 'unauthorized' | 'rate_limited' | …
```
Image downscale (§9.9(5)) and `X-Meta` width/height/duration/`audio_only` are done inside `upload`. The tray/UI glue stays in `composer.js`.

## 9. `core/router.js` (hash routing, §9.8)
Routes: `#/` (`list`), `#/c/<chatId>` and `#/c/<chatId>/m/<messageId>` (`chat`), `#/starred`, `#/settings[/<tab>]`, `#/admin[/<tab>]`; anything else → `list`.
```js
route = { name:'list'|'chat'|'starred'|'settings'|'admin', path:'/c/12/m/340', chatId?:int, messageId?:int, tab?:string }
router.current → route                    router.start()  (main.js)
router.parse(hash) → route                 router.href(pathOrRoute) → '#/c/12'
router.go(pathOrRoute, { replace? } = {})  // pushState on narrow screens (< 900 px); on wide screens a chat→chat/list move uses replaceState; other targets push. Explicit `replace` wins.
router.openChat(chatId, { messageId, replace })   router.home({ replace })      router.openSettings(tab?)   router.openAdmin(tab?)   router.openStarred()
router.back() → void                       // closes the top layer if any, else history.back() when the app has an earlier in-app entry, else go('/')
router.up() → void                         // logical parent: chat/starred/settings/admin → list
router.on('route', (route, prev) => …) → off()
router.isNarrow() → bool                   // viewport < 900 px
router.setGuard(fn) → void                 // fn(route) → route | null; a returned route redirects (replace). main.js sets it
router.revalidate() → void                 // re-run the guard for the current route (main.js, after ev.ready)
// Layers: anything that must be closed by Escape and the hardware Back button (lightbox, menu, emoji picker, reply/edit bar, drawer, in-chat search, dialogs)
router.pushLayer({ id, priority = 20, close }) → { release(), dismiss(), isTop() }
      // priority convention: dialog 60, lightbox 50, menu/popover/emoji picker 40, reply/edit bar 30, drawer 20, search bar 10.
      // On narrow screens pushLayer adds a history entry so the hardware Back button calls `close()`; `release()` = the view closed it itself (removes the entry); `dismiss()` = close it (calls `close()` once)
router.closeTopLayer() → bool             // used by the global Escape handler: highest priority, latest first
```
Route changes close every open layer. Never put auth state in the hash.

## 10. `core/ui.js` (§9.8 accessibility)
```js
ui.toast(message, { type = 'info'|'success'|'error', timeout = 4000 /* errors 6000, 0 = sticky */, key?, action?:{ label, onClick }, onClick? } = {}) → { dismiss(), update(message) }
      // `key` replaces an existing toast with the same key; max 4 visible; `onClick` fires on a click of the toast body (then it closes)
ui.confirm({ title, message, confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false }) → Promise<boolean>
ui.prompt({ title, label, value = '', placeholder = '', confirmLabel = 'OK', maxLength?, validate?(v) → errorString|null, multiline = false }) → Promise<string|null>
ui.dialog({ title, content /* Node|string */, actions = [{ label, value?, primary?, danger?, disabled?, onClick?(handle) → false|Promise<false|any> /* false = keep open */ }], size = 'md'|'sm'|'lg'|'full', dismissable = true, className, initialFocus /* selector|Element */, role = 'dialog'|'alertdialog', onClose?(result) }) → handle
      // handle = { el /* .dialog-backdrop */, panel /* .dialog */, body /* .dialog-body */, close(result), closed: Promise<result>, setTitle(text), setBusy(bool) }
      // role=dialog aria-modal labelled by its title, focus moves in, is trapped, returns to the opener on close; Escape/Back/backdrop close it (dismissable). `result` = the clicked action's `value` (default its label) or undefined.
ui.popover(content /* Node */, { anchor?:Element, x?, y?, placement = 'bottom-start'|'top-start'|…, label, role = 'dialog', onClose, dismissOnBlur = true }) → { el, close(), reposition() }
      // positioned inside the viewport next to `anchor` or at (x, y); closes on outside pointerdown, Escape, route change, resize, scroll of the page
ui.sheet(content /* Node */, { title, label, onClose }) → { el, body, close() }      // bottom sheet (touch/mobile), same layer/focus rules
ui.menu(items, { anchor?, x?, y?, label, title, sheet = 'auto'|'always'|'never', onClose } = {}) → { close() }
      // items: [{ label, icon?, onSelect(), danger?, disabled?, checked?, hint?, separator?: true }] → role=menu with roving focus (↑ ↓ Home End Enter Space Esc); renders as a popover on pointer devices and as a bottom sheet when `sheet:'always'` or ('auto' and ui.prefersSheet())
ui.prefersSheet() → bool                  // coarse pointer or viewport < 600 px
ui.trapFocus(container, { initialFocus, returnFocus = true, opener } = {}) → release()   // `opener`: element to refocus on release (a caller that makes #app inert first must capture it BEFORE; dialog/sheet/fatal do)
ui.focusables(container) → HTMLElement[]
ui.onLongPress(el, handler, { ms = 450, tolerance = 8, mouse = false } = {}) → off()      // §9.8: pointer-event timer for touch; handler({ x, y, target, event }); suppresses the following click; cancelled by > tolerance px movement
ui.announce(text)                         // polite live-region announcement for screen readers
ui.spinner(size = 20) → HTMLElement       ui.setDrawerOpen(open)       ui.isDrawerOpen()
ui.fatal(title, message, { actionLabel, onAction } = {}) → void          // full-screen notice (used by main.js for 4003/outdated)
ui.peoplePicker({ multi = false, exclude = [], onPick, placeholder = 'Search people', includeDisabled = false } = {}) → { el, input, focus(), getSelected() → User[], clear(), setExclude(ids), destroy() }
      // searchable list of the directory (display name + `@username`; the signed-in user shows "(You)"; disabled users hidden unless includeDisabled; sorted by name; max 300 rows then a hint), used by newchat, newgroup, forward and the drawer's "Add members".
      // single mode: a row click (or Enter in the search box while a query is typed) calls onPick(user). multi mode: rows toggle and onPick(user, selected, allSelected) fires on every toggle; getSelected() returns the chosen users.
      // arrow keys move between the search box and the rows; empty result text: `No chats or people match "<q>"`. Append `el` to your dialog; call destroy() when the owner closes (it also detaches itself once `el` left the page).
```

## 11. `core/notify.js`, `core/sound.js` (§9.4)
```js
notify.start()  sound.start()                      // main.js only
notify.status() → { state:'enabled'|'blocked'|'default'|'unavailable', text, why, note }   // note = "Alerts only work while DeskTalk is open on screen." on touch devices. For Settings → Notifications: "Desktop notifications: enabled" / "blocked in browser settings" / "not available on this connection (http)"; `why` = the explanation shown behind [Why?]; also adds the phone note
notify.requestPermission() → Promise<state>        // MUST be called from a user click
notify.isSupported() → bool                        // isSecureContext && 'Notification' in window
notify.closeFor(chatId)                            // close the OS notification of a chat
sound.state() → 'locked'|'unlocked'|'unsupported'   sound.unlock()    // called on first user gesture automatically
sound.chime({ chatId, mention = false, force = false }) → bool    // throttled (§9.4); false when blocked/throttled/off
sound.sent() → void        sound.test() → void     // soft "sent" tick; settings "Play test sound"
```
`notify.js` reacts to store events itself (title/favicon badge, chime, toast, OS notification, title blink, resume summary, multi-tab leader election); views do not call it except the Settings/status helpers above. A message toast click does `router.openChat(chatId)`.

---------------------------------------------------------------------------------------------------

## 12. `css/base.css` contract (what other stylesheets may rely on)
### 12.1 Theme
Light by default; dark when `prefers-color-scheme: dark` unless `html[data-theme="light"]`; forced dark with `html[data-theme="dark"]`. Always style with the tokens, never with literal colours, so both themes work.
Colour tokens: `--bg-app --bg-panel --bg-panel-2 --bg-hover --bg-active --bg-input --bg-chat --bg-elevated --bg-overlay`, `--text --text-secondary --text-tertiary --text-on-accent --text-link`, `--border --border-strong --divider`,
`--accent --accent-hover --accent-soft --accent-text`, `--danger --danger-soft --success --warning --warning-soft --info`, `--focus`,
message tokens `--bubble-in-bg --bubble-in-text --bubble-out-bg --bubble-out-text --bubble-meta-in --bubble-meta-out --tick-sent --tick-read --bubble-system-bg --bubble-system-text --bubble-quote-in --bubble-quote-out --mention-bg --flash`,
palette `--sc-0 … --sc-11` (sender-name colours, ≥ 4.5:1 on both bubble backgrounds) and `--av-0 … --av-11` (avatar backgrounds with white initials, ≥ 4.5:1).
Other tokens: `--font` (system stack), `--font-mono`, `--fs-scale` (1 / 0.9 / 1.15 from `data-font`), `--fs-xs --fs-sm --fs-md --fs-lg --fs-xl` (already multiplied by `--fs-scale`), `--sp-1 … --sp-8` (4 px steps), `--r-sm --r-md --r-lg --r-full`, `--shadow-1 --shadow-2`, `--dur` (150 ms, 0 under reduced motion), `--ease`,
z-index scale `--z-pane --z-drawer --z-banner --z-menu --z-dialog --z-toast --z-lightbox`, layout `--sidebar-w` (clamp 320–420 px), `--drawer-w`, `--header-h` (56 px), `--app-h` (set by JS from `visualViewport`, falls back to 100dvh), safe-area `--safe-top --safe-right --safe-bottom --safe-left`. JS also sets `--app-top` (the visual viewport offset, used by `.app`).
### 12.2 Layout classes
`#app.app` is `position:fixed; inset:0; height:var(--app-h)` – a flex row of `.pane`s. Widths: ≥ 900 px three-pane (`.pane-sidebar` fixed `--sidebar-w`, `.pane-main` flex 1, `.pane-drawer` `--drawer-w` when `[data-drawer=open]`); 600–899 px two-pane (the drawer overlays `.pane-main` from the right); < 600 px single pane: `[data-view=list]` shows only `.pane-sidebar`, every other `data-view` shows only `.pane-main`, the open drawer covers the screen.
Helpers for pane contents: `.pane-header` (56 px bar, flex, `padding-top: var(--safe-top)` on narrow), `.pane-body` (flex 1, scrolls), `.scroll-y` (`overflow-y:auto; overscroll-behavior:contain; -webkit-overflow-scrolling:touch`), `.pane-footer`.
Visibility switches for narrow layouts: `.only-narrow` / `.only-wide` (< / ≥ 900 px) and `.only-touch` (`pointer:coarse`).
### 12.3 Utility and component classes
`.hidden` / `[hidden]` (display none), `.sr-only`, `.row` (flex row, centred, gap `--sp-2`), `.col`, `.grow`, `.shrink-0`, `.gap-1 … .gap-4`, `.truncate` (one-line ellipsis), `.muted` (`--text-secondary`), `.subtle` (`--text-tertiary`), `.mono`, `.center`, `.divider`, `.spacer`.
Controls: `.btn` (+ `.btn-primary .btn-secondary .btn-ghost .btn-danger .btn-sm .btn-block`), `.btn-icon` (40×40 round icon button, 44×44 on coarse pointers), `.input` (text input/select/textarea look, 16 px on coarse pointers), `.field` (label + control wrapper), `.label`, `.hint`, `.error-text`, `.switch` (use `<label class="switch"><input type="checkbox"><span class="track"></span></label>`), `.chip`, `.card`, `.badge` (`.badge-unread` accent, `.badge-muted` grey, `.badge-mention`), `.spinner`, `.banner` (`.banner-warn .banner-info .banner-danger`), `.list-row` (hover/active/focus states, `[aria-selected=true]`/`.active`), `.menu-item`, `.tick` (`.tick-sent .tick-delivered .tick-read` colour the `checks`/`check` icons via `currentColor`), `.empty-state`.
Avatars: `.avatar` + size `.av-xs .av-sm .av-md .av-lg .av-xl` (24/32/40/56/96 px) + colour `.av-c0 … .av-c11`; `.av-dot` (+ `.on`) presence dot bottom-right. Sender names: `.sender-c0 … .sender-c11`.
ui.js / main.js components (styled here): `.toast-root .toast .toast-info .toast-success .toast-error .toast-text .toast-link .toast-action .toast-close`, `.dialog-backdrop .dialog .dialog-sm|lg|full .dialog-header .dialog-title .dialog-body .dialog-message .dialog-actions`, `.popover`, `.menu .menu-item[.danger] .menu-icon .menu-label .menu-hint .menu-check .menu-separator`, `.sheet-backdrop .sheet .sheet-handle .sheet-title`, `.conn-banner`, `.boot .boot-card .boot-icon .boot-spinner`, `.fatal .fatal-card`, `.empty-pane .empty-pane-icon .chips`, `.view-missing`, `.unsupported`, people picker `.people-picker .people-list .people-row .people-name .people-username .people-check[.on] .people-note` (override in panels.css if you need another look).
### 12.4 Behaviour baked into base.css
`html,body{height:100%; overscroll-behavior:none}`, `-webkit-text-size-adjust:100%`, font stack `system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif`, `:focus-visible` ring (`--focus`), `[dir=auto]`/`.bidi{unicode-bidi:plaintext; text-align:start}`, `prefers-reduced-motion` disables transitions/animations, inputs ≥ 16 px and `.btn-icon` ≥ 44 px on `(pointer:coarse)`, `.no-callout{-webkit-touch-callout:none; user-select:none}` (message bubble chrome).

## 13. Selectors/attributes other modules must provide
* Sidebar search input: `data-shortcut="search"`.
* Roving-tabindex message list, labels, ARIA: §9.8 (ui-conv).
* Anything that opens an overlay layer must use `router.pushLayer` so Escape/Back work.
