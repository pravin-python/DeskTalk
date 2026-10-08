"""REST handlers of SPEC 4.2 / 4.3: ``/api/info /register /login /logout /me /password /sessions /sessions/revoke``.

``register_routes(router, hub, db, cfg)`` registers exactly these routes.  Everything cross-cutting is the router's
job (Host / Origin / ``X-Requested-With`` checks, the cookie session check with ``auth='cookie'``, the forced
password change, ``Set-Cookie`` re-issue, security headers); the password hashing, the login / registration / guess
throttles and the setup-code functions are ``auth.py``'s; accounts are created by ``hub.create_user`` so that the
fan-out of SPEC 3.2(4) happens exactly once, and sessions are ended through ``hub.revoke`` so that the matching
sockets close at once (SPEC 11(3)).  Error bodies follow SPEC 4.2: ``{"error": {"code", "msg", "reason"?,
"retry_after"?}}``.

Python 3.8 compatible, standard library only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import auth, db_users, util
from . import db as dbmod
from .config import Config
from .http import HttpError, Request, Response, Router, error_response, json_response

log = logging.getLogger("chatd.api")

__all__ = ["register_routes"]

#: REST error code -> HTTP status (SPEC 4.2); ``error_response`` adds ``Retry-After`` when ``retry_after`` is set.
STATUS = {
    "bad_request": 400,
    "weak_password": 400,
    "bad_credentials": 401,
    "unauthorized": 401,
    "forbidden": 403,
    "disabled": 403,
    "registration_closed": 403,
    "setup_code_required": 403,
    "bad_setup_code": 403,
    "bad_join_code": 403,
    "password_change_required": 403,
    "not_found": 404,
    "username_taken": 409,
    "name_taken": 409,
    "conflict": 409,
    "too_large": 413,
    "rate_limited": 429,
    "server_busy": 429,
}
_NAME_MAX = 400
_PASSWORD_MAX = 4096
_CODE_MAX = 256
_LOGIN_PASSWORD_MAX = 512


def _refuse(code: str, msg: str, status: Optional[int] = None, **kwargs: Any) -> HttpError:
    return HttpError(error_response(status or STATUS[code], code, msg, **kwargs))


def _request_failed(exc: dbmod.RequestError) -> HttpError:
    """A db failure as the REST error of SPEC 4.2 (``username_taken``, ``registration_closed``... via ``rest_code``)."""
    code = exc.rest_code
    if code == "invalid_state":
        code = "conflict"
    status = STATUS.get(code)
    if status is None:
        log.error("no REST status for the db error %s", util.safe_log_value(code))
        return HttpError(error_response(500, "server_error", "internal error"))
    reason = exc.reason if exc.reason not in ("username_taken", "name_taken", "max_users") else None
    return HttpError(error_response(status, code, exc.msg, retry_after=exc.retry_after, reason=reason))


def _busy(exc: dbmod.ServerBusy) -> HttpError:
    return _refuse("server_busy", "the server is busy, retry shortly", retry_after=exc.retry_after)


def _throttled(wait: float) -> HttpError:
    return _refuse("rate_limited", "too many attempts, wait a moment", retry_after=max(wait, 1.0))


async def _body(req: Request) -> Dict[str, Any]:
    """The JSON object of a request; a lone surrogate in it is ``400 bad_request`` ``invalid_text`` (SPEC 7.1)."""
    body = await req.read_json()
    if util.has_lone_surrogate(body):
        raise _refuse("bad_request", "text is not valid", reason="invalid_text")
    return body


def _text(body: Dict[str, Any], name: str, limit: int = _NAME_MAX, required: bool = True) -> Optional[str]:
    value = body.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise _refuse("bad_request", "%s must be a string of at most %d characters" % (name, limit))
    return value


def register_routes(router: Router, hub: Any, db: Any, cfg: Config) -> None:
    """Register the REST routes of ``api.py`` (SPEC 6.2: these routes and nothing else)."""

    def secure_cookie(token: str) -> str:
        return auth.cookie_header(token, cfg.tls, cfg.session_days * 86400)

    def me_of(me: Dict[str, Any]) -> Dict[str, Any]:
        me["online"] = True  # whoever calls the REST API is online by definition (``ev.ready.me`` says the same)
        return me

    # ---- GET /api/info -------------------------------------------------------------------------------------------

    def read_info(conn: Any) -> Dict[str, Any]:
        settings = db_users.workspace_settings(conn, cfg.workspace_name, cfg.registration_open)
        return {
            "name": settings["name"],
            "registration_open": settings["registration_open"],
            "needs_setup": db_users.needs_setup(conn),
            "tls": cfg.tls,
        }

    async def info(req: Request) -> Response:
        return json_response(200, await db.run_read(read_info))

    # ---- POST /api/register --------------------------------------------------------------------------------------

    async def check_codes(ip: str, setup_code: Optional[str], join_code: Optional[str]) -> None:
        """Refuse a wrong setup / join code before any password is hashed (the db function checks again in its
        transaction); wrong guesses count against the per-IP guess limiter (SPEC 4.1)."""
        if await db.run_read(db_users.needs_setup):
            if not setup_code:
                raise _refuse("setup_code_required", "the setup code is required")
            if not auth.verify_setup_code(cfg.data_dir, setup_code):
                auth.guess_limiter.record_failure(ip)
                raise _refuse("bad_setup_code", "the setup code is wrong")
            return
        settings = await db.run_read(db_users.workspace_settings, cfg.workspace_name, cfg.registration_open)
        if not settings["registration_open"]:
            raise _refuse("registration_closed", "registration is closed")
        if not await auth.check_join_code(db, join_code):
            auth.guess_limiter.record_failure(ip)
            raise _refuse("bad_join_code", "the join code is wrong")

    async def register(req: Request) -> Response:
        body = await _body(req)
        ip = req.remote_addr
        raw_username = _text(body, "username")
        raw_display = _text(body, "display_name")
        password = _text(body, "password", _PASSWORD_MAX)
        setup_code = _text(body, "setup_code", _CODE_MAX, False)
        join_code = _text(body, "join_code", _CODE_MAX, False)
        wait = max(auth.registration_limiter.check(ip), auth.guess_limiter.check(ip))
        if wait > 0:
            raise _throttled(wait)
        await check_codes(ip, setup_code, join_code)
        username = db_users.normalize_username(raw_username)
        display_name = db_users.normalize_display_name(raw_display)
        if username is None:
            raise _refuse("bad_request", "username must be 3-32 characters of a-z, 0-9, '.', '_' or '-'")
        if display_name is None:
            raise _refuse("bad_request", "display name must be 1-40 characters")
        problem = auth.check_password_policy(password, username, display_name)
        if problem is not None:
            raise _refuse("weak_password", problem)
        try:
            pw_hash = await auth.hash_password(password)
            user = await hub.create_user(
                {
                    "username": username,
                    "display_name": display_name,
                    "pw_hash": pw_hash,
                    "role": "member",
                    "activated": True,
                    "must_change_password": False,
                    "setup_code": setup_code,
                    "check_setup_code": True,
                    "check_registration": True,
                    "join_code": join_code,
                    "actor_id": None,
                    "ip": ip,
                }
            )
        except dbmod.ServerBusy as exc:
            raise _busy(exc)
        except dbmod.RequestError as exc:
            if exc.code in ("bad_setup_code", "bad_join_code"):
                auth.guess_limiter.record_failure(ip)
            raise _request_failed(exc)
        auth.registration_limiter.record(ip)
        token, token_hash = auth.new_session_token()
        await db.run(
            db_users.create_session, user["id"], token_hash, ip, req.headers.get("user-agent", ""), cfg.session_days
        )
        log.info("registered user=%d ip=%s", user["id"], util.safe_log_value(ip))
        me = me_of(dict(user, show_last_seen=True, must_change_password=False))
        resp = json_response(201, {"me": me})
        resp.add_header("Set-Cookie", secure_cookie(token))
        return resp

    # ---- POST /api/login -----------------------------------------------------------------------------------------

    async def login(req: Request) -> Response:
        body = await _body(req)
        ip = req.remote_addr
        username = _text(body, "username")
        password = _text(body, "password", _PASSWORD_MAX)
        wait = auth.login_throttle.check(ip, username)
        if wait > 0:
            raise _throttled(wait)
        record = await db.run_read(db_users.get_login_record, username)
        try:
            if len(password) > _LOGIN_PASSWORD_MAX:  # no real password is this long: skip the expensive hash
                verified, needs_rehash = False, False
            else:
                verified, needs_rehash = await auth.verify_password(password, record["pw_hash"] if record else None)
        except dbmod.ServerBusy as exc:
            raise _busy(exc)
        if not verified or record is None:
            auth.login_throttle.record_failure(ip, username)
            log.info("login failed user=%s ip=%s", util.log_username(username), util.safe_log_value(ip))
            raise _refuse("bad_credentials", "wrong username or password")
        if record["disabled"]:
            raise _refuse("disabled", "this account has been disabled")
        new_hash: Optional[str] = None
        if needs_rehash:
            try:
                new_hash = await auth.hash_password(password)
            except dbmod.ServerBusy:
                log.info("password re-hash postponed: the hash queue is full")
        token, token_hash = auth.new_session_token()
        try:
            done = await db.run(
                db_users.complete_login, record["id"], token_hash, ip, req.headers.get("user-agent", ""),
                cfg.session_days, record["pw_hash"] if new_hash is not None else None, new_hash,
            )
        except dbmod.RequestError as exc:
            raise _request_failed(exc)
        auth.login_throttle.record_success(ip, username)
        if done["first_login"]:
            hub.broadcast_user(done["user"])  # ev.user_update {activated:true} (SPEC 4.2, 8.1)
        log.info("login user=%d ip=%s", record["id"], util.safe_log_value(ip))
        resp = json_response(200, {"me": me_of(done["me"])})
        resp.add_header("Set-Cookie", secure_cookie(token))
        return resp

    # ---- POST /api/logout, GET /api/me ---------------------------------------------------------------------------

    def end_session_response(req: Request) -> Response:
        """``204`` that clears the cookie; the router must not re-issue it on the same response."""
        assert req.session is not None
        req.session["reissue_cookie"] = False
        resp = Response(204)
        resp.add_header("Set-Cookie", auth.clear_cookie_header(cfg.tls))
        return resp

    async def logout(req: Request) -> Response:
        session = req.session
        assert session is not None
        await db.run(db_users.delete_session, session["user_id"], session["token_hash"])
        await hub.revoke(session["user_id"], reason="logout", token_hash=session["token_hash"])
        return end_session_response(req)

    async def me(req: Request) -> Response:
        session = req.session
        assert session is not None
        found = await db.run_read(db_users.get_me, session["user_id"])
        if found is None or found["disabled"]:
            raise _refuse("unauthorized", "sign in required")
        return json_response(200, {"me": me_of(found)})

    # ---- POST /api/password --------------------------------------------------------------------------------------

    async def password(req: Request) -> Response:
        session = req.session
        assert session is not None
        body = await _body(req)
        old_password = _text(body, "old_password", _PASSWORD_MAX)
        new_password = _text(body, "new_password", _PASSWORD_MAX)
        ip, user_id = req.remote_addr, session["user_id"]
        record = await db.run_read(db_users.get_password_record, user_id)
        if record is None or record["disabled"]:
            raise _refuse("unauthorized", "sign in required")
        wait = auth.login_throttle.check(ip, record["username"], "a")
        if wait > 0:
            raise _throttled(wait)
        try:
            verified, _ = await auth.verify_password(old_password, record["pw_hash"])
        except dbmod.ServerBusy as exc:
            raise _busy(exc)
        if not verified:
            auth.login_throttle.record_failure(ip, record["username"], "a")
            raise _refuse("forbidden", "the old password is wrong", reason="bad_old_password")
        if auth.normalize_password(new_password) == auth.normalize_password(old_password):
            raise _refuse("weak_password", "choose a different password", reason="same_as_old")
        problem = auth.check_password_policy(new_password, record["username"], record["display_name"])
        if problem is not None:
            raise _refuse("weak_password", problem)
        try:
            new_hash = await auth.hash_password(new_password)
            await db.run(db_users.change_password, user_id, record["pw_hash"], new_hash, session["token_hash"])
        except dbmod.ServerBusy as exc:
            raise _busy(exc)
        except dbmod.RequestError as exc:
            raise _request_failed(exc)
        await hub.revoke(user_id, reason="password_changed", except_token_hash=session["token_hash"])
        return Response(204)

    # ---- GET /api/sessions, POST /api/sessions/revoke ------------------------------------------------------------

    async def sessions(req: Request) -> Response:
        session = req.session
        assert session is not None
        listing = await db.run_read(db_users.list_sessions, session["user_id"], session["token_hash"])
        return json_response(200, {"sessions": listing})

    async def revoke(req: Request) -> Response:
        session = req.session
        assert session is not None
        body = await _body(req)
        user_id, current = session["user_id"], session["token_hash"]
        wanted_id, others = body.get("id"), body.get("all_others")
        if (wanted_id is None) == (others is None) or (others is not None and others is not True):
            raise _refuse("bad_request", "send either an id or all_others: true")
        if others:
            await db.run(db_users.revoke_other_sessions, user_id, current)
            await hub.revoke(user_id, reason="revoked", except_token_hash=current)
            return Response(204)
        removed = await db.run(db_users.revoke_session, user_id, wanted_id)
        if removed is None:
            raise _refuse("not_found", "no such session")
        if removed == current:
            await hub.revoke(user_id, reason="logout", token_hash=current)
            return end_session_response(req)
        await hub.revoke(user_id, reason="revoked", token_hash=removed)
        return Response(204)

    router.add("GET", "/api/info", info)
    router.add("POST", "/api/register", register)
    router.add("POST", "/api/login", login)
    router.add("POST", "/api/logout", logout, auth="cookie", pw_exempt=True)
    router.add("GET", "/api/me", me, auth="cookie", pw_exempt=True)
    router.add("POST", "/api/password", password, auth="cookie", pw_exempt=True)
    router.add("GET", "/api/sessions", sessions, auth="cookie")
    router.add("POST", "/api/sessions/revoke", revoke, auth="cookie")
