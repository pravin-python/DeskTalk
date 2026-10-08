"""Passwords, session tokens and cookies, login/registration throttles, setup and join codes (SPEC §4.1, §4.3, §6.2).

Importable without ``sqlite3``. Process-wide state (the hasher, the three limiters) is created by :func:`configure`
and read through the module attributes ``hasher``, ``login_throttle``, ``registration_limiter`` and
``guess_limiter``: always call ``auth.login_throttle.check(...)``, never bind the object at import time.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import math
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, Optional, Tuple

from . import db as dbmod
from . import db_users, util

log = logging.getLogger("chatd.auth")

# --------------------------------------------------------------------------------------------------------------------
# Password hashing (SPEC §4.1)
# --------------------------------------------------------------------------------------------------------------------

SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024
PBKDF2_ITERATIONS = 600_000
PASSWORD_MAX_LEN = 128
HASH_WORKERS = 2
HASH_QUEUE = 8


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> Optional[bytes]:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


def normalize_password(password: str) -> str:
    """NFKC form of a password: policy checks and hashing both use it (so every Unicode spelling verifies)."""
    return unicodedata.normalize("NFKC", password)


def _password_bytes(password: str) -> bytes:
    return normalize_password(password).encode("utf-8", "surrogatepass")


def _scrypt_maxmem(n: int, r: int, p: int) -> int:
    return max(SCRYPT_MAXMEM, 128 * r * (n + p + 2) + 1024 * 1024)


def have_scrypt() -> bool:
    """``True`` when ``hashlib.scrypt`` exists (OpenSSL 1.1+); otherwise PBKDF2 is the hash."""
    return hasattr(hashlib, "scrypt")


def hash_password_sync(password: str, scrypt_n: int = SCRYPT_N) -> str:
    """Hash ``password`` (blocking, ~150-260 ms): ``scrypt$n$r$p$salt$hash`` or ``pbkdf2$iterations$salt$hash``.

    Never call it on the event loop; use :meth:`PasswordHasher.hash`.
    """
    salt = os.urandom(16)
    secret = _password_bytes(password)
    if have_scrypt():
        digest = hashlib.scrypt(
            secret,
            salt=salt,
            n=scrypt_n,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=_scrypt_maxmem(scrypt_n, SCRYPT_R, SCRYPT_P),
        )
        return "scrypt$%d$%d$%d$%s$%s" % (scrypt_n, SCRYPT_R, SCRYPT_P, _b64(salt), _b64(digest))
    digest = hashlib.pbkdf2_hmac("sha256", secret, salt, PBKDF2_ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ITERATIONS, _b64(salt), _b64(digest))


def _derive(stored: str, secret: bytes) -> Optional[Tuple[bytes, bytes, bool]]:
    """Recompute the digest for ``stored``'s own parameters: ``(candidate, expected, uses_current_policy)``."""
    parts = stored.split("$")
    try:
        if parts[0] == "scrypt" and len(parts) == 6 and have_scrypt():
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            salt, expected = _unb64(parts[4]), _unb64(parts[5])
            if salt is None or expected is None or not 16 <= len(expected) <= 64:
                return None
            if not (2 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
                return None
            candidate = hashlib.scrypt(
                secret, salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_scrypt_maxmem(n, r, p)
            )
            return candidate, expected, (r, p, len(expected)) == (SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
        if parts[0] == "pbkdf2" and len(parts) == 4:
            iterations = int(parts[1])
            salt, expected = _unb64(parts[2]), _unb64(parts[3])
            if salt is None or expected is None or not 16 <= len(expected) <= 64 or not 1 <= iterations <= 10_000_000:
                return None
            candidate = hashlib.pbkdf2_hmac("sha256", secret, salt, iterations, len(expected))
            return candidate, expected, iterations == PBKDF2_ITERATIONS and not have_scrypt()
    except ValueError:
        return None
    return None


def _stored_cost(stored: str) -> Optional[int]:
    parts = stored.split("$")
    try:
        return int(parts[1]) if parts[0] in ("scrypt", "pbkdf2") and len(parts) > 1 else None
    except ValueError:
        return None


def verify_password_sync(password: str, stored: str, scrypt_n: int = SCRYPT_N) -> Tuple[bool, bool]:
    """Verify ``password`` against a stored hash (blocking). Returns ``(ok, needs_rehash)``.

    The parameters come from the stored string; ``needs_rehash`` is true when they differ from the current policy
    (``scrypt`` with ``scrypt_n``, or PBKDF2 where scrypt is unavailable) so the caller can transparently re-hash.
    A malformed stored string verifies as ``(False, False)``.
    """
    if len(normalize_password(password)) > PASSWORD_MAX_LEN:  # the policy never lets such a password exist
        return False, False
    derived = _derive(stored, _password_bytes(password))
    if derived is None:
        return False, False
    candidate, expected, params_current = derived
    if not hmac.compare_digest(candidate, expected):
        return False, False
    cost = _stored_cost(stored)
    current = params_current and (not have_scrypt() or cost == scrypt_n)
    return True, not current


class PasswordHasher:
    """Hashing and verification on a bounded executor: ``ThreadPoolExecutor(2)`` with at most 8 queued jobs.

    A job submitted while ``workers + queue`` jobs are outstanding raises ``db.ServerBusy`` (REST answers
    ``429 server_busy`` with ``Retry-After: 1``; the WS requests that hash answer ``server_busy``, SPEC §2.3).
    """

    def __init__(self, scrypt_n: int = SCRYPT_N, workers: int = HASH_WORKERS, queue: int = HASH_QUEUE) -> None:
        self.scrypt_n = scrypt_n
        self._workers = workers
        self._capacity = workers + queue
        self._executor: Optional[ThreadPoolExecutor] = None
        self._outstanding = 0
        self._lock = threading.Lock()
        self._dummy: Optional[str] = None

    def _release(self, _future: Any) -> None:
        with self._lock:
            self._outstanding -= 1

    def _submit(self, fn: Callable[..., Any], *args: Any) -> "asyncio.Future[Any]":
        with self._lock:
            if self._outstanding >= self._capacity:
                raise dbmod.ServerBusy("too many password operations in flight", retry_after=1.0)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="hash")
            self._outstanding += 1
            executor = self._executor
        job = executor.submit(fn, *args)
        job.add_done_callback(self._release)
        return asyncio.wrap_future(job, loop=asyncio.get_running_loop())

    def _dummy_hash(self) -> str:
        with self._lock:
            if self._dummy is None:
                self._dummy = hash_password_sync("dummy password for timing equalisation", self.scrypt_n)
            return self._dummy

    def _verify(self, password: str, stored: Optional[str]) -> Tuple[bool, bool]:
        if stored is None:
            verify_password_sync(password, self._dummy_hash(), self.scrypt_n)
            return False, False
        return verify_password_sync(password, stored, self.scrypt_n)

    async def hash(self, password: str) -> str:
        """Hash on the executor (``db.ServerBusy`` when the queue is full)."""
        return await self._submit(hash_password_sync, password, self.scrypt_n)

    async def verify(self, password: str, stored: Optional[str]) -> Tuple[bool, bool]:
        """Verify on the executor; ``stored=None`` (unknown user) checks a dummy hash and answers ``(False, False)``."""
        return await self._submit(self._verify, password, stored)

    def shutdown(self) -> None:
        """Stop the worker threads (waits for running jobs)."""
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)


# --------------------------------------------------------------------------------------------------------------------
# Password policy (SPEC §4.1)
# --------------------------------------------------------------------------------------------------------------------

COMMON_PASSWORDS: Tuple[str, ...] = tuple(
    """
    password password1 password12 password123 password1234 password! password@1 passw0rd p@ssw0rd p@ssword p@55w0rd
    pa55word pa$$word passw0rd1 12345678 123456789 1234567890 12345678910 11111111 111111111 1111111111 00000000
    000000000 0000000000 12341234 123123123 1234512345 12345679 87654321 987654321 9876543210 123456123456 12121212
    11223344 1q2w3e4r 1q2w3e4r5t 1qaz2wsx 1qaz2wsx3edc 1qazxsw2 zaq12wsx qazwsxedc qazwsx123 q1w2e3r4 q1w2e3r4t5
    qwertyui qwertyuiop qwerty12 qwerty123 qwerty1234 qwerty12345 qwerty123456 qwertyuiop123 qwerty! qwerty@123
    asdfghjk asdfghjkl asdfgh123 asdf1234 asdfasdf asdfjkl; zxcvbnm zxcvbnm1 zxcvbnm123 zxcvbnm,. qweasdzxc
    qweasd123 abcd1234 abcd12345 abc12345 abc123456 abcdefgh abcdefghi abcdefghij abcdef123 abc123abc a1b2c3d4
    a1b2c3d4e5 iloveyou iloveyou1 iloveyou123 iloveyou2 iloveu12 iloveyou! ilovemyself ilovegod ilovemom ilovedad
    letmein1 letmein12 letmein123 letmein! welcome1 welcome12 welcome123 welcome! welcome@123 welcome2020
    welcome2021 welcome2022 welcome2023 welcome2024 welcome2025 admin123 admin1234 admin12345 admin@123 admin@1234
    administrator administrator1 adminadmin root1234 rootroot toor1234 changeme changeme1 changeme123 change123
    letmeinnow default123 default1 test1234 test12345 testtest testing123 testing1 tester123 guest123 guest1234
    guestguest user1234 user12345 useruser master123 mastermaster monkey123 monkey12 monkeymonkey dragon123 dragon12
    dragondragon football football1 football123 baseball baseball1 baseball123 basketball soccer123 hockey123
    cricket123 cricket1 batman123 batman12 superman superman1 superman123 spiderman spiderman1 starwars starwars1
    pokemon123 pokemon1 shadow123 shadow12 sunshine sunshine1 sunshine123 princess princess1 princess123 butterfly
    butterfly1 flower123 flowers1 chocolate chocolate1 cookie123 cookies1 summer123 summer2020 summer2021 summer2022
    summer2023 summer2024 summer2025 winter123 winter2020 winter2021 winter2022 winter2023 winter2024 winter2025
    spring123 autumn123 january1 february1 monday123 friday123 sunday123 jan12345 trustno1 trustno12 whatever
    whatever1 whatever123 freedom1 freedom123 hello123 hello1234 hello12345 helloworld helloworld1 hellohello
    secret123 secret12 secretsecret mypassword mypassword1 mypassword123 mypass123 mypasswd1 yourpassword
    newpassword newpassword1 oldpassword password0 password00 password11 password2 password22 password99 pass1234
    pass12345 passpass passpass1 passwort passwort1 contrasena contrasena1 motdepasse bismillah internet internet1
    computer computer1 computer123 microsoft windows10 windows11 windows123 google123 facebook facebook1 instagram
    twitter123 company123 company1 office123 office2020 office2021 office2022 office2023 office2024 office2025
    login123 login1234 loginlogin access123 access1234 pakistan pakistan1 pakistan123 india123 india1234 islamabad
    lahore123 karachi123 asdf@123 qwer@123 abcd@123 pass@123 pass@1234 test@123 user@123 india@123 pakistan@123
    """.split()  # noqa: SIM905 - a compact word list reads better than 250 quoted strings
)


def same_password(first: str, second: str) -> bool:
    """``True`` when two passwords are the same after NFKC normalisation (``POST /api/password`` ``same_as_old``)."""
    return hmac.compare_digest(_password_bytes(first), _password_bytes(second))


def weak_password(password: Any, username: str = "", display_name: str = "", min_len: int = 8) -> Optional[str]:
    """``None`` when ``password`` satisfies the policy of SPEC §4.1, else a short human reason (HTTP ``weak_password``).

    NFKC-normalises first; ``min_len <= length <= 128`` characters (too long is rejected before any hashing); not
    equal (casefold) to the username or the display name; not among :data:`COMMON_PASSWORDS`. ``same_as_old`` is the
    caller's check (it needs the old password).
    """
    if not isinstance(password, str):
        return "the password must be text"
    text = normalize_password(password)
    if len(text) > PASSWORD_MAX_LEN:
        return "the password must be at most %d characters" % PASSWORD_MAX_LEN
    if len(text) < min_len:
        return "the password must be at least %d characters" % min_len
    folded = text.casefold()
    for other in (username, display_name):
        if other and folded == unicodedata.normalize("NFKC", other).casefold():
            return "the password must not equal your username or name"
    if folded in _COMMON_SET:
        return "that password is too common"
    return None


_COMMON_SET = frozenset(COMMON_PASSWORDS)


# --------------------------------------------------------------------------------------------------------------------
# Session tokens, cookies, authenticate (SPEC §4.1, §6.2)
# --------------------------------------------------------------------------------------------------------------------

COOKIE_NAME = "fc_session"
SESSION_TOUCH_INTERVAL = 600.0
COOKIE_REISSUE_AFTER = 86400.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}\Z")


def token_hash(token: str) -> str:
    """``sha256(token)`` as hex: the only form of a session token the database ever sees."""
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def new_session_token() -> Tuple[str, str]:
    """``(token, token_hash)`` for a new session: ``secrets.token_urlsafe(32)`` and its SHA-256."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def session_public_id(hash_hex: str) -> str:
    """The public session id: the first 16 hex characters of the token hash."""
    return hash_hex[:16]


def parse_cookie(header: Optional[str]) -> Optional[str]:
    """The session token from a ``Cookie`` header value, or ``None`` (absent, duplicated name or malformed token)."""
    if not header:
        return None
    found: Optional[str] = None
    for piece in header.split(";"):
        name, sep, value = piece.strip().partition("=")
        if sep and name == COOKIE_NAME:
            if found is not None:
                return None
            found = value.strip().strip('"')
    return found if found is not None and _TOKEN_RE.match(found) else None


def cookie_header(token: str, secure: bool, max_age: int) -> str:
    """The ``Set-Cookie`` value for a session: HttpOnly, SameSite=Strict, ``Secure`` over TLS."""
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
        COOKIE_NAME,
        token,
        int(max_age),
        "; Secure" if secure else "",
    )


def clear_cookie_header(secure: bool) -> str:
    """The ``Set-Cookie`` value that deletes the session cookie."""
    return "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0%s" % (COOKIE_NAME, "; Secure" if secure else "")


async def authenticate(
    db: Any, token: Any, ip: str, user_agent: str, session_days: Optional[float] = None
) -> Optional[Dict[str, Any]]:
    """The one session check (SPEC §6.2): the session dict for a valid token, else ``None``.

    Returns ``{user_id, token_hash, ip, user_agent, must_change_password, reissue_cookie}``; ``None`` for an unknown or
    expired token or a disabled user. ``sessions.last_used_at`` slides at most every 10 minutes (the expiry becomes
    ``now + session_days``); ``reissue_cookie`` is true when such a slide moved the expiry by more than one day so
    the caller re-sends ``Set-Cookie``.
    """
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    days = _config["session_days"] if session_days is None else session_days
    digest = token_hash(token)
    now = util.now()
    found = await db.run_read(db_users.lookup_session, digest, now)
    if found is None:
        return None
    reissue = False
    if now - found["last_used_at"] >= SESSION_TOUCH_INTERVAL:
        expires_at = await db.run(db_users.touch_session, digest, days, now)
        if expires_at is None:
            return None
        reissue = expires_at - found["expires_at"] > COOKIE_REISSUE_AFTER
    return {
        "user_id": found["user_id"],
        "token_hash": digest,
        "ip": ip,
        "user_agent": user_agent,
        "must_change_password": found["must_change_password"],
        "reissue_cookie": reissue,
    }


# --------------------------------------------------------------------------------------------------------------------
# Throttles and limiters (SPEC §4.1): in memory, injectable clock and limits
# --------------------------------------------------------------------------------------------------------------------

_MAX_EXPONENT = 24


def _whole_seconds(value: float) -> float:
    """Round a remaining wait up to whole seconds (the ``retry_after`` / ``Retry-After`` the caller reports)."""
    return float(math.ceil(value - 1e-6)) if value > 1e-6 else 0.0


class _LruTimes:
    """A capped LRU map ``key -> deque of event times`` (oldest first)."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._items: "OrderedDict[Any, Deque[float]]" = OrderedDict()

    def times(self, key: Any, window: float, now: float, maxlen: int) -> Deque[float]:
        """The pruned deque of ``key`` (created on demand, most recently used)."""
        entry = self._items.get(key)
        if entry is None:
            entry = deque(maxlen=maxlen)
            self._items[key] = entry
            while len(self._items) > self._cap:
                self._items.popitem(last=False)
        else:
            self._items.move_to_end(key)
        while entry and entry[0] <= now - window:
            entry.popleft()
        return entry

    def peek(self, key: Any, window: float, now: float) -> Optional[Deque[float]]:
        entry = self._items.get(key)
        if entry is None:
            return None
        while entry and entry[0] <= now - window:
            entry.popleft()
        return entry

    def discard(self, key: Any) -> None:
        self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)


class LoginThrottle:
    """The three login counters of SPEC §4.1 with exponential delays; never locks.

    ``a``: per ``(ip, username key)`` 5 failures / 5 min, ``b``: per ``ip`` 20 / 10 min, ``c``: per username key
    30 / 15 min (``util.login_key``: the lowercased username, or ``?`` when it is not a valid username, so known and
    unknown usernames are counted identically). With ``n`` = failures inside the window minus the limit, a counter with
    ``n >= 1`` demands a pause of ``min(5 * 2**(n-1), 900)`` seconds after its most recent failure; :meth:`check`
    returns the largest remaining pause (``0.0`` = allowed). Defaults are scaled by ``util.scaled``; ``test_limits``
    (``login_a``/``login_b``/``login_c`` = ``[count, window_s]``) are used as given. Each map is capped at ``cap`` keys.
    """

    DEFAULTS = {"a": (5, 300.0), "b": (20, 600.0), "c": (30, 900.0)}

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        test_limits: Optional[Dict[str, Any]] = None,
        cap: int = 10000,
    ) -> None:
        self._clock = clock
        self._limits = test_limits or {}
        self._maps = {name: _LruTimes(cap) for name in self.DEFAULTS}

    def _limit(self, counter: str) -> Tuple[int, float]:
        override = self._limits.get("login_" + counter)
        if isinstance(override, (list, tuple)) and len(override) == 2:
            return int(override[0]), float(override[1])
        count, window = self.DEFAULTS[counter]
        return count, util.scaled(window)

    @staticmethod
    def _key(counter: str, ip: str, username: Any) -> Any:
        name = util.login_key(username)
        return {"a": (ip, name), "b": ip, "c": name}[counter]

    def check(self, ip: str, username: Any, counters: str = "abc") -> float:
        """Seconds the caller must still wait (``0.0`` when the attempt may proceed)."""
        now = self._clock()
        worst = 0.0
        for counter in counters:
            limit, window = self._limit(counter)
            times = self._maps[counter].peek(self._key(counter, ip, username), window, now)
            if not times:
                continue
            excess = len(times) - limit
            if excess < 1:
                continue
            delay = util.scaled(min(5.0 * 2 ** min(excess - 1, _MAX_EXPONENT), 900.0))
            worst = max(worst, times[-1] + delay - now)
        return _whole_seconds(worst)

    def record_failure(self, ip: str, username: Any, counters: str = "abc") -> None:
        """Count one failed attempt in the selected counters (``'a'`` alone for a failed password change)."""
        now = self._clock()
        for counter in counters:
            limit, window = self._limit(counter)
            key = self._key(counter, ip, username)
            self._maps[counter].times(key, window, now, limit + _MAX_EXPONENT).append(now)

    def record_success(self, ip: str, username: Any) -> None:
        """A successful login clears counter (a) of this ``(ip, username)`` only."""
        self._maps["a"].discard(self._key("a", ip, username))

    def sizes(self) -> Dict[str, int]:
        """Number of keys held per counter (tests; each is capped)."""
        return {name: len(entries) for name, entries in self._maps.items()}


class GuessLimiter:
    """Wrong ``setup_code`` / ``join_code`` guesses: ``limit`` failures per IP inside ``window`` seconds, then 429."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        limit: int = 5,
        window: float = 600.0,
        cap: int = 10000,
    ) -> None:
        self._clock = clock
        self._limit = limit
        self._window = window
        self._times = _LruTimes(cap)

    def check(self, ip: str) -> float:
        """Seconds until the oldest counted failure expires when ``ip`` is blocked, else ``0.0``."""
        now = self._clock()
        window = util.scaled(self._window)
        times = self._times.peek(ip, window, now)
        if times is None or len(times) < self._limit:
            return 0.0
        return _whole_seconds(times[0] + window - now)

    def record_failure(self, ip: str) -> None:
        now = self._clock()
        window = util.scaled(self._window)
        self._times.times(ip, window, now, self._limit + 1).append(now)


class RegistrationLimiter:
    """5 successful registrations per hour per IP and 60 per hour overall (SPEC §4.1).

    ``test_limits`` keys ``reg_per_ip_hour`` and ``reg_global_hour`` (ints) override the counts.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        test_limits: Optional[Dict[str, Any]] = None,
        window: float = 3600.0,
        cap: int = 10000,
    ) -> None:
        limits = test_limits or {}
        self._clock = clock
        self._per_ip = int(limits.get("reg_per_ip_hour", 5))
        self._global = int(limits.get("reg_global_hour", 60))
        self._window = window
        self._ips = _LruTimes(cap)
        self._all = _LruTimes(1)

    def check(self, ip: str) -> float:
        """Seconds to wait when ``ip`` or the whole server reached its hourly limit, else ``0.0``."""
        now = self._clock()
        window = util.scaled(self._window)
        waits = [0.0]
        per_ip = self._ips.peek(ip, window, now)
        if per_ip is not None and len(per_ip) >= self._per_ip:
            waits.append(per_ip[0] + window - now)
        overall = self._all.peek("all", window, now)
        if overall is not None and len(overall) >= self._global:
            waits.append(overall[0] + window - now)
        return _whole_seconds(max(waits))

    def record(self, ip: str) -> None:
        """Count one successful registration."""
        now = self._clock()
        window = util.scaled(self._window)
        self._ips.times(ip, window, now, self._per_ip + 1).append(now)
        self._all.times("all", window, now, self._global + 1).append(now)


# --------------------------------------------------------------------------------------------------------------------
# Process-wide configuration
# --------------------------------------------------------------------------------------------------------------------

_config: Dict[str, Any] = {"session_days": 30, "min_password_len": 8}
hasher = PasswordHasher()
login_throttle = LoginThrottle()
registration_limiter = RegistrationLimiter()
guess_limiter = GuessLimiter()


def configure(
    scrypt_n: int = SCRYPT_N,
    session_days: float = 30,
    min_password_len: int = 8,
    test_limits: Optional[Dict[str, Any]] = None,
) -> None:
    """Install the process-wide hasher and limiters from ``cfg.scrypt_n``, ``cfg.session_days``,
    ``cfg.min_password_len`` and ``cfg.test_limits`` (SPEC §2.1). Replaces any earlier configuration.
    """
    global hasher, login_throttle, registration_limiter, guess_limiter
    hasher.shutdown()
    _config["session_days"] = session_days
    _config["min_password_len"] = min_password_len
    hasher = PasswordHasher(scrypt_n)
    login_throttle = LoginThrottle(test_limits=test_limits)
    registration_limiter = RegistrationLimiter(test_limits=test_limits)
    guess_limiter = GuessLimiter()


async def hash_password(password: str) -> str:
    """Hash with the configured policy on the bounded executor (``db.ServerBusy`` when it is full)."""
    return await hasher.hash(password)


async def verify_password(password: str, stored: Optional[str]) -> Tuple[bool, bool]:
    """``(ok, needs_rehash)``; ``stored=None`` (unknown user) verifies a dummy hash so timing reveals nothing."""
    return await hasher.verify(password, stored)


def check_password_policy(password: Any, username: str = "", display_name: str = "") -> Optional[str]:
    """:func:`weak_password` with the configured ``min_password_len``."""
    return weak_password(password, username, display_name, _config["min_password_len"])


# --------------------------------------------------------------------------------------------------------------------
# Setup code and join code (SPEC §4.3)
# --------------------------------------------------------------------------------------------------------------------

SETUP_CODE_FILE = "setup_code.txt"
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{8}\Z")
_setup_codes: Dict[str, str] = {}


def _setup_path(data_dir: Any) -> str:
    return os.path.join(str(data_dir), SETUP_CODE_FILE)


def _data_key(data_dir: Any) -> str:
    return os.path.normcase(os.path.abspath(str(data_dir)))


def _expected_setup_code(data_dir: Any) -> Optional[str]:
    cached = _setup_codes.get(_data_key(data_dir))
    if cached is not None:
        return cached
    try:
        with open(_setup_path(data_dir), encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return text if _CODE_RE.match(text) else None


async def ensure_setup_code(db: Any, data_dir: Any) -> Optional[str]:
    """While ``users`` is empty: create or reuse ``<data>/setup_code.txt`` (private file) and return the code.

    Otherwise delete a stale file and return ``None``. Called once by ``app.py`` after the migration.
    """
    if not await db.run_read(db_users.needs_setup):
        clear_setup_code(data_dir)
        return None
    code = _expected_setup_code(data_dir)
    if code is None:
        code = util.short_code()
        try:
            util.write_private_file(_setup_path(data_dir), code + "\n")
        except OSError as exc:
            log.error("cannot write %s: %s", SETUP_CODE_FILE, type(exc).__name__)
    _setup_codes[_data_key(data_dir)] = code
    return code


def verify_setup_code(data_dir: Any, code: Any) -> bool:
    """Constant-time comparison of ``code`` with the current setup code of ``data_dir`` (``False`` when none exists)."""
    if not isinstance(code, str):
        return False
    expected = _expected_setup_code(data_dir)
    if expected is None:
        return False
    return hmac.compare_digest(code.encode("utf-8"), expected.encode("utf-8"))


def clear_setup_code(data_dir: Any) -> None:
    """Forget the setup code and delete ``setup_code.txt`` (idempotent)."""
    _setup_codes.pop(_data_key(data_dir), None)
    util.retry_file_op(os.remove, _setup_path(data_dir))


async def check_join_code(db: Any, code: Any) -> bool:
    """Constant-time comparison of ``code`` with ``meta.join_code`` (a cheap pre-check before hashing a password)."""
    if not isinstance(code, str) or not code:
        return False
    stored = await db.run_read(dbmod.get_meta, "join_code")
    return bool(stored) and hmac.compare_digest(code.encode("utf-8"), stored.encode("utf-8"))
