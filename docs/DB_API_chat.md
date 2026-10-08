# DB API: chats, messages, receipts (`db_chats`, `db_messages`, `db_receipts`)

Manual of the db-chat layer for the hub (`chatd/hub.py`). Contract: `docs/SPEC.md` (§3 data model, §7 protocol, §8
delivery). Every function below is **synchronous** and has the signature `fn(conn, ...)`; the hub runs it through
`await db.run(fn, *args)` (writer, already inside `BEGIN IMMEDIATE`) or `await db.run_read(fn, *args)` (reader,
`query_only`). Functions never commit, roll back, run scripts, touch asyncio or the hub. All of them are re-exported by
the `chatd.db` facade (`db.msg_send`, `db.ChatError`, ...), so `db.<name>` and `db_messages.<name>` are the same object.

* Read functions (reader): `chat_get`, `chat_history`, `msg_info`, `msg_search`, `msg_starred`, `msg_shared`,
  `build_ready`, `load_membership_index`, `direct_chat_id`, `message_chat_id`, `admin_user_chat_ids`, `build_chats`,
  `build_chat`, `counters*`. They return the `d` of the `res` frame directly (or their documented value).
* Write functions (writer): everything else. They return an **outcome** dict (section 2).
* Every request function re-reads `users.disabled` of the caller first: a disabled or unknown caller raises
  `ChatError('unauthorized')`, after which the hub runs `hub.revoke(user_id, reason='disabled')` (SPEC 7.1.1 (e)).

## 1. Errors

`db_chats.ChatError(code, msg, reason=None, retry_after=None, chat_id=None, message_id=None)` (an `Exception`; fields of
the same names; `err.to_err()` returns the `err` object of a failed `res`: `{code, msg}` plus the optional keys that are
set). Codes are exactly those of SPEC 7.1.1 and are raised in its evaluation order (the per-request order is listed in
section 4). `ChatError` is a **subclass of `db.RequestError`** (db-core's class for the users/admin side; same
constructor and fields), so one `except db.RequestError as exc: res = {"ok": False, "err": exc.to_err()}` handles
every db function.
Any other exception is a bug => `server_error`. `msg_search`/`msg_shared` also turn an interrupted query
(`sqlite3.OperationalError: interrupted`) into `ChatError('server_busy', retry_after=2.0)`; `Database.run_read` raises
`db.ServerBusy` for the same condition when the hub passes `interrupt_after`.

Shape errors (`bad_request`) are re-checked inside each function (types, ranges, lengths), so the hub's synchronous
validation is a fast path, not the only line of defence. A lone surrogate in any text parameter (`body`, `client_id`,
`attachment_id`, `emoji`, `q`, `scope`, `kind`, titles, descriptions, `display_name`, `role`) is `bad_request` with
`reason:'invalid_text'` (`db_chats.reject_invalid_text`, built on `util.has_lone_surrogate`), checked first, before
authorisation and target resolution (SPEC 7.1).

## 2. The outcome record (write functions)

```
out = await db.run(db.msg_edit, user_id, message_id, body)
out["res"]       # the `d` of the res frame
out["events"]    # ordered fan-out plan (section 2.1): enqueue ALL of them before the res
out["index"]     # membership-index entries to install (section 2.3)
out["noop"]      # True when nothing changed: events is [] and nothing was written (SPEC 7.3 no-op rule)
```

The remaining keys are the post-write record of SPEC 7.6(6); they are always present (empty values when a request has
nothing to report) so the hub never queries the database between commit and fan-out.

| key | value |
|---|---|
| `recipients` | list, one entry per current LISTED member, ascending `user_id`: `{user_id, history_from_id, cleared_before_id, last_read_id, delivered_id, read_receipt_id, disabled:bool, activated:bool, read_receipts:bool, role}` (`role` = chat role, an extra). Read after all writes by every message-scoped and chat-scoped function, `chat.prefs`, `chat.clear`, `receipt.read` and a one-item `receipt.delivered`; empty for `admin_update_user` and the no-op paths of `msg.*` and `msg.forward` retries. |
| `hidden`, `starred`, `quote_hidden` | `set` of user ids: a `hidden_messages` row for the message / a `stars` row for it (read after the write: empty for everybody after delete-for-everyone) / a `hidden_messages` row for the QUOTED message (empty without `reply_to_id`). |
| `quoted` | `None`, or `{id, sender_id, kind, body (first 200 code points, '' when deleted), attachment_name, deleted:bool}`. |
| `sender_status` | the SPEC 8.1 status of the message for its sender (`'sent'`, `'delivered'`, `'read'`, `None` for self-chat/system), computed with `db.status_for` from `recipients`. |
| `deduped` | `True` when `msg.send` / `msg.forward` returned existing messages (no events): the hub refunds the rate-limit tokens. |
| `read_sync` | `{user_id: Counters}` for exactly the users that need an `ev.read_sync` (SPEC 7.6(6)): own send/forward when `last_read_id` advanced; delete-for-everyone: members for whom the message was visible, unread and not their own; edit: users whose mention row was added/removed and for whom the message is visible and unread; delete-for-me: the actor when a counter changed; `chat.clear` and `receipt.read` the actor. |
| `receipt` | `None`, or `{chat_id, user_id, delivered_up_to, read_up_to, audience, authors}`: the actor's two ABSOLUTE public watermarks and the SPEC 8.2 audience. `audience` is COMPLETE and de-duplicated (the actor first, then the current authors of non-system messages in the advanced range): do not add the actor again; `authors` is the same list without the actor. `receipts` is the list of all of them (`receipt_delivered` with several items; `receipt` is the single entry of a one-item request, otherwise `None`). |
| `chats` | `{(viewer_id, chat_id): Chat}` for every `ev.chat_update` and `res{chat}` of the request, built by `db.build_chats` after all inserts. |

Extra keys (documented per request): `parts` (message-scoped: the viewer-independent `Parts`, section 3),
`self_chat`, `records` (`msg_forward`), `released_attachments` (`msg_delete`), `added` / `removed` / `promoted`
(membership requests), `touched_chats` (`admin_update_user`).

`Counters` = `{chat_id, last_read_id, last_message_id, unread, unread_mentions, first_unread_mention_id}`, capped at 1000
(`db_chats.configure_limits(unread_cap=...)`), valid as of `last_message_id` read in the same transaction.

### 2.1 Events (`out["events"]`)

Each entry: `{"t": "ev.message", "cls": "durable", "durable": True, "key": None, "groups": [{"user_ids": [3, 7],
"d": {...}}, ...]}`. `cls` is the SPEC 6 frame class: `"durable"` for every event except `ev.receipt`, which is
`"keyed"` (`durable` is `False`, `key` is `("receipt", chat_id, user_id)`); `"ephemeral"` never occurs in an outcome.

* The hub encodes `{"t": event["t"], "d": group["d"]}` **once per group** and enqueues the same string on every
  connection of every user in `group["user_ids"]` (`None` = every connection of every user). Variants of one message
  are separate groups (client_id/status only in the sender's group, own star, quote state), so no string is ever shared
  across variants (SPEC 6).
* Groups with an empty user list are dropped; an event with no group is never emitted.
* `cls == "keyed"` is an `ev.receipt` (SPEC 6/7.3: replaces a still-queued frame with the same `key`, is NEVER dropped
  by the ephemeral shedding rule and counts as a durable frame for the 512-frame limit). Map it to the transport's keyed
  send (`ws.send_text(text, durable=ev["durable"], key=ev["key"])` is the current transport signature, whose `durable=False`
  path must not shed keyed frames) and keep pending connections' receipts in `conn.backlog_receipts` (SPEC 7.6(7)).
* Order of the list = enqueue order (SPEC 7.3 matrix). Enqueue all of them synchronously, then the `res`.
* Directive entries (not frames): `{"t": "hub.revoke", "user_id": int, "reason": "disabled", ...}` appears in
  `admin_update_user` between `ev.user_update` and the promotions: call `hub.revoke(user_id, reason='disabled')` at that
  position (it sends `ev.kicked` and closes `4001`). Treat any `t` not starting with `ev.` as a directive.
* All `d` payloads are JSON-serialisable plain data. `users` inside `ev.user_update` / `ev.me` have no `online` key:
  the hub adds the truthful presence value.

### 2.2 Chat locks (SPEC 7.6(2))

The hub takes the chat lock BEFORE calling the function. Helpers that tell it which chat:

* `db_messages.message_chat_id(conn, message_id) -> Optional[int]` for `msg.edit` / `msg.delete everyone` / `msg.react`
  (reader; unknown id: take no lock, the call fails with `not_found`). `msg.pin` has `chat_id`; `msg.send` has `chat_id`.
* `db_chats.direct_chat_id(conn, user_id, peer_id) -> Optional[int]` for `chat.open_direct` on an existing chat.
* `db_chats.admin_user_chat_ids(conn, user_id) -> List[int]` (ascending) for `admin.update_user`: the default group plus
  every non-default group the target administers. Compare the outcome's `touched_chats` with the locked set; retry
  when it is larger.
* `msg.forward`: lock `chat_ids` ascending.

### 2.3 Membership index (`out["index"]`, `load_membership_index`)

Entry: `{chat_id, kind, only_admins_post:bool, members:{user_id: chat role} (LISTED members only), peer_disabled:bool,
self_chat:bool}`. `load_membership_index(conn) -> {chat_id: entry}` builds the whole index for `hub.start()` (two
statements). Every write function that changes kind/settings/members/roles/listing/peer state returns the fresh entries
in `out["index"]`; replace `index[entry["chat_id"]]` under the chat lock before releasing it. A chat whose last member
left keeps an entry with `members == {}`. `index_entry(conn, chat_id)` builds one entry (also usable by `hub.create_user`
/ `hub.external_change` after db-core's registration).

Event builders for the hub's own flows (registration, `external_change`): `db_chats.chat_members_event(chat_id,
user_ids, added=(), removed=(), updated=()) -> event` (the arrays are sorted ascending by user id),
`db_chats.chat_update_event(conn, chat_id, user_ids=None, chats=None) -> event` and
`db_messages.system_message_event(conn, chat_id, message_id) -> event`.

### 2.4 Process-wide limits

`db_chats.configure_limits(max_body_chars=None, edit_window_s=None, delete_window_s=None, unread_cap=None)`: call once at
startup from `cfg` (`cfg.max_body_chars`, `cfg.edit_window_s`, `cfg.delete_window_s`, `cfg.test_limits.get('unread_cap',
1000)`). Defaults: 8000, 900, 172800, 1000.

## 3. Message serialisation (SPEC 7.2)

* `db_messages.load_parts(conn, message_ids) -> {id: Parts}`: viewer-independent parts (set-based).
* `db_messages.render(parts, is_sender=False, starred=False, state='none', status=None) -> Message dict` and
  `serialize_message_variants(parts, variants, status=None) -> {variant: Message}` with
  `variant = (is_sender, starred, reply_state)`, `reply_state in none|visible|deleted|unavailable`. `client_id` and
  `status` are filled only when `is_sender`. Nested objects are shared between variants (read-only).
* `db_messages.reply_state(parts, floor, quote_hidden)` with `floor = max(history_from_id, cleared_before_id)` of the
  recipient (unavailable iff `quoted.id <= floor` or the recipient hid the quote; else `deleted` / `visible`).
* `db_messages.message_event(t, parts, record) -> event` builds `ev.message` / `ev.message_update` from a record (what
  the write functions already did for `out["events"]`); `message_record(conn, message_id) -> (parts, record)` rebuilds a
  record after any write; `system_message_event(conn, chat_id, message_id)` is the `ev.message` of a system message
  to every current member (use it after `register_user` inserted a `joined`/`created` message).
* `db_messages.serialize_for_viewer(conn, viewer_id, message_ids) -> [Message]` (history, search and starred use it).
* `db_receipts.status_for(recipients, sender_id, message_id, self_chat=False)`, `status_of(ctx, id)`: the §8.1 rule
  without database access.
* `db_chats.build_chats(conn, [(viewer_id, chat_id), ...]) -> {(viewer_id, chat_id): Chat}` is THE Chat builder
  (pairs where the viewer is not a listed member are omitted; run it after all writes; the `members` list object is
  shared by every viewer of a chat, so it may be encoded once). `build_chat(conn, chat_id, viewer_id)` for one pair.

## 4. Requests

`uid` = the caller. "errors" lists codes in the evaluation order of SPEC 7.1.1 for that request (shape and
`unauthorized` first, always). "lock" = chat lock the hub must hold (section 2.2).

### Chats

| request | function (runner) | `res` | events (in order) / notes |
|---|---|---|---|
| `chat.get` | `db_chats.chat_get(conn, uid, chat_id)` (read) | `{chat}` | errors: not_member |
| `chat.open_direct` | `chat_open_direct(conn, uid, user_id)` (write); lock: `direct_chat_id` | `{chat}` | new chat (dormant) or the unlisted peer opening it: `ev.chat_update` to `[uid]` only; an existing listed chat: `noop`, no events. `index` updated. errors: not_found (unknown user), invalid_state (new chat with a disabled peer, reason `peer_disabled`) |
| `chat.create_group` | `chat_create_group(conn, uid, title, member_ids, description=None)` | `{chat}` | `ev.chat_update` (one group per member) then `ev.message` (system `created`). The Chat already holds the system message as `last_message`. No lock. errors: bad_request (title 1..60 after normalisation, description <= 500, more than 200 `member_ids`), not_found (unknown user), invalid_state (disabled member `reason:'disabled'`, > 200 members `reason:'max_members'`) |
| `chat.update` | `chat_update(conn, uid, chat_id, title=None, description=None, only_admins_post=None)` | `{chat}` | `ev.chat_update` (all members) [, `ev.message` `renamed` on a title change]. errors: bad_request (no field), not_member, invalid_state (direct chat), forbidden (not group admin). Equal values: `noop`. |
| `chat.add_members` | `chat_add_members(conn, uid, chat_id, user_ids)` | `{chat}` | `ev.chat_update` (new members) then `ev.chat_members {added}` (existing members) then `ev.message` `added` (all). `added` = their member entries. errors: bad_request (1..50 ids), not_member, not_found, invalid_state (direct/default group), forbidden, invalid_state (disabled new member, > 200). All already members: `noop`. |
| `chat.remove_member` | `chat_remove_member(conn, uid, chat_id, user_id)` | `{chat}` | `ev.chat_removed` (the removed user) then ONE `ev.chat_members {removed[, updated]}` (remaining members) then `ev.message` `removed` (+ `promoted`). errors: not_member, not_found (target not a member), invalid_state (direct/default group; `user_id == uid` reason `use_leave`), forbidden |
| `chat.set_admin` | `chat_set_admin(conn, uid, chat_id, user_id, admin)` | `{chat}` | `ev.chat_members {updated}` (all) then `ev.message` `promoted`/`demoted`. errors: not_member, not_found, invalid_state (direct/default), forbidden, invalid_state (last ENABLED admin, reason `last_admin`). Same role: `noop`. |
| `chat.leave` | `chat_leave(conn, uid, chat_id)` | `{}` | as remove_member (`ev.chat_removed` to the leaver only; `ev.message` `left`); the last member leaving emits only `ev.chat_removed`. errors: not_member, invalid_state (direct/default) |
| `chat.prefs` | `chat_prefs(conn, uid, chat_id, muted_until=None, pinned=None, archived=None)` | `{chat}` | `ev.chat_update` to `[uid]` only. Final-state rule; `muted_until <= now` stored as 0. errors: bad_request (no field; `pinned` and `archived` both true), not_member, invalid_state (pin on a chat that stays archived; 4th pinned chat, reason `pin_limit`). Unchanged: `noop`. No lock. |
| `chat.clear` | `chat_clear(conn, uid, chat_id)` | `{chat}` | to the actor: `ev.chat_update`, `ev.read_sync`, then `ev.receipt` (audience per §8.2) when `delivered_id` rose. Empty/already cleared: `noop`. No lock. |
| `chat.history` | `db_messages.chat_history(conn, uid, chat_id, before_id=None, after_id=None, around_id=None, limit=None)` (read) | `{messages, has_more_before, has_more_after}` | errors: bad_request (two cursors, bad types), not_member, not_found (`around_id` not visible). `limit` clamped to [1, 100], default 50. |

### Messages

| request | function | `res` | events / notes |
|---|---|---|---|
| `msg.send` | `db_messages.msg_send(conn, uid, chat_id, client_id, body=None, attachment_id=None, reply_to_id=None, seen_up_to_id=None)`; lock: `chat_id` | `{message}` (sender variant) | [`ev.chat_update` to the previously unlisted peer: first message of a dormant direct chat], `ev.message` (variant groups), [`ev.receipt`], [`ev.read_sync` (sender)], [`ev.chat_update` to members whose `archived` flipped]. Record + `parts`. `deduped`: existing message returned, no events. errors in SPEC order: bad_request, unauthorized, not_member, forbidden (`only_admins_post`), invalid_state (disabled peer), conflict (client_id of another chat), not_found (reply target / attachment), invalid_state (reply to system/deleted), too_large, bad_request (empty). The `mentions` argument of the wire frame is ignored (not a parameter). |
| `msg.edit` | `msg_edit(conn, uid, message_id, body)`; lock: `message_chat_id` | `{message}` | `ev.message_update` (visible members) [, `ev.read_sync`]. errors: bad_request, not_found, invalid_state (system), forbidden, invalid_state (deleted/non-text/forwarded), too_large / bad_request (body), window_expired. Same body: `noop`. |
| `msg.delete` | `msg_delete(conn, uid, message_id, scope)`; lock (everyone): `message_chat_id` | `everyone`: `{message}`; `me`: `{}` | everyone: `ev.message_update`, [`ev.chat_update` if it was pinned], [`ev.read_sync`]; me: `ev.message_removed`, [`ev.chat_update`], [`ev.read_sync`] (actor only). `released_attachments: [{id, path}]`: delete the FILE (relative to `uploads/`) after COMMIT (the `attachments` row is already deleted when no message references it). errors: not_found, invalid_state (system, either scope), forbidden, window_expired. Already deleted / hidden: `noop`. |
| `msg.react` | `msg_react(conn, uid, message_id, emoji)`; lock: `message_chat_id` | `{message}` | `ev.message_update`. SET semantics. errors: bad_request (emoji), not_found, invalid_state (deleted/system/disabled peer). |
| `msg.star` | `msg_star(conn, uid, message_id, starred)` | `{message}` | `ev.message_update` to `[uid]` only; no lock. |
| `msg.pin` | `msg_pin(conn, uid, chat_id, message_id, pinned)`; lock: `chat_id` | `{chat}` | `ev.message_update`, `ev.chat_update` (all members), [`ev.message` system `pinned`]. errors: not_member, not_found (message not of this chat / not visible), forbidden (`only_admins_post`), invalid_state (deleted/system/disabled peer/5 pins `reason:'pin_limit'`). No-op: `noop`. |
| `msg.forward` | `msg_forward(conn, uid, message_ids, chat_ids, client_id)`; lock: `chat_ids` ascending | `{messages}` | per created message the events of `msg.send`; `records` = one full record per CREATED message (`message_id`, `chat_id`, `parts`, ...); top-level `chats`/`read_sync`/`receipts` aggregate them. `deduped` + `noop` when it was a retry. Order and errors: SPEC 7.4 (shape; unauthorized; retry detection => `conflict` for a partial retry; not_member; not_found; invalid_state; forbidden; invalid_state). The hub takes the tokens (`msg.forward` 1, `msg.send` `len(chat_ids)`) and refunds them on `deduped`. |
| `msg.info` | `msg_info(conn, uid, message_id)` (read) | `{message, recipients:[{user_id, delivered, read, delivered_at, read_at}]}` | errors: not_found, invalid_state (system), forbidden, invalid_state (deleted) |
| `msg.search` | `msg_search(conn, uid, q, chat_id=None, before_id=None, limit=None)` (read) | `{results:[{message, chat_id}], has_more[, total]}` | `total` only on the first page of a chat-scoped search. errors: bad_request (`q` 2..64), not_member, server_busy |
| `msg.starred` | `msg_starred(conn, uid, chat_id=None, before_id=None, limit=None)` (read) | `{messages, has_more}` | not_member for a foreign `chat_id` |
| `msg.shared` | `msg_shared(conn, uid, chat_id, kind, before_id=None, limit=None)` (read) | `{messages, has_more}` | `kind` in media/files/links; errors: bad_request, not_member, server_busy |

### Receipts

| request | function | `res` | events |
|---|---|---|---|
| `receipt.delivered` | `db_receipts.receipt_delivered(conn, uid, items=None, chat_id=None, up_to_id=None)`: pass the wire `d` as is, either `items=[{chat_id, up_to_id}, ...]` (1..50) or the single `chat_id` + `up_to_id`; both, neither or an empty `items` => `bad_request`. No lock. | `{}` | one keyed `ev.receipt` per item whose public value rose. A non-member item fails the whole request: `not_member` with `err.chat_id`. `recipients` is filled for a one-item request. |
| `receipt.read` | `receipt_read(conn, uid, chat_id, up_to_id)`. No lock. | `Counters` | [`ev.receipt`] (public watermark rose), [`ev.read_sync` to `[uid]`] (`last_read_id` rose). errors: bad_request, not_member (carries `err.chat_id`) |

Receipts rows are written only for chats with <= 50 listed members; routing is the actor plus the current authors of the
advanced range (`receipt["audience"]`), never all members.

### Snapshot and admin

* `db_chats.build_ready(conn, uid, default_workspace_name=None, default_registration_open=None)` (read) returns
  `{instance_id, me, workspace:{name, registration_open}, users, chats, server_time}`; the hub adds `protocol`, `limits`
  and overlays `online`/`last_seen`. `chats` = every chat with a listed member row for `uid`, in the SPEC 3.2(7) order.
  Set-based: a constant number of statements regardless of the number of chats.
* `db_chats.admin_update_user(conn, actor_id, user_id, role=None, disabled=None, display_name=None, ip=None)` (write;
  locks: `admin_user_chat_ids`) returns `{user}`; errors in order: bad_request (no field / bad role), not_found, forbidden,
  invalid_state (last active admin, reason `last_admin`), conflict (`name_taken`). Events: `ev.user_update` (all), [`ev.me`
  to the actor when `user_id == actor_id`], role change: `ev.chat_members {updated}` of the default group; disable: the
  `hub.revoke` directive, then per group that lost its last enabled admin (ascending `chat_id`): `ev.chat_members
  {updated:[promoted]}` and the system `promoted` `ev.message`. Writes the `audit_log` row (`admin.update_user`, via
  `db_users.audit`). `index` holds the default group (role change), promoted groups and every direct chat of a user whose
  `disabled` flag changed. The other `admin.*` requests belong to db-core.

## 5. Cross-contract with db-core

* `db_chats.create_everyone_chat(conn, title, created_by, ts) -> chat_id`: inserts the default group row only.
* `db_chats.add_member_row(conn, chat_id, user_id, role, ts, listed=1)`: joins with `history_from_id = last_message_id`
  (0 when empty) and all three watermarks equal to it. Call BEFORE inserting the system message.
* `db_messages.insert_system_message(conn, chat_id, event, actor_id, target_ids, body, title=None, message_id=None,
  bump_activity=True, ts=None) -> message_id`: always sets `chats.last_message_id`; bumps `last_activity_at` except for
  `joined`.
* Used from db-core: `db_users.list_users/get_me/get_user/get_instance_id/workspace_settings/audit/
  drop_attachment_if_unreferenced`.

## 6. SQL helpers (the single visibility predicate)

`db_messages.visible_sql(*extra_floors, m='m', cm='cm', exclude_hidden=True)` is the ONE definition of SPEC 3.1
(`cm.listed = 1 AND m.id > max(cm.history_from_id, cm.cleared_before_id[, extras]) AND NOT hidden`); every query of the
three modules (history, search, shared, starred, pins, `last_message`, unread counters, forward sources, reply targets,
`msg.info`) is built from it. `db_messages.find_visible(conn, viewer_id, message_id)` resolves a message the viewer may
see (else `not_found`). `db_receipts.counter_columns()` is the capped unread/mention select-list used by both
`counters*` and the Chat builder. `EXPLAIN QUERY PLAN` of every statement is index-backed (`tests/test_dbchat_plans.py`).

## 7. Behaviour notes (decisions where the spec leaves room)

* A group admin may delete any message of that group at any time, including their own beyond the delete window.
* `chat.add_members` with an empty list is `bad_request`; unknown users are reported before the kind/role checks.
* `chat.remove_member` of yourself is `invalid_state use_leave` even for a non-admin (applicability precedes the role).
* `msg.forward` retry detection looks only at the copy of the first (chat, source) pair; a retry whose other copies are
  missing is `conflict`.
* Delete-for-me of a message hidden before (idempotent) works for hidden messages of chats still joined; for scope
  `everyone` a message you hid is `not_found`.
* Stars of a removed/leaving member are deleted for that chat only; reactions, receipts rows and pins stay.
* SPEC 3 says "after commit, delete the attachment's file and `attachments` row": the row is deleted INSIDE the
  transaction (`db_users.drop_attachment_if_unreferenced`, only when no message references it) and the hub removes the
  file after COMMIT from `released_attachments`; a crash in between leaves an orphan file that the orphan sweep removes.
* `receipt["audience"]` already contains the actor (SPEC 7.6(6) words it as "authors; the hub adds the actor"): use it
  as is, or use `receipt["authors"]` and add the actor yourself.
