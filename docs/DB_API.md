# DB_API — `Database`, the users/sessions/admin/files functions, `auth.py`, `util.py`, `maintenance.py`

Owner: db-core. This file is exact; SPEC §6.2 is the contract it implements. The chat/message/receipt functions are in
`docs/DB_API_chat.md`. Python 3.8 syntax, stdlib only, SQLite >= 3.24.

## 0. Conventions

* Every `db.<fn>` below is a plain synchronous function `fn(conn, ...)`. Call it as `await database.run(db.fn, *args)`
  (writer, already inside `BEGIN IMMEDIATE`), `await database.run_read(db.fn, *args)` (reader, `query_only`), or with
  `database.run_sync(...)`. Functions marked **[W]** write and must go through `run`; **[R]** only read and may use either.
  They never commit, roll back or open transactions. Rows are plain tuples. All timestamps are float epoch seconds.
* `db.<fn>` is the facade: it also resolves every public name of `db_users`, `db_chats`, `db_messages`, `db_receipts`.
  `import chatd.db_users` first or `import chatd.db` first both work.
* **Errors.** Protocol failures raise `db.RequestError(code, msg, reason=None, retry_after=None, chat_id=None,
  message_id=None)` with `.to_err() -> dict` (the `err` object of a failed `res`) and `.rest_code`. `code` is the WebSocket
  taxonomy of SPEC §7.1.1 (`bad_request`, `unauthorized`, `not_found`, `forbidden`, `invalid_state`, `conflict`, ...);
  failures that only exist on REST carry their REST code directly: `setup_code_required`, `bad_setup_code`,
  `bad_join_code`, `registration_closed`, `disabled`, `quota_exceeded`. `rest_code` converts the two WS spellings that REST
  names differently: `conflict`+`username_taken` -> `username_taken`, `conflict`+`name_taken` -> `name_taken`,
  `invalid_state`+`max_users` -> `registration_closed`; everything else is `code`. (`db_chats.ChatError` has the same fields;
  the hub catches both until the owners unify them: `except db.request_errors() as exc` names both classes.)
  `db.ServerBusy(message, retry_after=1.0)` = `server_busy`/429.
  `util.FatalError(message, code=78)` ends a command (`SchemaTooNew`, `WalUnavailable`, `maintenance.MaintenanceError`
  and `util.AlreadyRunning` are subclasses). Messages never contain payloads.
* **Disabled callers.** Functions that take an acting user id re-read `users.disabled` and raise `unauthorized`
  (`require_active_user`); admin functions then check `role` and raise `forbidden` (`require_admin`).
* **User shape** (`public user`, §7.2 without the live `online`): `{id, username, display_name, status_text, role,
  last_seen (null when hidden or never), disabled, activated, read_receipts}`. **`me`** adds `show_last_seen`,
  `must_change_password`. The hub adds `online`.

## 1. `db.py` — `Database` and module functions

`Database(path, cfg=None, readers=3)` — `cfg` is only used for `cfg.data_dir` (the `backups/` directory of the
pre-migrate copy; default: the directory of `path`). `Database(path, 2)` also works (reader count). Constructible outside a loop.

| call | semantics |
|---|---|
| `start()` | blocking. Creates the parent dir, starts 1 writer + `readers` reader threads, opens the connections (pragmas of §3, `fold()` registered), asserts WAL. Raises `util.FatalError` (code 78): `sqlite3` missing/old, data dir unusable, `WalUnavailable`. Leaves no thread behind on failure. |
| `migrate()` | blocking, on the writer thread. Writes `<data>/backups/pre-migrate-v<old>-<YYYYmmdd-HHMMSS>.db` first (only when a schema exists; sessions emptied in the copy), then applies the ordered `db.MIGRATIONS` (each in its own `BEGIN IMMEDIATE`, version bump in the same tx, safe against a concurrent migrating process). Raises `db.SchemaTooNew` (a `FatalError`, code 78; nothing touched) when the file is newer than `len(MIGRATIONS)`; a non-DeskTalk file raises `FatalError`. Sets `.schema_version`. |
| `open()` | `start()` then `migrate()` (closes again when `migrate` fails). |
| `async run(fn, *a)` | writer thread: `BEGIN IMMEDIATE`, `fn(conn, *a)`, `COMMIT`; any exception -> `ROLLBACK` if `conn.in_transaction`, re-raised. Futures resolve in submission order. Cancelling the awaiting task does not cancel a write that already started. |
| `run_sync(fn, *a)` | blocking `run` for non-asyncio callers (tests, seeding). Never call it from inside a db function or from the loop thread of a running server. |
| `async run_raw(fn, *a)` | writer thread, **no** transaction (`PRAGMA data_version`: `await db.run_raw(db.data_version)`). |
| `async run_read(fn, *a, interrupt_after=None)` | (`interrupt_after` is an additive keyword on top of SPEC §6.2's `run_read(fn, *a)`.) One reader thread, one short deferred `BEGIN`..`COMMIT`. With `interrupt_after` seconds a `threading.Timer` calls `conn.interrupt()` and the interrupted query raises `db.ServerBusy`. |
| `close()` | blocking, idempotent: queued jobs finish, then `wal_checkpoint(TRUNCATE)` + `conn.close()` ON the writer thread and every reader connection on its own thread. The returned object is awaitable, so `await db.close()` is equally valid. Later `run*` raise `db.DatabaseClosed`. |
| `connect_extra()` | new connection for backups/sweeps/CLI work (writer pragmas incl. `synchronous=FULL`, `fold` registered, not `query_only`); the caller closes it and uses it from one thread. |
| `.path`, `.schema_version` | attributes. |

`open_connection` retries `PRAGMA journal_mode=WAL` for `db.WAL_RETRY_SECONDS` (3 s) when another process is converting the same new file
(`delete` answer or `database is locked`) before it raises `WalUnavailable`.

`db.register_functions(conn)` registers `fold()` on a connection you opened with plain `sqlite3.connect` (Database connections already have it).

Module level: `SCHEMA_VERSION` (= `len(MIGRATIONS)`), `MIGRATIONS`, `SCHEMA_V1_DDL` (verbatim SPEC §3; a test compares it
with the spec), `open_for_seeding(cfg) -> Database` (= `Database(<cfg.data_dir>/chat.db, cfg)` + `open()`),
`sqlite_problem() -> Optional[str]` (None when `sqlite3` imports and is >= 3.24), `get_meta(conn, key, default=None)`,
`set_meta(conn, key, value)` (UPSERT, stores `str(value)`), `data_version(conn) -> int`,
`open_connection(path, role='extra'|'writer'|'reader') -> conn`, `snapshot_database(src_conn, dest_path, pages=256,
sleep=0.02)` (backup API into `<dest>.tmp`, sessions emptied with `secure_delete`, `journal_mode=DELETE`, then
`util.retry_file_op(os.replace)`), `apply_migrations(conn, backup_dir) -> (old, new)`, `read_schema_version(conn)`,
`split_statements(script)`; exceptions `DbError`, `SchemaTooNew(found, supported)`, `WalUnavailable(mode)`, `ServerBusy`,
`DatabaseClosed`, `RequestError`.
`import chatd.db` works without `sqlite3` (the facade is skipped); then `sqlite_problem()` explains and `start()` raises `FatalError`.

## 2. `db_users.py` — users, sessions, registration, profile, admin, files

"Called by": **api** = `api.py` (REST), **hub** = `hub.py`, **http** = `http.py`/`files.py`, **app** = `app.py`, **cli** = `maintenance.py`.

### 2.1 Identity helpers (pure)

| function | returns | called by |
|---|---|---|
| `normalize_username(raw)` | lowercase username matching `^[a-z0-9._-]{3,32}$`, else `None` (non-ASCII -> `None`) | api, hub, cli |
| `normalize_display_name(raw)` | `util.normalize_text` result when 1..40 chars, else `None` | api, hub, cli |
| `normalize_status_text(raw)` | normalised text (may be empty) when <= 140 chars, else `None` | hub |
| `normalize_workspace_name(raw)` | normalised, 1..40 chars, else `None` | hub |

Call `normalize_*` BEFORE `auth.weak_password(password, username, display_name, ...)` and before hashing.

### 2.2 Registration (the single registration function)

`register_user(conn, spec, max_users=2000, data_dir=None, workspace_name=None, registration_open_default=False, ts=None)` **[W]**

* Called by `hub.create_user` (REST register and `admin.create_user`) and `maintenance.create_admin`. Pass
  `max_users=cfg.max_users`, `data_dir=str(cfg.data_dir)`, `workspace_name=cfg.workspace_name` (used for the title of `Everyone`
  and the `created` text only when `meta.workspace_name` is unset), `registration_open_default=cfg.registration_open`.
* `spec` (SPEC §6.1 plus two keys): `username`, `display_name` (raw strings; normalised and validated here), `pw_hash`,
  `role` (`'admin'|'member'`), `activated` (true: `last_login_at = last_seen_at = ts`), `must_change_password`, `setup_code`,
  `check_setup_code`, `actor_id`, `ip`, and **`check_registration`**, **`join_code`**.
* **Public registration** = `check_setup_code` or `check_registration` set (`POST /api/register` sets BOTH):
  while `users` is empty the setup code is verified inside the transaction with `auth.verify_setup_code(data_dir, code)`
  (`setup_code_required` / `bad_setup_code`); the first user is always `admin`, later public users are always `member`
  (a requested role is ignored). With `check_registration` and users present: `registration_open` (meta, else the default)
  and `users < max_users` else `registration_closed`, then the join code else `bad_join_code`. This closes the first-admin
  race: two racing registrations give exactly one admin, the loser gets `registration_closed`.
  **Pass `check_registration=True` on every `/api/register` call**, otherwise a registration that lost the first-admin race
  is accepted as an ordinary member.
* **Trusted callers** (`actor_id` = an enabled admin -> audit row `admin.create_user`; or no flags = the CLI) skip those checks,
  may use reserved usernames and display names, and may pass `role='admin'`.
* Always: `max_users` (`invalid_state`/`max_users`, REST `registration_closed`); `bad_request` for an invalid username, display name
  or empty `pw_hash`; identity rules of §4.3: username already used, or equal to another user's `display_key`, or reserved for a
  public caller -> `conflict`/`username_taken`; display name equal (NFKC+casefold) to another display name or to another user's
  username, or reserved unless the caller is trusted or the very first admin -> `conflict`/`name_taken`.
* Effects: user row; first user: `Everyone` (`db_chats.create_everyone_chat`, title = workspace name), `meta.join_code`;
  `db_chats.add_member_row(..., role)`; system message `created` (first) or `joined` (`actor_id=None`, `bump_activity=False`).
* Returns `{user, me, first_user, chat_id, message_id, event ('created'|'joined'), member, message_event, index}`:
  * `member` is the `{user_id, role, delivered_up_to, read_up_to}` entry for `ev.chat_members {added}` (the watermarks equal the
    chat's previous `last_message_id`); the hub builds that event with `db_chats.chat_members_event(chat_id, other_member_ids,
    added=[member])` where `other_member_ids` = `index['members']` without the new user;
  * `message_event` is the ready `ev.message` of the `created`/`joined` system message, built in the same transaction with
    `db_messages.system_message_event(conn, chat_id, message_id)` (audience: every listed member of `Everyone`, the new one
    included);
  * `index` is the fresh membership-index entry of `Everyone` from `db_chats.index_entry(conn, chat_id)` (replace
    `index[chat_id]` under the `Everyone` lock before releasing it).
  The hub then broadcasts `ev.user_update` -> `ev.chat_members {added}` -> `message_event` and, for `first_user`, calls
  `auth.clear_setup_code(data_dir)`; no query is needed between the commit and the fan-out.

### 2.3 Login, sessions, passwords

| function | returns / raises | called by |
|---|---|---|
| `get_login_record(conn, username)` **[R]** | `{id, username, display_name, pw_hash, disabled, must_change_password}` or `None` (also for an invalid username, no query) | api (login) |
| `get_password_record(conn, user_id)` **[R]** | same shape by id | api (`/api/password`) |
| `update_pw_hash(conn, user_id, old_pw_hash, new_pw_hash)` **[W]** | `bool`; compare-and-set transparent re-hash | (via `complete_login`) |
| `complete_login(conn, user_id, token_hash, ip, user_agent, session_days, old_pw_hash=None, new_pw_hash=None, ts=None)` **[W]** | `{first_login, user, me, expires_at}`; the one transaction of a good login: user re-read (`unauthorized`; disabled -> `RequestError('disabled')`), optional CAS re-hash, session row, `last_login_at`/`last_seen_at`. `first_login` (was never activated) -> broadcast `ev.user_update` | api |
| `create_session(conn, user_id, token_hash, ip, user_agent, session_days, ts=None)` **[W]** | `{token_hash, expires_at}`; `unauthorized` for a missing/disabled user; ip clipped to 64, user agent to 300 chars. Used after `hub.create_user` on register | api |
| `lookup_session(conn, token_hash, ts=None)` **[R]** | `{user_id, last_used_at, expires_at, must_change_password}` or `None` (unknown, expired, disabled user) | `auth.authenticate` |
| `touch_session(conn, token_hash, session_days, ts=None)` **[W]** | new `expires_at` or `None` when the row vanished | `auth.authenticate` |
| `list_sessions(conn, user_id, current_token_hash, ts=None)` **[R]** | `[{id, created_at, last_used_at, ip, user_agent, current}]`, newest use first, live sessions only | api |
| `revoke_session(conn, user_id, session_id)` **[W]** | the deleted `token_hash` or `None` (unknown id, another user's id, malformed id -> answer 404) | api |
| `revoke_other_sessions(conn, user_id, keep_token_hash)` **[W]** | list of deleted hashes | api |
| `revoke_user_sessions(conn, user_id)` **[W]** | list of deleted hashes (disable, admin reset; `admin.update_user` of db_chats may call it) | hub, db_chats, cli |
| `delete_session(conn, user_id, token_hash)` **[W]** | `bool` (logout) | api |
| `purge_expired_sessions(conn, ts=None)` **[W]** | rows deleted (hourly) | app |
| `session_statuses(conn, [(user_id, token_hash), ...], ts=None)` **[R]** | `{token_hash: 'ok'|'revoked'|'disabled'}` (`disabled` wins; `revoked` = row gone/expired), chunked | hub (`revalidate_all`, `external_change`) |
| `change_password(conn, user_id, old_pw_hash, new_pw_hash, keep_token_hash)` **[W]** | revoked token hashes; `unauthorized` if disabled; `conflict` if the stored hash is no longer `old_pw_hash`; clears `must_change_password`, deletes every OTHER session | api |
| `set_user_password(conn, user_id, pw_hash, must_change, revoke_sessions=True)` **[W]** | revoked hashes; `not_found` | cli, admin_reset_password |
| `set_last_seen(conn, user_ids, ts)` **[W]** | `None`; chunked `UPDATE users SET last_seen_at` | app (60 s heartbeat, shutdown) |

### 2.4 Users, profile, workspace, setup state

| function | returns | called by |
|---|---|---|
| `get_user(conn, user_id)`, `get_me(conn, user_id)` **[R]** | public user / `me` or `None` (disabled users are returned) | api, hub |
| `list_users(conn)` **[R]** | every user (public shape), by id | hub (`ev.ready.users`, `external_change` diff) |
| `profile_update(conn, user_id, fields, ts=None)` **[W]** | `{me, changed, changed_fields}`; `fields` keys `display_name`, `status_text`, `read_receipts` (bool), `show_last_seen` (bool); no key -> `bad_request`; taken/reserved name -> `conflict`/`name_taken`; disabled -> `unauthorized`. No-op rule: unchanged -> `changed=False` (the hub emits nothing) | hub |
| `user_count(conn)`, `needs_setup(conn)` **[R]** | `int`, `bool` (`/api/info.needs_setup`) | api, auth |
| `get_instance_id(conn)` **[R]** | `meta.instance_id` | hub |
| `workspace_settings(conn, default_name, default_registration_open, include_join_code=False)` **[R]** | `{name, registration_open[, join_code]}`; `meta` wins over the config defaults | api, hub |
| `require_active_user(conn, user_id)`, `require_admin(conn, user_id)` | `{id, username, role}`; `unauthorized` / `forbidden` | any db function, db_chats |
| `audit(conn, actor_id, action, target_id=None, ip=None, ts=None)` **[W]** | appends an `audit_log` row. Vocabulary (§3): `admin.create_user`, `admin.update_user`, `admin.reset_password`, `admin.settings`, `cli.create_admin`, `cli.reset_password`, `cli.restore` | db_chats (`admin.update_user`), cli |

### 2.5 Admin (`actor_id` = the caller; all re-check `require_admin`)

| function | returns / raises |
|---|---|
| `admin_users(conn, actor_id)` **[R]** | public users + `{created_at, last_login_at, message_count, must_change_password}` (`message_count` via the `messages_sender` index) |
| `admin_reset_password(conn, actor_id, user_id, pw_hash, ip=None, ts=None)` **[W]** | `{user_id, username, revoked}` (the hub then `hub.revoke(user_id, reason='revoked')`); sets `must_change_password=1`, deletes the target's sessions, audit `admin.reset_password`; `not_found` |
| `admin_settings(conn, actor_id, default_name, default_registration_open, ip=None, workspace_name=None, registration_open=None, rotate_join_code=None, ts=None)` **[W or R]** | `{workspace:{name, registration_open, join_code}, changed}`. No field given = a pure read (no write, no audit; valid on a reader). Equal values = no-op. One `admin.settings` audit row per effective change; `bad_request` for an invalid name or non-bool. Hub emits `ev.workspace` only when `changed`. Never renames `Everyone` |
| `admin_stats(conn, actor_id)` **[R]** | `{users, chats, messages, attachments, last_backup_at}`; the hub adds online, `storage_bytes` (`attachment_storage_bytes`, cached hourly), db_bytes, disk_free, uptime, python, version, urls |
| `admin_audit(conn, actor_id, limit=100)` **[R]** | `[{id, ts, actor_id, action, target_id, ip}]` newest first, limit clamped 1..100 |

`admin.update_user` is owned by db_chats; it should use `require_admin`, `audit(..., 'admin.update_user', target)` and
`revoke_user_sessions` from here.

### 2.6 Attachments, quotas, access rule, orphan sweep

| function | returns / raises | called by |
|---|---|---|
| `upload_usage(conn, user_id, ts=None, window=86400.0)` **[R]** | `{unattached_bytes, recent_bytes}` (the two quota queries of §5.1) | http/files (before reading the body) |
| `insert_attachment(conn, uploader_id, attachment, max_unattached_bytes=None, max_recent_bytes=None, ts=None)` **[W]** | the public attachment `{id, name, mime, size, kind, url, width, height, duration}`; re-checks the uploader (`unauthorized`) and both quotas (`quota_exceeded`; `used + size > limit`); `bad_request` for a non-32-hex id, an empty `name`/`mime`/`path`, a `kind` outside `image|audio|video|file`, a negative or non-int `size` or a `path` that is absolute, contains `..` or a drive colon. `attachment` keys: `id`, `name`, `mime`, `kind`, `size`, `path` (relative to `uploads/`), optional `width`, `height` (ints 1..16384), `duration` (finite 0..86400, never kept for `kind='file'`): an implausible hint is stored as `NULL` and returned as `null` (SPEC §5.6: hints are dropped silently) | http/files |
| `attachment_access(conn, user_id, attachment_id)` **[R]** | the row `{id, uploader_id, name, mime, kind, size, path, width, height, duration, created_at}` or `None` (-> 404). Rule §4.2: uploader, or visible (§3.1: listed member, `id > max(history_from_id, cleared_before_id)`, not hidden) non-deleted message referencing it. Malformed ids and disabled or unknown users return `None` (a disabled account reaches nothing, §7.1.1(e)) | http/files |
| `get_attachment(conn, attachment_id)` | the row or `None` (no access check) | hub/db_chats |
| `attachment_public(row)` | public attachment dict | |
| `attachment_storage_bytes(conn)` **[R]** | `SUM(size)` (hourly cache) | app |
| `drop_attachment_if_unreferenced(conn, attachment_id)` **[W]** | the stored `path` when the row was deleted (no message references it), else `None`; the caller then `util.retry_file_op(os.remove, ...)`. For delete-for-everyone and the sweep | db_messages, sweep |
| `sweep_orphans(conn, uploads_dir, ts=None, unattached_after=7200.0, part_after=3600.0, file_after=86400.0, batch=500)` | the 15-minute sweep of §2.4 on an OWN connection (`database.connect_extra()`, run it in an executor): deletes unattached attachments older than 2 h (row, then file), `uploads/.tmp/*.part` older than 1 h, unreferenced rows whose file is missing (a referenced row with a missing file is kept and logged once), files `uploads/<aa>/<32hex>` without a row older than 24 h. Returns `{unattached, parts, missing_rows, orphan_files, missing_referenced}` | app |

SQL constants for `tests/test_query_plans.py` (run `EXPLAIN QUERY PLAN <sql>` with the parameters; none shows a `SCAN`):
`UPLOAD_UNATTACHED_BYTES_SQL` `(user_id,)`, `UPLOAD_RECENT_BYTES_SQL` `(user_id, since_ts)`,
`ATTACHMENT_ACCESS_SQL` `(attachment_id, user_id, user_id, user_id)` (the `/files` check; plain `?` placeholders),
`ORPHAN_UNATTACHED_SQL` `(cutoff_ts, limit)`, `ATTACHMENT_BATCH_SQL` `(after_id, limit)`. They use `INDEXED BY` so the plan
does not depend on `ANALYZE` statistics.

## 3. `auth.py`

Importable without `sqlite3`. Process state lives in module attributes set by `configure`; always read them as `auth.hasher`,
`auth.login_throttle`, `auth.registration_limiter`, `auth.guess_limiter` (they are replaced by `configure`).

| call | semantics |
|---|---|
| `configure(scrypt_n=65536, session_days=30, min_password_len=8, test_limits=None)` | **`app.py` must call it once at startup** with `cfg.scrypt_n`, `cfg.session_days`, `cfg.min_password_len`, `cfg.test_limits` (before serving). Creates the hasher (`ThreadPoolExecutor(2)`, at most 8 queued) and fresh limiters (`test_limits` keys `login_a/b/c` = `[count, window_s]`, `reg_per_ip_hour`, `reg_global_hour`) |
| `async hash_password(password) -> str` | scrypt `scrypt$n$8$1$<salt_b64>$<hash_b64>` (or `pbkdf2$600000$...` without scrypt) on the executor. Raises `db.ServerBusy` (retry_after 1) when 10 jobs are outstanding: REST `429 server_busy` + `Retry-After: 1`; WS `admin.create_user`/`admin.reset_password` `server_busy` |
| `async verify_password(password, stored_or_None) -> (ok, needs_rehash)` | parameters read from the stored string; `None` (unknown user) verifies a dummy hash of the same cost and answers `(False, False)`; a password longer than 128 characters (after NFKC) answers `(False, False)` without hashing. On `ok and needs_rehash`: `new = await hash_password(pw)` then `db.complete_login(..., old_pw_hash=stored, new_pw_hash=new)` |
| `hash_password_sync(password, scrypt_n=65536)`, `verify_password_sync(password, stored, scrypt_n=65536)`, `PasswordHasher(scrypt_n, workers=2, queue=8)` | blocking variants / the executor object. Passwords are NFKC-normalised before hashing (`normalize_password`) |
| `weak_password(password, username='', display_name='', min_len=8) -> Optional[str]` | `None` when fine, else a human message (answer `400 weak_password`, WS `bad_request` `reason:'weak_password'`). Rules: NFKC; `min_len <= len <= 128`; not equal (casefold) to username/display name; not in `COMMON_PASSWORDS` (>= 200 entries). `check_password_policy(password, username='', display_name='')` uses the configured `min_password_len`. `same_as_old` is the caller's check: `same_password(old, new) -> bool` (NFKC compare, constant time) |
| `new_session_token() -> (token, token_hash)`, `token_hash(token)`, `session_public_id(token_hash)` | `secrets.token_urlsafe(32)`, sha256 hex, first 16 hex chars |
| `async authenticate(db, token, ip, user_agent, session_days=None) -> Optional[dict]` | SPEC §6.2: `{user_id, token_hash, ip, user_agent, must_change_password, reissue_cookie}`; `None` for malformed/unknown/expired token or disabled user. Reader lookup; a writer touch only when `last_used_at` is >= 10 min old (`expires_at = now + session_days`); `reissue_cookie` when that moved the expiry by more than one day |
| `parse_cookie(header)`, `cookie_header(token, secure, max_age)`, `clear_cookie_header(secure)` | cookie `fc_session`; `parse_cookie` returns `None` for absent/duplicated/malformed values |
| `LoginThrottle(clock=time.monotonic, test_limits=None, cap=10000)` (`auth.login_throttle`) | `check(ip, username, counters='abc') -> float` whole seconds to wait, rounded up (`0.0` = go; answer `429 rate_limited` with that `retry_after` and `Retry-After`); `record_failure(ip, username, counters='abc')`; `record_success(ip, username)` (clears counter (a) only). Counters: (a) `(ip, username)` 5 / 5 min, (b) `ip` 20 / 10 min, (c) username 30 / 15 min; `n` = failures in the window minus the limit; with `n >= 1` a pause of `min(5*2^(n-1), 900)` s is required after the last failure (`check` returns what is left; the largest of the three wins). Known and unknown usernames, and every invalid username (key `?`), are counted identically. Call `check` BEFORE verifying, `record_failure` after a failed login. Failed `POST /api/password`: `check(ip, username, 'a')` / `record_failure(ip, username, 'a')` |
| `GuessLimiter()` (`auth.guess_limiter`) | wrong setup/join code: `check(ip)` (whole seconds, `0.0` = go) / `record_failure(ip)`; 5 failures / 10 min / IP |
| `RegistrationLimiter(clock, test_limits)` (`auth.registration_limiter`) | `check(ip)` (whole seconds) before registering, `record(ip)` after a successful registration; 5 / h / IP, 60 / h overall |
| `async ensure_setup_code(db, data_dir) -> Optional[str]` | while `users` is empty: create or reuse `<data>/setup_code.txt` (0600) and return the 8-char code; otherwise delete a stale file and return `None`. Call after `migrate()` |
| `verify_setup_code(data_dir, code) -> bool`, `clear_setup_code(data_dir)` | constant-time compare; idempotent delete (call it after the first admin committed and from `external_change`) |
| `async check_join_code(db, code) -> bool` | cheap pre-check against `meta.join_code` |

All throttle windows and delays go through `util.scaled()` (defaults only; `test_limits` windows are used as given).

## 4. `util.py`

`now()`, `scaled(seconds)`, `set_test_scale(x)`; `new_id()` (32 hex), `short_code()` (8 chars, setup/join codes);
`json_dumps(obj, ensure_ascii=True)`, `json_loads_strict(text, max_depth=8)` (the §7.1 rules: no NaN/Infinity, ints <= 18 digits, no
duplicate keys, depth <= 8; always `ValueError`); `normalize_text(s, allow_newline=False)`, `display_key(s)`, `fold(v)`,
`USERNAME_RE` (`^[a-z0-9._-]{3,32}\Z`), `RESERVED_USERNAMES`, `login_key(username)` (throttle key: lowercased valid username or `?`),
`log_username(v)` (the username or `<invalid>`), `safe_log_value(v, limit=100)`;
`setup_logging(level, data_dir, attach_file) -> None` (`stop_logging()` undoes it; stderr via QueueHandler/QueueListener, file
`<data>/logs/desktalk.log` 5 MB x 5 only when `attach_file`; `SafeFormatter` escapes `\r\n` in messages; `SafeRotatingFileHandler`);
`lan_addresses() -> (primary|None, [others])`;
`retry_file_op(fn, *args) -> bool` (5 retries, `50 ms * 2^n`; deletes (`os.remove/unlink/rmdir`): a missing file is success and a final
failure goes to the pending-delete list and returns `False`, never raising; other operations (`os.replace`, `os.rename`, ...) re-raise the
last `OSError` after the final attempt), `pending_delete_load(data_dir)`, `pending_delete_sweep() -> int` (remaining);
`instance_lock(data_dir, info=None) -> InstanceLock` (`.update(dict)`, `.release()`, `.info`; raises `AlreadyRunning`, a `FatalError` with
`code=73`, `.info/.pid/.port`) and `read_lock_info(data_dir)`; the lock byte is at offset 4096, the JSON (< 512 bytes, space padded) at
offset 0 is read unbuffered; `FatalError(message, code=78)`; path helpers `db_path`, `control_dir`, `uploads_dir`, `backups_dir`;
`write_private_file(path, text)`.

## 5. `maintenance.py`

`sqlite3` and every `db*` module are imported inside the functions, so the module imports (and `schema_info`'s caller `doctor` runs)
without `sqlite3`. Failures raise `MaintenanceError` (a `util.FatalError`, exit code 1 unless stated) with a console message.
CLI writers use `BEGIN IMMEDIATE` and then `touch_reload`.

| call | semantics |
|---|---|
| `create_admin(data_dir, username, password, display_name=None, workspace_name=None, min_password_len=8, max_users=2000, scrypt_n=None) -> User` | creates `chat.db` (schema) if missing; new user via `db.register_user` (`activated=False`, no setup code; first user creates `Everyone`), or promotes an existing user: `role='admin'`, enabled, new password, `must_change_password=0`, sessions deleted, role mirrored in `Everyone`. Audit `cli.create_admin`. Default hash cost = `auth.hasher.scrypt_n` |
| `reset_password(data_dir, username, password, must_change=False, min_password_len=8, scrypt_n=None)` | new hash, all sessions deleted, `must_change_password = must_change`, audit `cli.reset_password` |
| `backup(db_path, out_dir, with_uploads=False, prefix='chat') -> str` | sqlite backup API on an own connection (`pages=256, sleep=0.02`) into `<out>/<prefix>-YYYYmmdd-HHMMSS.db` (a `-N` suffix on a same-second clash), `.tmp` then `retry_file_op(os.replace)`, sessions emptied; `with_uploads`: folder `<prefix>-YYYYmmdd-HHMMSS/` with `chat.db` and `uploads/` (hard links, `.tmp` skipped; uploads must stay write-once). Refuses `out_dir` below the app's `web/` |
| `prune_backups(out_dir, prefix='auto', keep=7) -> List[str]` | deletes all but the newest `keep` `<prefix>-YYYYmmdd-HHMMSS[-n].db`; `chat-*`, `pre-migrate-*`, `pre-restore-*` are never touched |
| `restore(path, data_dir)` | takes `util.instance_lock` (running server -> `MaintenanceError` code 73 naming pid/port), copies the snapshot to `chat.db.restore`, checks `integrity_check` and the schema version, then moves `chat.db`, `-wal`, `-shm` (and `uploads/` for a folder snapshot) to `<data>/backups/pre-restore-<ts>/`, installs the snapshot, migrates an older schema and appends audit `cli.restore`. Nothing moves when a check fails |
| `schema_info(data_dir) -> dict` | read-only (`mode=ro`), never raises `sqlite3` errors: `{path, exists, code_schema_version, schema_version, journal_mode, integrity ('ok'|text), meta:{workspace_name?, registration_open?}, error}` |
| `touch_reload(data_dir)` | create/bump `<data>/control/reload` |

## 6. Recipes

* **Startup (`Server.run`)**: `util.instance_lock` -> `util.setup_logging` -> `util.pending_delete_load` -> `auth.configure(...)`,
  `util.set_test_scale(cfg.test_scale)` -> `db.start()`; `db.migrate()` -> `await auth.ensure_setup_code(db, cfg.data_dir)`.
* **Control poll**: `await db.run_raw(db.data_version)` every 2 s (writer connection); on change `db.list_users` /
  `db.session_statuses`; when `needs_setup` is false `auth.clear_setup_code`.
* **Register**: `normalize_*` -> `auth.registration_limiter.check(ip)` / `auth.guess_limiter.check(ip)` -> `auth.check_password_policy`
  -> `auth.hash_password` -> `hub.create_user(spec)` (-> `db.register_user` with `check_setup_code=True, check_registration=True`)
  -> `auth.registration_limiter.record(ip)` -> `auth.new_session_token()` + `db.create_session` -> `Set-Cookie`.
  A `RequestError` of `bad_setup_code`/`bad_join_code` -> `guess_limiter.record_failure(ip)`.
* **Login**: `throttle.check` -> `run_read(db.get_login_record)` -> `auth.verify_password(pw, record and record['pw_hash'])`
  -> wrong: `throttle.record_failure`, 401 `bad_credentials` -> right: `disabled` -> 403 -> `run(db.complete_login, ...)`
  -> `throttle.record_success` -> broadcast `ev.user_update` when `first_login`.
* **Change password** (`POST /api/password`): `auth.login_throttle.check(ip, username, 'a')` -> `run_read(db.get_password_record, uid)` ->
  `auth.verify_password(old, record['pw_hash'])`; wrong: `record_failure(ip, username, 'a')` and `403 forbidden` `reason:'bad_old_password'`
  -> `auth.same_password(old, new)`: `400 weak_password` `reason:'same_as_old'` -> `auth.check_password_policy(new, username,
  display_name)` -> `await auth.hash_password(new)` -> `run(db.change_password, uid, record['pw_hash'], new_hash, session['token_hash'])`
  (`conflict` when the password changed meanwhile) -> `hub.revoke(uid, reason='password_changed', except_token_hash=...)` -> `204`.
* **Uploads**: `run_read(db.upload_usage, uid)` (before reading the body) -> stream -> `run(db.insert_attachment, uid, row,
  max_unattached_bytes, max_recent_bytes)`; downloads: `run_read(db.attachment_access, uid, id)`.
* **Background**: hourly `db.purge_expired_sessions`, `db.attachment_storage_bytes`, `util.pending_delete_sweep()`; every 15 min
  `db.sweep_orphans(conn_from_connect_extra, uploads_dir)` in an executor; backup ticker `maintenance.backup(db_path, backup_dir,
  prefix='auto')` -> `db.set_meta('last_backup_at', ts)` -> `maintenance.prune_backups(backup_dir, 'auto', 7)`.
