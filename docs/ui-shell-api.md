# ui-shell API (what other owners may call)

Owner: ui-shell. Files: `web/js/views/{auth,sidebar,newchat,newgroup,settings,admin,searchpanel,starred}.js`, `web/css/{auth,sidebar,panels}.css`.
The core contract is `docs/ui-core-api.md`; SPEC section numbers (`§`) refer to `docs/SPEC.md`. Everything below is a named ES-module export.
`Chat` / `User` are the §7.2 shapes as held by `store`.

Views mounted by `main.js` (`mount(container, ctx)`, nothing else is called from outside): `auth`, `sidebar`, `starred`, `settings`, `admin`.
The other modules are helpers; the ones ui-conv may use are listed here.

---------------------------------------------------------------------------------------------------

## `views/newchat.js`

```js
open() → Promise<void>
```
Opens the "New chat" dialog: a "New group" row and the people picker (§9.2 sidebar "New chat"). Picking a person runs `openDirect`; the "New group" row closes the dialog and runs `newgroup.open()`. Resolves when the dialog is closed.

```js
openDirect(userId, { replace = false } = {}) → Promise<Chat|null>
```
The ONE way to start/open a direct chat with a person (new-chat dialog, search panel, info drawer "Message" button, contact cards). Uses the existing chat when `store.directChatWith(userId)` knows it; otherwise `store.request('chat.open_direct', {user_id})` (the store applies the returned chat) and then `router.openChat(chat.id, { replace })`. Never throws: errors become an error toast (`errorText`) and the promise resolves `null`. `userId` may be `store.me.id` (self chat "You").

## `views/newgroup.js`

```js
open({ preselect = [], title = '' } = {}) → Promise<Chat|null>
```
Opens the "New group" dialog (title 1..60 code points, optional description <= 500, members picker; `preselect` = user ids ticked at the start; the creator is never listed). On success the new chat is already in the store and open (`router.openChat`); resolves with that chat, or `null` when cancelled / on error (error shown inside the dialog). Used by the sidebar menu and by `newchat.js`.

## `views/sidebar.js` — chat actions shared with the info drawer

```js
chatMenuItems(chat) → Array<ui.menu item>           // the sidebar row menu: Pin/Unpin, Mute for 8 hours / 1 week / always | Unmute, Archive/Unarchive, Clear chat, Leave group
openChatMenu(chat, { anchor?, x?, y? } = {}) → { close() }   // ui.menu(chatMenuItems(chat), …) (bottom sheet on touch)
setMuted(chat, choice) → Promise<boolean>            // choice: '8h' | '1w' | 'always' | 'off' ; chat.prefs {muted_until} derived from the server clock (§8.5); false after an error toast
setPinned(chat, pinned) → Promise<boolean>           // chat.prefs {pinned}; enforces limits.max_pinned_chats client-side with a toast; archived chats cannot be pinned
setArchived(chat, archived) → Promise<boolean>       // chat.prefs {archived}
clearChat(chat) → Promise<boolean>                   // confirm dialog, then chat.clear ("for me only")
leaveChat(chat) → Promise<boolean>                   // confirm dialog, then chat.leave; groups other than the default one only; the open chat is closed by main.js on ev.chat_removed
```
All of them show their own error toasts and never throw; the boolean is "the change happened". The mute menu items are also available as `muteItems(chat)`.

```js
muteItems(chat) → Array<ui.menu item>                // only the mute entries ("Mute for 8 hours", "Mute for 1 week", "Mute always" or "Unmute")
```

## Pane views (main-pane routes, no exports besides `mount`)

| module | route | notes |
|---|---|---|
| `views/settings.js` | `#/settings[/<tab>]` | tabs `profile` (default), `privacy`, `notifications`, `appearance`, `account`. `router.openSettings('notifications')` jumps to a tab. |
| `views/admin.js` | `#/admin[/<tab>]` | tabs `users` (default), `workspace`, `stats`, `audit`. Admins only (a member sees "Admins only" and `router.home`). |
| `views/starred.js` | `#/starred` | the Starred messages list (§9.2, §9.10). |
| `views/sidebar.js` | `#pane-sidebar` | chat list, filters, search (`searchpanel.js`), archived section. |
| `views/auth.js` | `#auth-root` | setup / login / register / forced change password. |

## What ui-shell expects from ui-conv (see `docs/ui-conv-api.md` for the exact names)

* `views/messageview.js` (or `lib/richtext.js`): a function that renders ONE message as a read-only bubble/card (used by Starred) and a rich-text function with search-term highlighting (used by search results).
* Jumping to a message is always `router.openChat(chatId, { messageId })` (route `#/c/<chat>/m/<message>`); `conversation.js` performs `store.jumpTo`.

## Expectations on ui-core that go beyond `docs/ui-core-api.md`

* `ui.peoplePicker({ multi, exclude, onPick })` (§1 ownership list) for `newchat.js` / `newgroup.js`.
* `createSearcher({ chat_id? })` from `core/api.js` (§1) for `searchpanel.js`.
