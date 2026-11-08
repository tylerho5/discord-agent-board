"""Pull-only Discord message board for people and their coding agents.

Reads go through Discord REST with the team's shared bot; posts go through one
channel-scoped webhook. Nothing here runs between invocations. Standard library
only, Python 3.9+. Usage and setup: the README.
"""

import hashlib
import http.client
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlencode, urlsplit

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/tylerho5/discord-agent-board, 1)"
DISCORD_EPOCH_MS = 1420070400000
REQUEST_TIMEOUT_S = 15
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_WAIT_S = 60
MAX_CONTENT_CHARS = 2000
GUILD_TEXT, PUBLIC_THREAD = 0, 11
CONTENT_MESSAGE_TYPES = (0, 19)  # default and reply; system messages are skipped
PAGE_LIMIT = 50
FETCH_LIMIT = 100
MAX_PAGES_PER_LOCATION = 20
MAX_ARCHIVE_PAGES = 10
FIRST_USE_WINDOW_S = 72 * 3600
CLOCK_MARGIN_S = 30
MAX_SNOWFLAKE = (1 << 63) - 1
RECENT_LIMIT = 20
CONTEXT_MESSAGES = 5
MAX_SEEN = 1000
THREAD_NAME_CHARS = 100
OWN_THREAD_TTL_S = 30 * 24 * 3600

# env vars that identify the running agent session, most specific first; the first one set wins
SESSION_ENV = ("DISCORD_BOARD_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "PI_SESSION_ID")
SESSION_SUFFIX_RE = re.compile(r"\.[0-9a-f]{4}$")
# Discord rejects webhook usernames containing these (HTTP 400), so handles may not either
DISCORD_RESERVED = {"discord": "dc", "clyde": "cl"}
RESERVED_RE = re.compile("|".join(DISCORD_RESERVED), re.IGNORECASE)
HANDLE_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,78}[A-Za-z0-9_])?")
WEBHOOK_RE = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([A-Za-z0-9_-]+)/?")
_WEBHOOK_PATH_RE = re.compile(r"(webhooks/\d+/)[A-Za-z0-9_.-]+")


class BoardError(Exception):
    """An operation failed; the message is safe to show (credentials redacted)."""


class ScopeError(BoardError):
    """The target is outside the configured board channel and its public threads."""


class NotFound(BoardError):
    pass


class DeliveryUncertain(BoardError):
    """A post may or may not have landed; it is never retried automatically."""


class ThreadNotStarted(BoardError):
    """The post landed but its thread could not be opened; ``message`` is the post."""

    def __init__(self, text, message):
        super().__init__(text)
        self.message = message


@dataclass(frozen=True)
class BoardConfig:
    guild_id: Optional[str]
    channel_id: Optional[str]
    agent: Optional[str]
    bot_token: Optional[str] = field(default=None, repr=False)
    webhook_url: Optional[str] = field(default=None, repr=False)
    state_dir: Optional[Path] = None


def _read_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise BoardError(f"cannot read {path}: {err.strerror}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise BoardError(f"{path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise BoardError(f"{path} must hold a JSON object")
    return data


def _write_json(path, data):
    """Atomically write JSON; the temp file's 0600 mode carries over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect, so credentials cannot be re-sent to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener():
    return urllib.request.build_opener(_NoRedirect)


def _is_id(text):
    return isinstance(text, str) and text.isascii() and text.isdigit()


def _expect(value, kind, what):
    if not isinstance(value, kind):
        raise BoardError(f"unexpected response from Discord for {what}")
    return value


def _reserved_free(text):
    """Rewrite Discord's forbidden words until none is left, since a rewrite can join its neighbours."""
    while True:
        rewritten = RESERVED_RE.sub(lambda m: DISCORD_RESERVED[m.group(0).lower()], text)
        if rewritten == text:
            return text
        text = rewritten


def _slug(text, limit=36):
    slug = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-._")
    return _reserved_free(slug)[:limit].strip("-._")


def handle_family(handle):
    """The handle without its session suffix; every session on one piece of work shares it."""
    return SESSION_SUFFIX_RE.sub("", handle)


def session_suffix(env):
    """A short stable tag for this agent session, or empty when the harness exposes none."""
    for name in SESSION_ENV:
        value = (env.get(name) or "").strip()
        if value:
            return "." + hashlib.sha256(value.encode("utf-8")).hexdigest()[:4]
    return ""


def derive_handle(person, env, cwd):
    """``<person>/<work>.<session>``: the person's name, the checkout's folder name and a tag
    for the agent session, so two sessions on one checkout keep separate handles and read state."""
    cwd = Path(cwd)
    root = next((d for d in (cwd, *cwd.parents) if (d / ".git").exists()), cwd)
    who = _slug(person) or _slug(env.get("USER") or env.get("USERNAME"))
    work = _slug(root.name)
    handle = f"{who}/{work}{session_suffix(env)}" if who and work else None
    return handle if handle and HANDLE_RE.fullmatch(handle) else None


def settings_path_for(env, config_path=None):
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    return Path(config_path or home / ".config" / "discord-agent-board" / "config.json")


def load_config(overrides, *, env, config_path=None, cwd=None):
    """Merge CLI overrides, then DISCORD_BOARD_* environment, then local files.

    Nonsecret settings live in ~/.config/discord-agent-board/config.json and
    secrets in credentials.json beside it (or the environment).
    The bot token falls back to DISCORD_BOT_TOKEN, then the mcp-discord
    config's ``botToken``; a user-account ``token`` is never read.
    """
    home = Path(env["HOME"]) if env.get("HOME") else Path.home()
    if not config_path:
        config_path = home / ".config" / "discord-agent-board" / "config.json"
    config_path = Path(config_path)
    settings = _read_json(config_path)
    creds = _read_json(config_path.parent / "credentials.json")

    def pick(key, env_name):
        for source in (overrides.get(key), env.get(env_name), settings.get(key)):
            if source not in (None, ""):
                return str(source)
        return None

    bot_token = (env.get("DISCORD_BOARD_BOT_TOKEN") or creds.get("bot_token")
                 or env.get("DISCORD_BOT_TOKEN"))
    if not bot_token:
        bot_token = _read_json(home / ".config" / "mcp-discord" / "config.json").get("botToken")
    state_dir = pick("state_dir", "DISCORD_BOARD_STATE_DIR")
    agent = pick("agent", "DISCORD_BOARD_AGENT") or derive_handle(
        settings.get("person"), env, cwd or Path.cwd())
    return BoardConfig(
        guild_id=pick("guild_id", "DISCORD_BOARD_GUILD_ID"),
        channel_id=pick("channel_id", "DISCORD_BOARD_CHANNEL_ID"),
        agent=agent,
        bot_token=bot_token or None,
        webhook_url=env.get("DISCORD_BOARD_WEBHOOK_URL") or creds.get("webhook_url") or None,
        state_dir=Path(state_dir).expanduser() if state_dir
        else home / ".local" / "state" / "discord-agent-board",
    )


def validate_handle(agent):
    if not agent or "/" not in agent or not HANDLE_RE.fullmatch(agent):
        raise BoardError(
            f"agent handle {agent!r} is invalid: use <name>/<work> with letters, digits, '.', '_', "
            "'-' and '/', up to 80 characters, e.g. tyler/api-refactor")
    reserved = RESERVED_RE.search(agent)
    if reserved:
        raise BoardError(
            f"agent handle {agent!r} contains {reserved.group(0)!r}, which Discord does not allow in "
            f"a webhook name; use something like {_reserved_free(agent)!r}")
    return agent


def setup_target(env):
    """The setup endpoint URL and bearer token from ``DISCORD_BOARD_SETUP_URL`` and
    ``DISCORD_BOARD_SETUP_TOKEN``, or None. The URL must be https."""
    url, token = ((env.get(k) or "").strip() for k in ("DISCORD_BOARD_SETUP_URL", "DISCORD_BOARD_SETUP_TOKEN"))
    return (url, token) if url.lower().startswith("https://") and token else None


def fetch_setup(url, *, bearer=None, opener=None):
    """Fetch the shared board settings from a setup link, or from the setup endpoint with a bearer token."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise BoardError("the setup URL must be an https link")
    label = f"GET {parts.hostname}"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request = urllib.request.Request(url, headers=headers)
    if bearer:
        request.add_unredirected_header("Authorization", "Bearer " + bearer)
    try:
        with (opener or default_opener()).open(
                request, timeout=REQUEST_TIMEOUT_S) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code in (401, 403, 429):
            raise BoardError(
                f"{label}: HTTP {err.code}; the setup link is invalid, expired or "
                "rate-limited, or this machine's setup token was revoked. "
                "Ask whoever runs your board for a fresh setup link") from None
        raise BoardError(f"{label}: HTTP {err.code}") from None
    except (OSError, ValueError, http.client.HTTPException) as err:
        raise BoardError(f"{label}: {err}") from None
    channel_id, bot_token = (data.get(k) if isinstance(data, dict) else None
                             for k in ("channel_id", "bot_token"))
    webhook_url = data.get("webhook_url") if isinstance(data, dict) else None
    if not (_is_id(channel_id)
            and isinstance(bot_token, str) and bot_token):
        raise BoardError(f"{label}: the setup endpoint has no board configured")
    if webhook_url is not None and not WEBHOOK_RE.fullmatch(str(webhook_url)):
        raise BoardError(f"{label}: the setup endpoint returned an invalid webhook URL")
    person = data.get("person")
    return {"channel_id": channel_id, "bot_token": bot_token, "webhook_url": webhook_url or None,
            "person": person if isinstance(person, str) and person else None}


def install_setup(url, *, bearer=None, agent=None, env, config_path=None, opener=None):
    """Fetch the board settings, check them against Discord, then write the local files.

    The server (guild) is learned from the board channel, so only the channel ID is configured.

    Returns the two paths written and whether posting is configured. Nothing
    secret is printed or returned.
    """
    if agent is not None:
        validate_handle(agent)
    shared = fetch_setup(url, bearer=bearer, opener=opener)
    base = load_config({}, env=env, config_path=config_path)
    config = replace(base, channel_id=shared["channel_id"], bot_token=shared["bot_token"],
                     webhook_url=shared["webhook_url"])
    channel = DiscordClient(config, opener=opener)._request("GET", f"{API}/channels/{config.channel_id}")
    guild_id = str(channel.get("guild_id") or "")
    if not guild_id:
        raise BoardError("the board channel is not in a server")
    config = replace(config, guild_id=guild_id)
    client = DiscordClient(config, opener=opener)
    client.board_channel()
    if config.webhook_url:
        client.check_webhook()
    settings_path = settings_path_for(env, config_path)
    creds_path = settings_path.parent / "credentials.json"
    settings = _read_json(settings_path)
    settings["channel_id"] = config.channel_id
    settings["guild_id"] = config.guild_id
    if shared["person"]:
        settings["person"] = shared["person"]
    if agent:
        settings["agent"] = agent
    creds = {"bot_token": config.bot_token}
    if config.webhook_url:
        creds["webhook_url"] = config.webhook_url
    created = not settings_path.parent.exists()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(settings_path.parent, 0o700)
        except OSError:
            pass
    _write_json(creds_path, creds)
    _write_json(settings_path, settings)
    handle = load_config({}, env=env, config_path=settings_path).agent
    return {"settings": settings_path, "credentials": creds_path,
            "posting": bool(config.webhook_url), "agent": handle}


def learn_guild(config, *, env, config_path=None, opener=None):
    """Learn the board channel's server when only the channel ID is configured.

    The result is saved to the config file when that file names the same channel.
    """
    channel = _expect(DiscordClient(config, opener=opener)._request(
        "GET", f"{API}/channels/{config.channel_id}"), dict, "a channel")
    guild_id = str(channel.get("guild_id") or "")
    if not guild_id:
        raise BoardError("the board channel is not in a server")
    path = settings_path_for(env, config_path)
    try:
        settings = _read_json(path)
        if settings.get("channel_id") == config.channel_id:
            settings["guild_id"] = guild_id
            _write_json(path, settings)
    except (BoardError, OSError):
        pass
    return replace(config, guild_id=guild_id)


def auto_setup(env, *, config_path=None, opener=None):
    """Configure the board from this machine's setup token; return the result or None."""
    target = setup_target(env)
    if not target:
        return None
    url, token = target
    return install_setup(url, bearer=token, env=env, config_path=config_path, opener=opener)


class DiscordClient:
    """Discord REST access scoped to one board channel and its public threads."""

    def __init__(self, config, *, opener=None, sleep=time.sleep):
        self.config = config
        self._opener = opener or default_opener()
        self._sleep = sleep
        self._scoped = {}
        self.server_time = None

    def redact(self, text):
        text = str(text)
        hook = WEBHOOK_RE.fullmatch(self.config.webhook_url or "")
        for secret in (self.config.webhook_url, hook and hook.group(2), self.config.bot_token):
            if secret:
                text = text.replace(secret, "[redacted]")
        return _WEBHOOK_PATH_RE.sub(r"\1[redacted]", text)

    def _request(self, method, url, *, body=None, bot=True, write=False):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if bot and not self.config.bot_token:
            raise BoardError("no bot token configured (see the README)")
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        label = self.redact(f"{method} {urlsplit(url).path}")
        uncertain = "; the message may have been posted, so check the channel before posting again"
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            if bot:
                request.add_unredirected_header("Authorization", "Bot " + self.config.bot_token)
            try:
                with self._opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
                    raw = response.read()
                    date = response.headers.get("Date")
                if date:
                    try:
                        stamp = parsedate_to_datetime(date).timestamp()
                    except (TypeError, ValueError, OverflowError):
                        pass
                    else:
                        self.server_time = (stamp if self.server_time is None
                                            else min(self.server_time, stamp))
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                payload = _error_payload(err)
                if err.code == 429:
                    wait = _retry_after(err, payload)
                    if attempt < MAX_RATE_LIMIT_RETRIES and wait <= MAX_RATE_LIMIT_WAIT_S:
                        self._sleep(wait)
                        continue
                    raise BoardError(
                        f"{label}: Discord rate limit persisted after {attempt} retries") from None
                detail = payload.get("message") if isinstance(payload, dict) else None
                message = self.redact(f"{label}: HTTP {err.code}" + (f" ({detail})" if detail else ""))
                if write and err.code >= 500:
                    raise DeliveryUncertain(message + uncertain) from None
                if err.code == 404:
                    raise NotFound(message) from None
                raise BoardError(message) from None
            except (OSError, ValueError, http.client.HTTPException) as err:
                message = self.redact(f"{label}: {err}")
                if write:
                    raise DeliveryUncertain(message + uncertain) from None
                raise BoardError(message) from None
        raise AssertionError("unreachable")

    def _scope(self, channel):
        cid = str(channel.get("id"))
        board = self.config.channel_id
        if str(channel.get("guild_id")) != self.config.guild_id:
            raise ScopeError(f"channel {cid} is not in the configured guild")
        if cid == board:
            if channel.get("type") != GUILD_TEXT:
                raise ScopeError(f"board channel {cid} is not a standard text channel")
        elif channel.get("type") != PUBLIC_THREAD or str(channel.get("parent_id")) != board:
            raise ScopeError(f"channel {cid} is not the board channel or one of its public threads")
        self._scoped[cid] = channel
        return channel

    def board_channel(self):
        if not (self.config.channel_id and self.config.guild_id):
            raise BoardError("no board channel configured (see the README)")
        return self.get_channel(self.config.channel_id)

    def get_channel(self, channel_id):
        cid = str(channel_id)
        if cid in self._scoped:
            return self._scoped[cid]
        if cid != self.config.channel_id:
            self.board_channel()
        return self._scope(_expect(self._request("GET", f"{API}/channels/{cid}"), dict, "a channel"))

    def refresh_channel(self, channel_id):
        self._scoped.pop(str(channel_id), None)
        return self.get_channel(channel_id)

    def get_message(self, channel_id, message_id):
        self.get_channel(channel_id)
        return _expect(self._request("GET", f"{API}/channels/{channel_id}/messages/{message_id}"),
                       dict, "a message")

    def get_messages(self, channel_id, *, before=None, after=None, limit=100):
        self.get_channel(channel_id)
        params = {k: v for k, v in (("before", before), ("after", after)) if v is not None}
        params["limit"] = limit
        return _expect(self._request("GET", f"{API}/channels/{channel_id}/messages?{urlencode(params)}"),
                       list, "messages")

    def _board_threads(self, threads):
        board = self.config.channel_id
        return [self._scope(t) for t in threads
                if t.get("type") == PUBLIC_THREAD and str(t.get("parent_id")) == board
                and str(t.get("guild_id")) == self.config.guild_id]

    def active_threads(self):
        self.board_channel()
        data = _expect(self._request("GET", f"{API}/guilds/{self.config.guild_id}/threads/active"),
                       dict, "active threads")
        return self._board_threads(data.get("threads", []))

    def archived_threads(self, *, before=None, limit=100):
        self.board_channel()
        params = {"limit": limit}
        if before:
            params["before"] = before
        data = _expect(self._request(
            "GET", f"{API}/channels/{self.config.channel_id}/threads/archived/public?{urlencode(params)}"),
            dict, "archived threads")
        return {"threads": self._board_threads(data.get("threads", [])),
                "has_more": bool(data.get("has_more"))}

    def check_webhook(self):
        """Return the webhook's API URL after confirming it belongs to the board channel."""
        match = WEBHOOK_RE.fullmatch(self.config.webhook_url or "")
        if not match:
            raise BoardError("no valid webhook URL configured; posting is unavailable (reads still work)")
        hook_url = f"{API}/webhooks/{match.group(1)}/{match.group(2)}"
        hook = _expect(self._request("GET", hook_url, bot=False), dict, "the webhook")
        if (str(hook.get("channel_id")) != self.config.channel_id
                or str(hook.get("guild_id")) != self.config.guild_id):
            raise ScopeError("the configured webhook does not belong to the board channel")
        return hook_url

    def post(self, text, *, agent, thread_id=None):
        if not text or not text.strip():
            raise BoardError("refusing to post an empty message")
        if len(text) > MAX_CONTENT_CHARS:
            raise BoardError(f"message is {len(text)} characters; Discord allows {MAX_CONTENT_CHARS}")
        validate_handle(agent)
        hook_url = self.check_webhook()
        query = {"wait": "true"}
        if thread_id and str(thread_id) != self.config.channel_id:
            self.get_channel(thread_id)
            query["thread_id"] = str(thread_id)
        body = {"content": text, "username": agent, "allowed_mentions": {"parse": []}}
        sent = self._request("POST", f"{hook_url}?{urlencode(query)}", body=body, bot=False, write=True)
        if not (isinstance(sent, dict) and sent.get("id")):
            raise DeliveryUncertain(
                "Discord answered with an unexpected body; the message may have been posted, "
                "so check the channel before posting again")
        return sent

    def start_thread(self, message_id, name):
        """Open a public thread on a board-channel message; the bot needs Create Public Threads."""
        try:
            thread = self._request("POST", f"{API}/channels/{self.config.channel_id}/messages/"
                                           f"{message_id}/threads", body={"name": name})
        except BoardError as err:
            if " HTTP 403" in str(err):
                raise BoardError(f"{err}; the board's bot needs the Create Public Threads "
                                 "permission in the board channel") from None
            raise
        return self._scope(_expect(thread, dict, "a thread"))


def _error_payload(err):
    try:
        return json.loads(err.read() or b"null")
    except (OSError, ValueError):
        return None


def _retry_after(err, payload):
    if isinstance(payload, dict) and isinstance(payload.get("retry_after"), (int, float)):
        wait = float(payload["retry_after"])
    else:
        try:
            wait = float(err.headers.get("Retry-After"))
        except (AttributeError, TypeError, ValueError):
            return 1.0
    return max(0.0, wait) if math.isfinite(wait) else 1.0


def snowflake_at(seconds):
    return max(0, int(seconds * 1000) - DISCORD_EPOCH_MS) << 22


def snowflake_seconds(snowflake):
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


def _utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _iso_seconds(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _name_pattern(*names):
    names = sorted(set(names), key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9._/-])(?:" + "|".join(re.escape(n) for n in names)
                      + r")(?![A-Za-z0-9_/-]|\.[A-Za-z0-9_/-])", re.IGNORECASE)


def tag_pattern(agent):
    """The handle, or the work's shared handle without the session suffix, as a whole word.

    Case-insensitive. A longer handle sharing the prefix does not match, nor does a handle inside
    a path or URL. ``tyler/docs.3fa9`` is matched by that exact name and by plain ``tyler/cx``.
    """
    return _name_pattern(agent, handle_family(agent))


def _position(state, loc_id):
    """The newest message ID in a location that a saved reading state has scanned through."""
    if not state:
        return 0
    return int(state.get("positions", {}).get(loc_id, state.get("floor", 0)))


def _scanned(state, loc_id, mid):
    """Whether a saved reading state has passed the message: listed in ``seen``, or after the
    state's first-use cutoff and at or below its position."""
    if not state:
        return False
    if str(mid) in state.get("seen", ()):
        return True
    return int(state.get("start", 0)) < mid <= _position(state, loc_id)


def _author(msg):
    return (msg.get("author") or {}).get("username")


def _own_post(msg, agent):
    return bool(msg.get("webhook_id")) and _author(msg) == agent


def _tags(msg, agent, pattern=None):
    """Whether the message is addressed to the handle.

    Its own posts never are. A reply to one of its posts is, even without the name.
    """
    if _own_post(msg, agent):
        return False
    parent = msg.get("referenced_message") or {}
    if parent.get("webhook_id") and _author(parent) == agent:
        return True
    return bool((pattern or tag_pattern(agent)).search(msg.get("content") or ""))


_LINK_RE = re.compile(
    r"https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?/?")


@dataclass
class BoardPage:
    messages: List[dict]
    has_more: bool = False
    continuation: Optional[List[str]] = None
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    coverage_complete: bool = True
    view: str = "new"
    location: Optional[dict] = None
    writes: List[Tuple[Path, dict]] = field(default_factory=list, repr=False)

    def to_json(self):
        return {"view": self.view, "location": self.location, "messages": self.messages,
                "has_more": self.has_more, "continuation": self.continuation,
                "warnings": self.warnings, "notes": self.notes,
                "coverage_complete": self.coverage_complete}


class Board:
    """Reading views, the inbox and posting, over a scoped client and local reading state.

    Whatever a view shows counts as read for the handle, so no other view shows it again.
    """

    def __init__(self, client, config, *, clock=time.time):
        self.client = client
        self.config = config
        self._clock = clock
        self._own = {}

    # -- shared helpers ----------------------------------------------------

    def _normalize(self, msg, location, agent=None):
        author = msg.get("author") or {}
        content = msg.get("content") or ""
        for attachment in msg.get("attachments") or []:
            content += f"\n[attachment: {attachment.get('filename')} {attachment.get('url')}]"
        is_thread = location.get("type") == PUBLIC_THREAD
        channel_id = str(msg.get("channel_id") or location["id"])
        started = msg.get("thread")
        return {
            "id": str(msg["id"]),
            "channel_id": channel_id,
            "author": author.get("global_name") or author.get("username") or "unknown",
            "content": content,
            "timestamp": msg.get("timestamp"),
            "url": f"https://discord.com/channels/{self.config.guild_id}/{channel_id}/{msg['id']}",
            "thread_id": str(location["id"]) if is_thread else None,
            "thread_name": location.get("name") if is_thread else None,
            "mentioned": bool(agent and self._addressed(msg, channel_id, agent)),
            "own": bool(agent and _own_post(msg, agent)),
            "new": True,
            "thread": ({"id": str(started["id"]), "name": started.get("name"),
                        "message_count": started.get("message_count")}
                       if isinstance(started, dict) and started.get("id") else None),
        }

    def _state_path(self, view, agent):
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", agent)
        digest = hashlib.sha256(agent.encode("utf-8")).hexdigest()[:8]
        name = f"{self.config.guild_id}-{self.config.channel_id}-{view}-{slug}-{digest}.json"
        return Path(self.config.state_dir) / name

    def _load(self, view, agent):
        return _read_json(self._state_path(view, agent))

    @staticmethod
    def _fresh_state(now):
        since = now - FIRST_USE_WINDOW_S
        floor = str(snowflake_at(since))
        return {"start": floor, "floor": floor, "archived_since": since, "positions": {}}

    def _own_threads(self, agent):
        """Places the handle follows, each with the message ID it started following after: threads
        it started or posted in, and its channel posts, which a thread may later open on."""
        if agent not in self._own:
            try:
                data = self._load("threads", agent).get("threads", {})
            except BoardError:
                data = {}
            self._own[agent] = {str(k): int(v["since"]) for k, v in data.items()
                                if isinstance(v, dict) and _is_id(str(v.get("since")))}
        return self._own[agent]

    def _remember_thread(self, agent, place_id, since):
        path = self._state_path("threads", agent)
        now = self._clock()
        try:
            threads = _read_json(path).get("threads", {})
        except BoardError:
            threads = {}
        threads = {k: v for k, v in threads.items() if isinstance(v, dict)
                   and isinstance(v.get("at"), (int, float)) and v["at"] > now - OWN_THREAD_TTL_S}
        old = str(threads.get(str(place_id), {}).get("since"))
        since = str(min(int(old), int(since))) if _is_id(old) else str(since)
        threads[str(place_id)] = {"since": since, "at": now}
        _write_json(path, {"threads": threads})
        self._own.pop(agent, None)

    def _addressed(self, msg, loc_id, agent, pattern=None):
        """A tag, a reply to the handle's post, or a later message in one of its threads."""
        if _own_post(msg, agent):
            return False
        since = self._own_threads(agent).get(str(loc_id))
        if since is not None and int(msg["id"]) > since:
            return True
        return _tags(msg, agent, pattern)

    def _discover(self, archived_since, warnings):
        """Board channel plus public threads that can hold messages in the scanned range."""
        locations = {self.config.channel_id: self.client.board_channel()}
        complete = True
        try:
            for thread in self.client.active_threads():
                locations[thread["id"]] = thread
        except BoardError as err:
            warnings.append(f"active thread discovery failed: {err}")
            complete = False
        try:
            before = None
            for _ in range(MAX_ARCHIVE_PAGES):
                page = self.client.archived_threads(before=before, limit=FETCH_LIMIT)
                older = False
                for thread in page["threads"]:
                    stamp = (thread.get("thread_metadata") or {}).get("archive_timestamp")
                    if archived_since is not None and stamp and _iso_seconds(stamp) < archived_since:
                        older = True
                        break
                    locations.setdefault(thread["id"], thread)
                if older or not page["has_more"] or not page["threads"]:
                    break
                before = page["threads"][-1]["thread_metadata"]["archive_timestamp"]
            else:
                warnings.append(f"archived thread discovery stopped after {MAX_ARCHIVE_PAGES} pages")
                complete = False
        except BoardError as err:
            warnings.append(f"archived thread discovery failed: {err}")
            complete = False
        return locations, complete

    # -- unread views: read (new) and inbox ------------------------------------

    def inbox(self, *, agent, limit=PAGE_LIMIT):
        """Unread messages addressed to the handle, from the channel and every public thread."""
        validate_handle(agent)
        _check_limit(limit)
        pattern = tag_pattern(agent)
        reads = {}

        def read_state(loc_id):
            if loc_id not in reads:
                reads[loc_id] = self._load(f"read-{loc_id}", agent)
            return reads[loc_id]

        page = self._poll_forward(
            "inbox", agent, limit,
            discover=lambda state, warnings: self._discover(state.get("archived_since"), warnings),
            relevant=lambda msg, loc: self._addressed(msg, loc, agent, pattern),
            skip=lambda msg, loc: _scanned(read_state(loc), loc, int(msg["id"])),
            first_note="first inbox for {agent} on this machine: scanned messages since {since}; "
                       "earlier ones were not scanned")
        page.view = "inbox"
        return self._annotate(page, agent)

    def _read_new(self, location, agent, limit):
        loc_id = str(location["id"])
        inbox = self._load("inbox", agent)

        def skip(msg, loc):
            mid = int(msg["id"])
            return _own_post(msg, agent) or (_scanned(inbox, loc, mid) and (
                str(mid) in inbox.get("seen", ()) or self._addressed(msg, loc, agent)))

        return self._poll_forward(
            f"read-{loc_id}", agent, limit,
            discover=lambda state, warnings: ({loc_id: self.client.refresh_channel(loc_id)}, True),
            relevant=lambda msg, loc: True, skip=skip,
            first_note="first read here for {agent} on this machine: showing messages since {since}; "
                       "use --recent for older ones")

    def _poll_forward(self, view, agent, limit, *, discover, relevant, skip, first_note):
        now = self._clock()
        notes, warnings = [], []
        path = self._state_path(view, agent)
        state = _read_json(path) or _read_json(self._state_path(
            "mentions" if view == "inbox" else "check", agent))
        if not state:
            state = self._fresh_state(now)
            notes.append(first_note.format(agent=agent, since=_utc(state["archived_since"])))
        floor = int(state["floor"])
        positions = {k: int(v) for k, v in state.get("positions", {}).items()}
        seen = {int(i) for i in state.get("seen", ())}

        self.client.server_time = None
        locations, complete = discover(state, warnings)
        if self.client.server_time is not None:
            now = min(now, self.client.server_time)
        safe = snowflake_at(now - CLOCK_MARGIN_S)
        upper = MAX_SNOWFLAKE

        found, through = [], {}
        for loc_id in sorted(locations, key=int):
            location = locations[loc_id]
            start = positions.get(loc_id, floor)
            last = location.get("last_message_id")
            if last and int(last) <= start:
                through[loc_id] = upper
                continue
            try:
                hits, through[loc_id] = self._scan_forward(
                    location, start, upper, relevant, skip, limit, agent, seen)
            except BoardError as err:
                warnings.append(f"{location.get('name') or loc_id}: {err}")
                complete = False
                continue
            found.extend(hits)

        found.sort(key=lambda m: int(m["id"]))
        shown = found[:limit]
        boundary = int(shown[-1]["id"]) if len(found) > limit else upper
        cap = min(boundary, safe)
        new_positions = {k: max(positions.get(k, floor), min(cap, v)) for k, v in through.items()}
        shown_ids = seen | {int(m["id"]) for m in shown}
        has_more = len(found) > limit or any(v < upper for v in through.values())
        page = BoardPage(shown, has_more=has_more, warnings=warnings, notes=notes,
                         coverage_complete=complete)
        if not complete:
            page.has_more = False
            return page
        start = int(state.get("start", 0))
        if has_more:
            page.continuation = []
            new_state = dict(state, positions={k: str(v) for k, v in new_positions.items()},
                             seen=_seen_list(i for i in shown_ids if i > floor or i <= start))
        else:
            new_state = {"start": str(start), "floor": str(safe),
                         "archived_since": snowflake_seconds(safe), "positions": {},
                         "seen": _seen_list(i for i in shown_ids if i > safe or i <= start)}
        page.writes.append((path, new_state))
        return page

    def _scan_forward(self, location, start, upper, relevant, skip, limit, agent, seen):
        cursor, hits = start, []
        for _ in range(MAX_PAGES_PER_LOCATION):
            batch = sorted(self.client.get_messages(location["id"], after=str(cursor), limit=FETCH_LIMIT),
                           key=lambda m: int(m["id"]))
            for msg in batch:
                mid = int(msg["id"])
                if mid > upper:
                    return hits, upper
                cursor = mid
                if (mid not in seen and msg.get("type", 0) in CONTENT_MESSAGE_TYPES
                        and relevant(msg, location["id"]) and not skip(msg, location["id"])):
                    hits.append(self._normalize(msg, location, agent))
                    if len(hits) >= limit:
                        return hits, mid
            if len(batch) < FETCH_LIMIT:
                return hits, upper
        return hits, cursor

    def commit(self, page):
        """Save what a page marked as read; call after its output succeeded."""
        for path, state in page.writes:
            _write_json(path, state)

    # -- read ----------------------------------------------------------------

    def read(self, target=None, *, agent, recent=False, limit=None, before=None, thread=None):
        """The channel's top level or a thread: unread messages by default, or the newest
        ``limit`` with ``recent``/``before``. A message link shows that message in context."""
        validate_handle(agent)
        if limit is not None:
            _check_limit(limit)
        if target is None and thread:
            raise BoardError("--thread names the thread that holds a bare message ID; pass that ID too")
        if target is None:
            location, message_id = self.client.board_channel(), None
        else:
            location, message_id = self._resolve(str(target).strip(), thread)
        if message_id:
            if recent or before is not None:
                raise BoardError("--recent and --before page a thread or the channel, not a single message")
            page = self._mark_shown(self._context(location, message_id, limit or PAGE_LIMIT, agent), agent)
            page.view = "context"
        elif recent or before is not None:
            page = self._mark_shown(self._history(location, limit or RECENT_LIMIT, before, agent), agent)
            page.view = "recent"
            if page.continuation and location.get("type") == PUBLIC_THREAD:
                page.continuation.insert(0, str(location["id"]))
        else:
            page = self._read_new(location, agent, limit or PAGE_LIMIT)
        page.location = self._describe(location, agent)
        return self._annotate(page, agent)

    def _resolve(self, target, thread):
        link = _LINK_RE.fullmatch(target)
        if link:
            guild, channel_id, message_id = link.groups()
            if guild != self.config.guild_id:
                raise ScopeError("that link points to another Discord server")
        elif _is_id(target):
            channel_id, message_id = self._resolve_bare_id(target, thread)
        else:
            raise BoardError("read takes a Discord message/thread link or a numeric ID")
        return self.client.get_channel(channel_id), message_id

    def _resolve_bare_id(self, target, thread):
        if thread:
            return str(thread), target
        if target == self.config.channel_id:
            return target, None
        try:
            self.client.get_channel(target)
            return target, None
        except NotFound:
            pass
        try:
            self.client.get_message(self.config.channel_id, target)
            return self.config.channel_id, target
        except NotFound:
            raise BoardError(
                f"{target} is not a thread or a message in the board channel; pass a message link "
                "(Copy Message Link in Discord) or --thread THREAD_ID") from None

    def _describe(self, location, agent):
        if location.get("type") != PUBLIC_THREAD:
            return {"type": "channel", "id": str(location["id"]), "name": location.get("name")}
        return {"type": "thread", "id": str(location["id"]), "name": location.get("name"),
                "message_count": location.get("message_count"),
                "subscribed": str(location["id"]) in self._own_threads(agent)}

    def _context(self, location, message_id, limit, agent):
        n = min(CONTEXT_MESSAGES, max(0, (limit - 1) // 2))
        target = self.client.get_message(location["id"], message_id)
        around = [target]
        if n:
            around += self.client.get_messages(location["id"], before=message_id, limit=n)
            around += self.client.get_messages(location["id"], after=message_id, limit=n)
        around = [m for m in around if m is target or m.get("type", 0) in CONTENT_MESSAGE_TYPES]
        around.sort(key=lambda m: int(m["id"]))
        return BoardPage([self._normalize(m, location, agent) for m in around])

    def _history(self, location, limit, before, agent):
        batch = self.client.get_messages(location["id"], before=before, limit=min(limit, FETCH_LIMIT))
        batch.sort(key=lambda m: int(m["id"]))
        messages = [self._normalize(m, location, agent) for m in batch
                    if m.get("type", 0) in CONTENT_MESSAGE_TYPES]
        page = BoardPage(messages)
        if len(batch) >= min(limit, FETCH_LIMIT) and batch:
            page.has_more = True
            page.continuation = ["--before", str(batch[0]["id"])]
        elif location.get("type") == PUBLIC_THREAD:
            try:
                starter = self.client.get_message(self.config.channel_id, location["id"])
                board = self.client.board_channel()
                page.messages.insert(0, self._normalize(starter, board, agent))
            except NotFound:
                pass
        return page

    def _was_read(self, agent, loc_id, mid, addressed, states):
        """Whether any view has already shown the handle this message."""
        def load(view):
            if view not in states:
                states[view] = self._load(view, agent)
            return states[view]
        if _scanned(load(f"read-{loc_id}"), loc_id, mid):
            return True
        inbox = load("inbox")
        return _scanned(inbox, loc_id, mid) and (addressed or str(mid) in inbox.get("seen", ()))

    def _mark_shown(self, page, agent):
        """Flag what this page shows for the first time, and record it all as read."""
        states, fresh = {}, {}
        for m in page.messages:
            loc, mid = m["channel_id"], int(m["id"])
            m["new"] = not (m["own"] or self._was_read(agent, loc, mid, m["mentioned"], states))
            if m["new"]:
                fresh.setdefault(loc, set()).add(mid)
        for loc, ids in fresh.items():
            state = dict(states.get(f"read-{loc}") or self._fresh_state(self._clock()))
            state["seen"] = _seen_list({int(i) for i in state.get("seen", ())} | ids)
            page.writes.append((self._state_path(f"read-{loc}", agent), state))
        return page

    def _annotate(self, page, agent):
        """Add subscription and unread counts to the threads that shown messages started."""
        for m in page.messages:
            info = m.get("thread")
            if not info:
                continue
            info["subscribed"] = info["id"] in self._own_threads(agent)
            info["new"] = info["more_new"] = None
            if info["subscribed"]:
                try:
                    info["new"], info["more_new"] = self._thread_unread(info["id"], agent)
                except BoardError as err:
                    page.warnings.append(f"thread {info['id']}: could not count unread: {err}")
        return page

    def _thread_unread(self, thread_id, agent):
        state = self._load(f"read-{thread_id}", agent) or self._fresh_state(self._clock())
        batch = self.client.get_messages(thread_id, after=str(_position(state, thread_id)), limit=FETCH_LIMIT)
        states = {f"read-{thread_id}": state}
        count = sum(1 for msg in batch
                    if msg.get("type", 0) in CONTENT_MESSAGE_TYPES and not _own_post(msg, agent)
                    and not self._was_read(agent, thread_id, int(msg["id"]),
                                           self._addressed(msg, thread_id, agent), states))
        return count, len(batch) >= FETCH_LIMIT

    # -- post ----------------------------------------------------------------

    def post(self, text, *, agent, thread_id=None, new_thread=None):
        if new_thread is not None:
            new_thread = " ".join(new_thread.split())
            if thread_id:
                raise BoardError("pass --thread or --new-thread, not both")
            if not 1 <= len(new_thread) <= THREAD_NAME_CHARS:
                raise BoardError(f"a thread name needs 1 to {THREAD_NAME_CHARS} characters")
        message = self.client.post(text, agent=agent, thread_id=thread_id)
        location = self.client.get_channel(thread_id) if thread_id else {"id": self.config.channel_id}
        result = self._normalize(message, location, None)
        try:
            self._remember_thread(agent, thread_id or result["id"], result["id"])
        except OSError:
            pass
        if new_thread is not None:
            try:
                thread = self.client.start_thread(result["id"], new_thread)
            except BoardError as err:
                raise ThreadNotStarted(
                    f"posted {result['url']}, but the thread was not opened: {err}", result) from None
            result["started_thread"] = {
                "id": str(thread["id"]), "name": thread.get("name"),
                "url": f"https://discord.com/channels/{self.config.guild_id}/{thread['id']}"}
        return result


def _seen_list(ids):
    return [str(i) for i in sorted(ids)[-MAX_SEEN:]]


def _check_limit(limit):
    if limit < 1:
        raise BoardError("limit must be at least 1")
