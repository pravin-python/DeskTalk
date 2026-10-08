# ui-shell API (what other owners may call)

Owner: ui-shell. Files: `web/js/views/{auth,sidebar,newchat,newgroup,settings,admin,searchpanel,starred}.js`, `web/css/{auth,sidebar,panels}.css`.
The core contract is `docs/ui-core-api.md`, the conversation side is `docs/ui-conv-api.md`; SPEC section numbers (`§`) refer to `docs/SPEC.md`.
Everything below is a named ES-module export. `Chat` / `User` are the §7.2 shapes as held by `store`.

Views mounted by `main.js` (`mount(container, ctx)`, nothing else is called from outside): `auth`, `sidebar`, `starred`, `settings`, `admin`.
The other modules are helpers; the ones ui-conv may use are listed in sections 1 and 2.

---------------------------------------------------------------------------------------------------

## 1. `views/newchat.js` and `views/newgroup.js`

```js
openDirect(userId, { replace = false } = {}) → Promise<Chat|null>      // views/newchat.js
```
The ONE way to start/open a direct chat with a person (new-chat dialog, search panel, admin "Message", info drawer "Message" button, contact cards). Uses the existing chat when
`store.directChatWith(userId)` knows it; otherwise `store.request('chat.open_direct', {user_id})` (the store applies the returned chat) and then `router.openChat(chat.id, { replace })`.
Never throws: an error becomes an error toast (`errorText`) and the promise resolves `null`. `userId` may be `store.me.id` (self chat "You").

```js
open() → Promise<void>                                                    // views/newchat.js
```
The "New chat" dialog: a "New group" row plus `ui.peoplePicker({multi:false})`. Picking a person runs `openDirect`; the "New group" row runs `newgroup.open()`. Resolves when the dialog is closed and the chosen action has run.

```js
open() → Promise<Chat|null>                                               // views/newgroup.js
```
The "New group" dialog (title 1..60 code points, optional description <= 500, members through `ui.peoplePicker({multi:true})`; the creator is never listed). On success the new chat is already in the store and open
(`router.openChat`) and the promise resolves with it; `null` when cancelled. Errors are shown inside the dialog (it stays open).

## 2. `views/sidebar.js` — chat actions shared with the info drawer

```js
chatMenuItems(chat) → Array<ui.menu item>          // the sidebar row menu: Pin/Unpin (not for archived chats), Mute for 8 hours / 1 week / always | Unmute, Archive/Unarchive, Clear chat, Leave group (groups except the default one)
openChatMenu(chat, { anchor?, x?, y? } = {}) → { close() }      // ui.menu(chatMenuItems(chat), …) – a popover, or a bottom sheet on touch
muteItems(chat) → Array<ui.menu item>              // only the mute entries
setMuted(chat, choice) → Promise<boolean>          // choice: '8h' | '1w' | 'always' | 'off'; sends chat.prefs {muted_until} computed from the server clock (§8.5)
setPinned(chat, pinned) → Promise<boolean>         // chat.prefs {pinned}; checks limits.max_pinned_chats first (toast); an archived chat is unarchived and pinned in one request
setArchived(chat, archived) → Promise<boolean>     // chat.prefs {archived}
clearChat(chat) → Promise<boolean>                 // confirm dialog ("for me only"), then chat.clear
leaveChat(chat) → Promise<boolean>                 // confirm dialog, then chat.leave; groups other than the default one only (false otherwise); main.js closes the open chat on ev.chat_removed
```
All of them show their own error toasts and never throw; the boolean is "the change happened" (`false` after a cancelled dialog, a refusal or an error).

## 3. Pane views and their routes (nothing to call)

| module | route / slot | notes |
|---|---|---|
| `views/settings.js` | `#/settings[/<tab>]` | tabs `profile` (default), `privacy`, `notifications`, `appearance`, `account` (change password, sessions, sign out). `router.openSettings('notifications')` jumps to a tab. |
| `views/admin.js` | `#/admin[/<tab>]` | tabs `users` (default), `workspace`, `stats`, `audit`. Admins only: a member sees "This area is for admins only." |
| `views/starred.js` | `#/starred` | newest-first starred messages, paged 30, rendered with `messageview.createMessageCard`, live patching (`message_update`, `message_removed`, `chat_removed`, reload on `ready`). |
| `views/sidebar.js` | `#pane-sidebar` | header (avatar → settings, "+" New chat, menu: New group / Starred / Settings / Administration), search box (`data-shortcut="search"`), filters All/Unread/Groups, archived section, chat rows. |
| `views/auth.js` | `#auth-root` | setup (first run, asks for the setup code) / login / register (asks for the join code) / forced change password. |
| `views/searchpanel.js` | inside the sidebar | Chats + People (local) and Messages (`createSearcher` + `messageview.createSearchResult`). |

## 4. ui-shell internal helpers (exported only so that its own views share them; do not depend on them from ui-conv)

* `views/auth.js`: `createField`, `createForm`, `passwordRules`, `describeError` (form fields with inline errors, busy state, rate-limit cooldown; used by Settings and Admin).
* `views/settings.js`: `mountTabbed(container, {title, base, tabs, route, banner, env})`, `switchRow`, `radioGroup` (used by Admin).

## 5. What ui-shell calls in the other owners' modules

* core: `ui.peoplePicker`, `createSearcher` (`core/api.js`), `runPaced` (`core/util.js`, "Add employees"), `store.*`, `router.*`, `ui.*`, `notify.status/requestPermission`, `sound.test/unlock`, `prefs`.
* ui-conv: `views/messageview.js` → `createMessageCard(message, {onOpen, showChat, onChanged})` (Starred) and `createSearchResult(message, {query, showChat, onOpen})` (search panel, loaded on first use).
  Jumping to a message is always `router.openChat(chatId, { messageId })`.
