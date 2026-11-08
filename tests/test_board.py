"""Offline seam tests for the Discord board client, board, and CLI."""

import http.client
import importlib.util
import io
import json
import socket
import subprocess
import sys
import time
import urllib.error
from email.utils import formatdate
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "discord-agent-board" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import discord_agent_board as db  # noqa: E402

GUILD = "1111111111111111111"
BOARD = "1000"
OTHER = "2000"
BOT_TOKEN = "bot-secret-token-123"
HOOK_ID, HOOK_TOKEN = "9000", "hook-secret-token-456"
WEBHOOK_URL = f"https://discord.com/api/webhooks/{HOOK_ID}/{HOOK_TOKEN}"
SETUP_HOST = "dash.example"
SETUP_CODE = "setup-code-789"
SETUP_URL = f"https://{SETUP_HOST}/setup?code={SETUP_CODE}"


def sf(minutes_ago, seq=0):
    ms = int((time.time() - minutes_ago * 60) * 1000)
    return str(((ms - db.DISCORD_EPOCH_MS) << 22) + seq)


def iso(minutes_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - minutes_ago * 60))


class FakeResponse:
    def __init__(self, status, payload, now):
        self.status = status
        self.headers = {"Date": formatdate(now, usegmt=True)}
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDiscord:
    """Synthetic Discord REST: a guild, a board channel, threads, and a webhook."""

    def __init__(self):
        self.channels = {
            BOARD: {"id": BOARD, "type": 0, "guild_id": GUILD, "name": "agents"},
            OTHER: {"id": OTHER, "type": 0, "guild_id": GUILD, "name": "general"},
            "3000": {"id": "3000", "type": 0, "guild_id": "999", "name": "elsewhere"},
        }
        self.messages = {BOARD: [], OTHER: []}
        self.webhook = {"id": HOOK_ID, "channel_id": BOARD, "guild_id": GUILD}
        self.requests = []
        self.failures = {}
        self.threads_allowed = True
        self.clock = time.time
        self.setup = {"channel_id": BOARD, "bot_token": BOT_TOKEN, "webhook_url": WEBHOOK_URL}

    def thread(self, tid, name, *, parent=BOARD, private=False, archived_ago=None):
        meta = {"archived": archived_ago is not None}
        if archived_ago is not None:
            meta["archive_timestamp"] = iso(archived_ago)
        self.channels[tid] = {
            "id": tid, "type": 12 if private else 11, "guild_id": GUILD,
            "parent_id": parent, "name": name, "thread_metadata": meta,
        }
        self.messages[tid] = []

    def message(self, channel, mid, content, *, author="ana", webhook=False):
        msg = {
            "id": mid, "channel_id": channel, "type": 0, "content": content,
            "timestamp": "2026-11-07T12:00:00+00:00",
            "author": {"id": "1", "username": author, "global_name": None},
        }
        if webhook:
            msg["webhook_id"] = HOOK_ID
            msg["author"]["bot"] = True
        self.messages[channel].append(msg)
        return msg

    def fail(self, method, path, outcome):
        self.failures.setdefault((method, path), []).append(outcome)

    def open(self, request, timeout=None):
        assert timeout, "every request needs a timeout"
        url = urlsplit(request.full_url)
        if url.netloc == SETUP_HOST:
            self.requests.append({"method": request.get_method(), "path": url.path,
                                  "headers": dict(request.header_items())})
            if isinstance(self.setup, int):
                raise urllib.error.HTTPError(
                    request.full_url, self.setup, "error", {}, io.BytesIO(b'{"error": "login required"}'))
            return FakeResponse(200, self.setup, self.clock())
        method = request.get_method()
        body = json.loads(request.data) if request.data else None
        self.requests.append({
            "method": method, "path": url.path, "query": parse_qs(url.query),
            "headers": dict(request.header_items()), "body": body,
        })
        queued = self.failures.get((method, url.path))
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, payload = outcome
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {"Retry-After": "0"},
                io.BytesIO(json.dumps(payload).encode()))
        status, payload = self.route(method, url.path, parse_qs(url.query), body)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, io.BytesIO(json.dumps(payload).encode()))
        return FakeResponse(status, payload, self.clock())

    def route(self, method, path, query, body):
        q = {k: v[0] for k, v in query.items()}
        parts = path.split("/")[3:]  # after /api/v10
        if parts[:1] == ["webhooks"]:
            if parts[1:] != [HOOK_ID, HOOK_TOKEN]:
                return 401, {"message": "Invalid Webhook Token"}
            if method == "GET":
                return 200, dict(self.webhook)
            assert q.get("wait") == "true"
            target = q.get("thread_id", self.webhook["channel_id"])
            msg = self.message(target, sf(0, len(self.requests)), body["content"],
                               author=body["username"], webhook=True)
            return 200, msg
        if method == "POST" and parts[2:3] == ["messages"] and parts[4:] == ["threads"]:
            if not self.threads_allowed:
                return 403, {"message": "Missing Permissions"}
            cid, mid = parts[1], parts[3]
            self.thread(mid, body["name"], parent=cid)
            return 201, self.channels[mid]
        assert method == "GET", f"unexpected {method} {path}"
        if parts[0] == "guilds" and parts[2:] == ["threads", "active"]:
            active = [c for c in self.channels.values()
                      if c["type"] in (11, 12) and not c["thread_metadata"]["archived"]]
            return 200, {"threads": active, "members": []}
        cid = parts[1]
        if cid not in self.channels:
            return 404, {"message": "Unknown Channel"}
        if len(parts) == 2:
            channel = self.channels[cid]
            if channel["type"] == 11:
                channel = dict(channel, message_count=len(self.messages[cid]))
            return 200, channel
        if parts[2:] == ["threads", "archived", "public"]:
            rows = sorted(
                (c for c in self.channels.values() if c["type"] == 11
                 and c.get("parent_id") == cid and c["thread_metadata"]["archived"]),
                key=lambda c: c["thread_metadata"]["archive_timestamp"], reverse=True)
            if "before" in q:
                rows = [c for c in rows if c["thread_metadata"]["archive_timestamp"] < q["before"]]
            limit = int(q.get("limit", 50))
            return 200, {"threads": rows[:limit], "members": [], "has_more": len(rows) > limit}
        msgs = sorted((self.with_thread(m) for m in self.messages[cid]), key=lambda m: int(m["id"]))
        if len(parts) == 4:
            hit = [m for m in msgs if m["id"] == parts[3]]
            return (200, hit[0]) if hit else (404, {"message": "Unknown Message"})
        limit = int(q.get("limit", 50))
        if "after" in q:
            page = [m for m in msgs if int(m["id"]) > int(q["after"])][:limit]
        else:
            if "before" in q:
                msgs = [m for m in msgs if int(m["id"]) < int(q["before"])]
            page = msgs[-limit:]
        return 200, list(reversed(page))

    def with_thread(self, msg):
        thread = self.channels.get(msg["id"])
        if not thread or thread.get("parent_id") != msg["channel_id"]:
            return msg
        return dict(msg, thread=dict(thread, message_count=len(self.messages[msg["id"]])))

    def sent(self, method=None):
        return [r for r in self.requests if method is None or r["method"] == method]


@pytest.fixture
def fake():
    return FakeDiscord()


@pytest.fixture
def config(tmp_path):
    return db.BoardConfig(
        guild_id=GUILD, channel_id=BOARD, agent="tyler/api-refactor",
        bot_token=BOT_TOKEN, webhook_url=WEBHOOK_URL, state_dir=tmp_path / "state")


@pytest.fixture
def client(config, fake):
    return db.DiscordClient(config, opener=fake, sleep=lambda s: None)


# --- config ---------------------------------------------------------------


@pytest.mark.parametrize("files, env, expected", [
    ({}, {"DISCORD_BOARD_BOT_TOKEN": "env-bot"}, "env-bot"),
    ({"creds": {"bot_token": "file-bot"}}, {"DISCORD_BOT_TOKEN": "generic"}, "file-bot"),
    ({"mcp": {"botToken": "mcp-bot"}}, {"DISCORD_BOT_TOKEN": "generic"}, "generic"),
    ({"mcp": {"botToken": "mcp-bot"}}, {}, "mcp-bot"),
    ({"mcp": {"token": "user-account-token"}}, {}, None),
])
def test_load_config_sources(tmp_path, files, env, expected):
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "discord-agent-board"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps(
        {"channel_id": "file-channel", "agent": "file-agent", "guild_id": GUILD}))
    if "creds" in files:
        (cfg_dir / "credentials.json").write_text(
            json.dumps(files["creds"]), encoding="utf-8-sig")
    if "mcp" in files:
        (home / ".config" / "mcp-discord").mkdir(parents=True)
        (home / ".config" / "mcp-discord" / "config.json").write_text(json.dumps(files["mcp"]))
    env = dict(env, HOME=str(home), DISCORD_BOARD_CHANNEL_ID="env-channel")

    cfg = db.load_config({"agent": "cli-agent"}, env=env)

    assert (cfg.guild_id, cfg.channel_id, cfg.agent) == (GUILD, "env-channel", "cli-agent")
    assert cfg.bot_token == expected
    assert BOT_TOKEN not in repr(cfg)


# --- client ---------------------------------------------------------------


@pytest.mark.parametrize("where", ["channel", "thread"])
def test_reads_use_bot_auth_and_return_raw_messages(fake, client, where):
    fake.thread("5000", "plan")
    cid = BOARD if where == "channel" else "5000"
    fake.message(cid, sf(5), "hello")

    msgs = client.get_messages(cid, after="0", limit=100)

    assert [m["content"] for m in msgs] == ["hello"]
    req = fake.sent()[-1]
    assert req["path"] == f"/api/v10/channels/{cid}/messages"
    assert req["query"] == {"after": ["0"], "limit": ["100"]}
    assert req["headers"]["Authorization"] == f"Bot {BOT_TOKEN}"
    assert req["headers"]["User-agent"].startswith("DiscordBot (")


def test_thread_discovery_filters_and_paginates(fake, client):
    fake.thread("5001", "mine")
    fake.thread("5002", "elsewhere", parent=OTHER)
    fake.thread("5003", "secret", private=True)
    for i in range(3):
        fake.thread(str(6000 + i), f"old{i}", archived_ago=10 + i)

    assert [t["id"] for t in client.active_threads()] == ["5001"]
    first = client.archived_threads(limit=2)
    assert [t["id"] for t in first["threads"]] == ["6000", "6001"] and first["has_more"]
    before = first["threads"][-1]["thread_metadata"]["archive_timestamp"]
    rest = client.archived_threads(before=before, limit=2)
    assert [t["id"] for t in rest["threads"]] == ["6002"] and not rest["has_more"]


@pytest.mark.parametrize("target", ["other-channel", "other-guild", "private-thread", "foreign-thread"])
def test_scope_is_enforced_for_reads_and_posts(fake, client, target):
    fake.thread("5003", "secret", private=True)
    fake.thread("5004", "theirs", parent=OTHER)
    cid = {"other-channel": OTHER, "other-guild": "3000",
           "private-thread": "5003", "foreign-thread": "5004"}[target]

    with pytest.raises(db.ScopeError):
        client.get_messages(cid, limit=10)
    with pytest.raises(db.ScopeError):
        client.post("hi", agent="tyler/api-refactor", thread_id=cid)
    assert not any("/messages" in r["path"] for r in fake.sent()) and not fake.sent("POST")


def test_board_channel_must_be_text_channel_in_guild(fake, config):
    fake.channels[BOARD]["guild_id"] = "999"
    with pytest.raises(db.ScopeError):
        db.DiscordClient(config, opener=fake).get_messages(BOARD, limit=1)


@pytest.mark.parametrize("thread", [None, "5000"])
def test_post_is_confirmed_and_labelled(fake, client, thread):
    fake.thread("5000", "plan")

    msg = client.post("ping @ana", agent="tyler/api-refactor", thread_id=thread)

    post = fake.sent("POST")[0]
    assert post["body"] == {"content": "ping @ana", "username": "tyler/api-refactor",
                            "allowed_mentions": {"parse": []}}
    assert post["query"]["wait"] == ["true"]
    assert post["query"].get("thread_id") == ([thread] if thread else None)
    assert msg["channel_id"] == (thread or BOARD) and msg["id"]


@pytest.mark.parametrize("text,users", [
    ("blocked on <@123456789012345678> for tags", ["123456789012345678"]),
    ("<@!123456789012345678> and <@123456789012345678>", ["123456789012345678"]),
    ("ping @ana, @everyone and <@&123456789012345678>", []),
])
def test_post_allows_only_user_ids_written_in_the_text(fake, client, text, users):
    client.post(text, agent="tyler/api-refactor")

    body = fake.sent("POST")[0]["body"]
    assert body["content"] == text
    assert body["allowed_mentions"] == ({"parse": [], "users": users} if users
                                        else {"parse": []})


@pytest.mark.parametrize("problem", ["empty", "too-long", "webhook-elsewhere"])
def test_post_refuses_before_sending(fake, client, problem):
    text = {"empty": "  ", "too-long": "x" * 2001}.get(problem, "hi")
    if problem == "webhook-elsewhere":
        fake.webhook["channel_id"] = OTHER
    with pytest.raises(db.BoardError):
        client.post(text, agent="tyler/api-refactor")
    assert not fake.sent("POST")


def test_rate_limits_are_retried_then_bounded(fake, config):
    waits = []
    client = db.DiscordClient(config, opener=fake, sleep=waits.append)
    path = f"/api/v10/channels/{BOARD}/messages"
    fake.fail("GET", path, (429, {"retry_after": 0.25, "global": False}))
    assert client.get_messages(BOARD, limit=1) == []
    assert waits == [0.25]

    for _ in range(10):
        fake.fail("GET", path, (429, {"retry_after": 0.25}))
    with pytest.raises(db.BoardError, match="rate limit"):
        client.get_messages(BOARD, limit=1)
    assert len(waits) <= 1 + db.MAX_RATE_LIMIT_RETRIES


@pytest.mark.parametrize("outcome", [
    socket.timeout("timed out"),
    http.client.IncompleteRead(b"partial response"),
    (502, {"message": "Bad Gateway"}),
])
def test_ambiguous_post_failure_is_not_retried(fake, client, outcome):
    fake.fail("POST", f"/api/v10/webhooks/{HOOK_ID}/{HOOK_TOKEN}", outcome)
    with pytest.raises(db.DeliveryUncertain) as err:
        client.post("hi", agent="tyler/api-refactor")
    assert len(fake.sent("POST")) == 1
    assert "may have been posted" in str(err.value)
    assert HOOK_TOKEN not in str(err.value)


def test_errors_redact_credentials(fake, client):
    fake.fail("GET", f"/api/v10/channels/{BOARD}",
              (403, {"message": f"denied for Bot {BOT_TOKEN} via {WEBHOOK_URL}"}))
    with pytest.raises(db.BoardError) as err:
        client.get_messages(BOARD, limit=1)
    text = str(err.value)
    assert "403" in text and BOT_TOKEN not in text and HOOK_TOKEN not in text


# --- board: polls, reading positions, reads --------------------------------

ME, PEER = "tyler/api-refactor", "ana/search-index"


class Clock:
    def __init__(self, minutes_ago):
        self.at = time.time() - minutes_ago * 60

    def __call__(self):
        return self.at


def board_for(client, config, minutes_ago=0):
    clock = Clock(minutes_ago)
    return db.Board(client, config, clock=clock), clock


def view(board, where, agent, limit=50):
    if where == "inbox":
        return board.inbox(agent=agent, limit=limit)
    return board.read(None if where == BOARD else where, agent=agent, limit=limit)


def drain(board, where, agent, limit=50):
    seen = []
    for _ in range(20):
        page = view(board, where, agent, limit)
        assert page.coverage_complete, page.warnings
        seen += [m["id"] for m in page.messages]
        board.commit(page)
        if not page.has_more:
            return seen
    raise AssertionError("poll never finished")


@pytest.mark.parametrize("webhook", [False, True])
@pytest.mark.parametrize("where", [BOARD, "5000"])
def test_poll_surfaces_messages_that_name_the_handle(fake, client, config, webhook, where):
    fake.thread("5000", "plan")
    cases = [
        (f"@{ME} please look", True), (f"{ME} please look", True), (f"cc {ME}.", True),
        (f"Re: {ME.upper()}, thanks", True), (f"@{ME}-extra hi", False), (f"{ME}-extra hi", False),
        (f"v1.{ME}", False), (f"see https://example.com/{ME}/run", False), (f"({ME}) ok?", True), ("ünïcode, no tag", False),
    ]
    ids = [fake.message(where, sf(30 - i), text, webhook=webhook)["id"] for i, (text, _) in enumerate(cases)]
    board, _ = board_for(client, config)

    mentions = board.inbox(agent=ME)
    check = view(board, where, ME)

    wanted = [mid for mid, (_, hit) in zip(ids, cases) if hit]
    assert [m["id"] for m in mentions.messages] == wanted
    assert [(m["id"], m["mentioned"]) for m in check.messages] == [
        (mid, hit) for mid, (_, hit) in zip(ids, cases)]
    msg = check.messages[0]
    assert msg["url"] == f"https://discord.com/channels/{GUILD}/{where}/{ids[0]}"
    assert msg["thread_id"] == (None if where == BOARD else "5000")
    assert msg["author"] == "ana"


@pytest.mark.parametrize("handle", ["ana", "ana/", "/ana", "a b/c", "x" * 81 + "/y"])
def test_handles_must_be_name_slash_work(fake, client, config, handle):
    board, _ = board_for(client, config)
    with pytest.raises(db.BoardError, match="handle"):
        board.inbox(agent=handle)


SESSION_A, SESSION_B = "tyler/docs-site.aaaa", "tyler/docs-site.bbbb"


def test_each_agent_session_gets_its_own_handle(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "discord-agent-board").mkdir(parents=True)
    (home / ".config" / "discord-agent-board" / "config.json").write_text(json.dumps({"person": "tyler"}))
    cwd = checkout(tmp_path)

    def handle(overrides=None, **env):
        return db.load_config(overrides or {}, env={"HOME": str(home), **env}, cwd=cwd).agent

    a, b = handle(CLAUDE_CODE_SESSION_ID="s-1"), handle(CLAUDE_CODE_SESSION_ID="s-2")
    assert a == handle(CLAUDE_CODE_SESSION_ID="s-1") and a != b
    assert db.handle_family(a) == db.handle_family(b) == "tyler/docs-site" and a != "tyler/docs-site"
    assert handle(DISCORD_BOARD_SESSION="s-2", CLAUDE_CODE_SESSION_ID="s-1") == b
    assert handle() == "tyler/docs-site"
    assert handle({"agent": "ana/x"}, CLAUDE_CODE_SESSION_ID="s-1") == "ana/x"


def test_sessions_of_one_work_share_its_name_but_not_read_state(fake, client, config):
    started = fake.message(BOARD, sf(40), "tyler/docs-site: starting", author=SESSION_A, webhook=True)["id"]
    to_b = fake.message(BOARD, sf(39), f"{SESSION_B} can you take the eval?", author=SESSION_A,
                        webhook=True)["id"]
    to_work = fake.message(BOARD, sf(38), "tyler/docs-site please look")["id"]
    to_a = fake.message(BOARD, sf(37), f"{SESSION_A} only you")["id"]
    board, _ = board_for(client, config)

    first_a = board.inbox(agent=SESSION_A)
    board.commit(first_a)
    assert [m["id"] for m in first_a.messages] == [to_work, to_a]
    assert board.inbox(agent=SESSION_A).messages == []
    assert [m["id"] for m in board.inbox(agent=SESSION_B).messages] == [started, to_b, to_work]


@pytest.mark.parametrize("first", ["read", "recent", "inbox", "context"])
def test_a_message_any_view_showed_is_not_shown_again(fake, client, config, first):
    fake.thread("5000", "plan")
    tagged = fake.message(BOARD, sf(30), f"{ME} rerun the build?")["id"]
    chatter = fake.message(BOARD, sf(29), "lunch?")["id"]
    in_thread = fake.message("5000", sf(28), f"{ME} and the eval?")["id"]
    board, _ = board_for(client, config)
    if first == "read":
        page = board.read(agent=ME)
    elif first == "recent":
        page = board.read(agent=ME, recent=True)
    elif first == "context":
        page = board.read(f"https://discord.com/channels/{GUILD}/{BOARD}/{tagged}", agent=ME)
    else:
        page = board.inbox(agent=ME)
    board.commit(page)
    shown = {m["id"] for m in page.messages}

    again = drain(board, "inbox", ME) + drain(board, BOARD, ME) + drain(board, "5000", ME)

    assert sorted(again) == sorted({tagged, chatter, in_thread} - shown)
    assert not any(m["new"] for m in board.read(agent=ME, recent=True).messages)


def test_a_reply_to_the_agents_post_is_a_mention(fake, client, config):
    fake.message(BOARD, sf(50), "status of the migration?", author=ME, webhook=True)
    reply = fake.message(BOARD, sf(49), "all good, thanks")
    reply.update(type=19, referenced_message={"webhook_id": HOOK_ID, "author": {"username": ME}})
    fake.message(BOARD, sf(48), "unrelated chatter")
    board, _ = board_for(client, config)

    assert [m["id"] for m in board.inbox(agent=ME).messages] == [reply["id"]]


@pytest.mark.parametrize("how", ["started", "posted-in", "opened-on-post"])
def test_follow_ups_in_the_agents_threads_are_mentions(fake, client, config, how):
    fake.thread("5000", "plan")
    fake.thread("6000", "unrelated")
    board, clock = board_for(client, config, minutes_ago=60)
    if how == "started":
        thread = board.post("starting the migration", agent=ME, new_thread="Migration")["started_thread"]["id"]
    elif how == "posted-in":
        thread = "5000"
        fake.message(thread, sf(90), "before the agent joined")
        board.post("I can take this", agent=ME, thread_id=thread)
    else:
        thread = board.post("starting the migration", agent=ME)["id"]
        fake.thread(thread, "Migration")
    board = db.Board(client, config, clock=clock)
    follow_up = fake.message(thread, sf(-1), "how long will it take?")["id"]
    fake.message("6000", sf(-1), "unrelated chatter")

    page = board.inbox(agent=ME)

    assert [m["id"] for m in page.messages] == [follow_up] and page.messages[0]["mentioned"]
    assert board.inbox(agent=PEER).messages == []


def test_thread_starters_show_their_size_and_unread_count_only_when_followed(fake, client, config):
    board, clock = board_for(client, config, minutes_ago=60)
    mine = board.post("starting the rebuild", agent=ME, new_thread="Rebuild")["started_thread"]["id"]
    for i in range(2):
        fake.message(mine, sf(-1, i), f"question {i}")
    theirs = fake.message(BOARD, sf(10), "someone else's topic")["id"]
    fake.thread(theirs, "Theirs")
    fake.message(theirs, sf(9), "a reply")
    board = db.Board(client, config, clock=clock)

    threads = {m["thread"]["name"]: m["thread"] for m in board.read(agent=ME, recent=True).messages
               if m["thread"]}
    assert threads["Rebuild"] | {"id": mine} == {
        "id": mine, "name": "Rebuild", "message_count": 2, "subscribed": True, "new": 2, "more_new": False}
    assert threads["Theirs"]["message_count"] == 1 and threads["Theirs"]["new"] is None

    page = board.read(mine, agent=ME)
    board.commit(page)
    assert page.location["subscribed"] and len(page.messages) == 2
    after = [m["thread"] for m in board.read(agent=ME, recent=True).messages if m["thread"]]
    assert [t["new"] for t in after if t["id"] == mine] == [0]


def test_an_old_message_shown_by_recent_stays_read_after_the_view_catches_up(fake, client, config):
    early, _ = board_for(client, config, minutes_ago=60 * 24 * 10)
    early.commit(early.inbox(agent=ME))
    old = fake.message(BOARD, sf(60 * 24 * 5), f"{ME} still around?")["id"]
    board, _ = board_for(client, config)

    board.commit(board.read(agent=ME, recent=True))
    board.commit(board.read(agent=ME))

    assert drain(board, "inbox", ME) == [] and old not in drain(board, BOARD, ME)


@pytest.mark.parametrize("person, folder, expected", [
    ("tyler", "discord-board-setup-cmd", "tyler/dc-board-setup-cmd"),
    ("Clyde", "docs-site", "cl/docs-site"),
])
def test_default_handles_avoid_the_words_discord_rejects(tmp_path, person, folder, expected):
    home = tmp_path / "home"
    (home / ".config" / "discord-agent-board").mkdir(parents=True)
    (home / ".config" / "discord-agent-board" / "config.json").write_text(json.dumps({"person": person}))

    assert db.load_config({}, env={"HOME": str(home)}, cwd=checkout(tmp_path, folder)).agent == expected


@pytest.mark.parametrize("handle", ["tyler/discord-triage", "Tyler/CLYDE-x"])
def test_handles_discord_would_reject_are_refused_before_any_request(fake, client, handle):
    with pytest.raises(db.BoardError, match="does not allow"):
        client.post("hi", agent=handle)
    assert not fake.sent()


def test_a_handle_does_not_see_its_own_posts_as_mentions(fake, client, config):
    own = fake.message(BOARD, sf(30), f"{ME}: starting the migration", author=ME, webhook=True)["id"]
    peer = fake.message(BOARD, sf(29), f"{PEER}: waiting on {ME}", author=PEER, webhook=True)["id"]
    board, _ = board_for(client, config)

    page = board.inbox(agent=ME)

    assert [m["id"] for m in page.messages] == [peer] and own not in [m["id"] for m in page.messages]


def test_fast_local_clock_does_not_skip_messages_between_polls(fake, client, config):
    server_clock = Clock(0)
    fake.clock = server_clock
    board, local_clock = board_for(client, config)
    local_clock.at = server_clock.at + 120
    assert drain(board, BOARD, ME) == []

    mid = fake.message(BOARD, str(db.snowflake_at(server_clock.at)), "new message")["id"]
    server_clock.at += 60
    local_clock.at += 60

    assert drain(board, BOARD, ME) == [mid]


def ago(clock, seconds):
    return str(db.snowflake_at(clock.at - seconds))


def live_board(fake, client, config):
    board, clock = board_for(client, config)
    fake.clock = clock
    return board, clock


@pytest.mark.parametrize("mode", [BOARD, "inbox"])
def test_a_message_posted_seconds_ago_shows_at_once_and_only_once(fake, client, config, mode):
    board, clock = live_board(fake, client, config)
    assert drain(board, mode, ME) == []
    clock.at += 5
    mid = fake.message(BOARD, ago(clock, 2), f"{PEER}: {ME} ping", author=PEER, webhook=True)["id"]

    assert drain(board, mode, ME) == [mid]
    clock.at += 5
    assert drain(board, mode, ME) == []
    clock.at += 60
    assert drain(board, mode, ME) == []


def test_a_message_stamped_after_the_servers_whole_second_clock_still_shows(fake, client, config):
    board, clock = live_board(fake, client, config)
    assert drain(board, BOARD, ME) == []
    mid = fake.message(BOARD, ago(clock, -0.9), "same second as the poll")["id"]

    assert drain(board, BOARD, ME) == [mid]


def test_a_message_that_becomes_visible_late_is_still_found(fake, client, config):
    board, clock = live_board(fake, client, config)
    assert drain(board, BOARD, ME) == []
    clock.at += 20
    recent = fake.message(BOARD, ago(clock, 3), "visible first")["id"]
    assert drain(board, BOARD, ME) == [recent]

    clock.at += 5
    late = fake.message(BOARD, ago(clock, 12), "older id, indexed late")["id"]
    assert drain(board, BOARD, ME) == [late]


def test_a_late_message_is_found_after_a_page_cut_inside_the_recent_window(fake, client, config):
    board, clock = live_board(fake, client, config)
    assert drain(board, BOARD, ME) == []
    clock.at += 20
    first, second, third = (fake.message(BOARD, ago(clock, s), f"m{s}")["id"] for s in (10, 8, 6))

    page = view(board, BOARD, ME, limit=2)
    board.commit(page)
    assert [m["id"] for m in page.messages] == [first, second] and page.has_more
    late = fake.message(BOARD, ago(clock, 9), "older id, indexed late")["id"]

    assert drain(board, BOARD, ME) == [late, third]


def test_a_mention_read_seconds_after_it_was_posted_stays_out_of_the_inbox(fake, client, config):
    board, clock = live_board(fake, client, config)
    assert drain(board, BOARD, ME) == [] and drain(board, "inbox", ME) == []
    clock.at += 20
    fake.message(BOARD, ago(clock, 15), f"{ME} rerun?")
    assert len(drain(board, BOARD, ME)) == 1

    assert drain(board, "inbox", ME) == []


def test_read_positions_are_isolated(fake, client, config):
    mine = fake.message(BOARD, sf(30), f"@{ME} first")["id"]
    theirs = fake.message(BOARD, sf(29), f"@{PEER} second")["id"]
    board, _ = board_for(client, config)

    assert drain(board, "inbox", ME) == [mine]
    assert drain(board, "inbox", PEER) == [theirs]
    assert drain(board, BOARD, ME) == [theirs]
    assert drain(board, BOARD, PEER) == [mine]
    for mode, agent in [(BOARD, ME), ("inbox", ME), ("inbox", PEER), (BOARD, PEER)]:
        assert drain(board, mode, agent) == []


@pytest.mark.parametrize("location", ["activity", "archived-thread", "scan-cap"])
def test_poll_continuation_preserves_mentions(fake, client, config, location, monkeypatch):
    earlier, _ = board_for(client, config, minutes_ago=60)
    assert drain(earlier, "inbox", ME) == []

    if location == "scan-cap":
        monkeypatch.setattr(db, "FETCH_LIMIT", 2)
        monkeypatch.setattr(db, "MAX_PAGES_PER_LOCATION", 1)
    if location != "archived-thread":
        fake.thread("5000", "plan")
        places = [BOARD, "5000", BOARD, "5000", BOARD]
    else:
        fake.thread("6000", "closed", archived_ago=10)
        places = ["6000"] * 4 + [BOARD]
    expected = []
    for i, place in enumerate(places):
        fake.message(place, sf(30 - i, 1), "chatter")
        expected.append(fake.message(place, sf(30 - i, 2), f"@{ME} item {i}")["id"])
    board, _ = board_for(client, config)

    assert drain(board, "inbox", ME, limit=2) == expected
    assert drain(board, "inbox", ME, limit=2) == []


@pytest.mark.parametrize("failing", ["thread-messages", "active-discovery", "archived-discovery"])
def test_incomplete_poll_preserves_positions(fake, client, config, failing):
    fake.thread("5000", "plan")
    fake.thread("6000", "closed", archived_ago=5)
    tagged = [fake.message(c, sf(20 - i), f"@{ME} {i}")["id"] for i, c in enumerate([BOARD, "5000", "6000"])]
    path = {"thread-messages": "/api/v10/channels/5000/messages",
            "active-discovery": f"/api/v10/guilds/{GUILD}/threads/active",
            "archived-discovery": f"/api/v10/channels/{BOARD}/threads/archived/public"}[failing]
    fake.fail("GET", path, (403, {"message": "Missing Access"}))
    board, _ = board_for(client, config)

    page = board.inbox(agent=ME)
    assert not page.coverage_complete and page.warnings
    board.commit(page)
    assert not list(config.state_dir.glob("*.json"))
    assert drain(board, "inbox", ME) == tagged


def test_first_use_reports_cutoff_and_older_history_pages_with_before(fake, client, config):
    old = [fake.message(BOARD, sf(60 * 24 * 5 + i), f"@{ME} old {i}")["id"] for i in (2, 1, 0)]
    new = fake.message(BOARD, sf(5), f"@{ME} new")["id"]
    board, _ = board_for(client, config)

    page = board.inbox(agent=ME)
    assert [m["id"] for m in page.messages] == [new] and page.coverage_complete and page.notes
    board.commit(page)

    history = board.read(agent=ME, limit=2, before=new)
    assert [m["id"] for m in history.messages] == old[1:] and history.has_more
    older = board.read(agent=ME, limit=2, before=history.continuation[1])
    assert [m["id"] for m in older.messages] == old[:1] and not older.has_more
    assert drain(board, "inbox", ME) == []


def test_read_message_context_and_thread_history(fake, client, config):
    fake.thread("5000", "plan")
    chan = [fake.message(BOARD, sf(40 - i), f"chan {i}")["id"] for i in range(9)]
    starter = fake.message(BOARD, "5000", "starting a thread")["id"]
    replies = [fake.message("5000", sf(20 - i), f"reply {i}")["id"] for i in range(5)]
    board, _ = board_for(client, config)

    ctx = board.read(f"https://discord.com/channels/{GUILD}/{BOARD}/{chan[4]}", agent=ME, limit=5)
    assert [m["id"] for m in ctx.messages] == chan[2:7]

    latest = board.read(f"https://discord.com/channels/{GUILD}/5000", agent=ME, recent=True, limit=3)
    assert [m["id"] for m in latest.messages] == replies[2:] and latest.has_more
    assert latest.continuation[:2] == ["5000", "--before"]
    rest = board.read("5000", agent=ME, limit=3, before=latest.continuation[2])
    assert [m["id"] for m in rest.messages] == [starter] + replies[:2]
    assert not rest.has_more and all(m["thread_name"] == "plan" for m in latest.messages)


@pytest.mark.parametrize("target, error", [
    (f"https://discord.com/channels/999/{BOARD}/1", db.ScopeError),
    (f"https://discord.com/channels/{GUILD}/{OTHER}/1", db.ScopeError),
    ("https://example.com/channels/1/2/3", db.BoardError),
    ("thread-reply", db.BoardError),
])
def test_read_refuses_targets_outside_board(fake, client, config, target, error):
    fake.thread("5000", "plan")
    reply = fake.message("5000", sf(5), "in a thread")["id"]
    board, _ = board_for(client, config)
    with pytest.raises(error):
        board.read(reply if target == "thread-reply" else target, agent=ME)


# --- CLI ------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location(
    "board_cli", SCRIPTS / "board.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture
def run(fake, tmp_path, monkeypatch):
    env = {"HOME": str(tmp_path / "home"), "DISCORD_BOARD_CHANNEL_ID": BOARD, "DISCORD_BOARD_GUILD_ID": GUILD,
           "DISCORD_BOARD_BOT_TOKEN": BOT_TOKEN, "DISCORD_BOARD_WEBHOOK_URL": WEBHOOK_URL,
           "DISCORD_BOARD_STATE_DIR": str(tmp_path / "state")}

    def invoke(*argv):
        return cli.main(list(argv), env=env, opener=fake)
    return invoke


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_check_output(fake, run, capsys, as_json):
    msg = fake.message(BOARD, sf(5), f"Grüße @{ME} — ünïcode")
    argv = ["inbox", "--agent", ME] + (["--json"] if as_json else [])

    assert run(*argv) == 0
    out = capsys.readouterr().out
    url = f"https://discord.com/channels/{GUILD}/{BOARD}/{msg['id']}"
    if as_json:
        data = json.loads(out)
        assert data["messages"][0]["url"] == url and data["coverage_complete"]
        assert data["messages"][0]["content"] == msg["content"]
        assert "cursor" not in data and BOT_TOKEN not in out
    else:
        assert url in out and "Grüße" in out
    assert run(*argv) == 0
    again = capsys.readouterr().out
    assert (json.loads(again)["messages"] == []) if as_json else ("No new" in again)


@pytest.mark.parametrize("as_json", [False, True])
def test_cli_post_reports_confirmed_link(fake, run, capsys, as_json):
    assert run("post", "--agent", ME, "--text", "hello team", *(["--json"] if as_json else [])) == 0
    out = capsys.readouterr().out
    posted = fake.messages[BOARD][-1]
    assert f"https://discord.com/channels/{GUILD}/{BOARD}/{posted['id']}" in out
    assert posted["author"]["username"] == ME and HOOK_TOKEN not in out


@pytest.mark.parametrize("refused", [False, True])
def test_cli_new_thread_opens_a_thread_on_the_post(fake, run, capsys, refused):
    fake.threads_allowed = not refused
    status = run("post", "--agent", ME, "--new-thread", "Index  rebuild", "--text", "starting")

    captured = capsys.readouterr()
    posted = fake.messages[BOARD][-1]
    assert f"https://discord.com/channels/{GUILD}/{BOARD}/{posted['id']}" in captured.out
    thread_call = fake.sent("POST")[-1]
    assert thread_call["body"] == {"name": "Index rebuild"}
    assert thread_call["headers"]["Authorization"] == f"Bot {BOT_TOKEN}"
    if refused:
        assert status == 1 and "Create Public Threads" in captured.err
    else:
        assert status == 0 and fake.channels[posted["id"]]["parent_id"] == BOARD
        assert f"({posted['id']})" in captured.out


def test_cli_read_shows_threads_and_marks_what_was_already_read(fake, run, capsys):
    starter = fake.message(BOARD, sf(10), "topic")["id"]
    fake.thread(starter, "Topic")
    fake.message(starter, sf(9), "a reply in the thread")

    assert run("read", "--agent", ME) == 0
    first = capsys.readouterr().out
    assert f'-> thread "Topic" ({starter}): 1 message' in first and "a reply" not in first
    assert run("read", "--agent", ME) == 0
    assert "No new messages. `read --recent`" in capsys.readouterr().out
    assert run("read", "--agent", ME, "--recent") == 0
    assert "(new)" not in capsys.readouterr().out
    assert run("read", starter, "--agent", ME) == 0
    thread = capsys.readouterr().out
    assert thread.startswith(f'Thread "Topic" ({starter}), 1 message') and "a reply" in thread


def test_cli_failures_exit_nonzero_without_state(fake, run, capsys, tmp_path, monkeypatch):
    fake.message(BOARD, sf(5), f"@{ME} hi")
    fake.fail("GET", f"/api/v10/guilds/{GUILD}/threads/active", (403, {"message": "Missing Access"}))
    assert run("inbox", "--agent", ME) == 1
    assert "incomplete" in capsys.readouterr().err

    class Broken:
        def write(self, text):
            raise BrokenPipeError()

        def flush(self):
            raise BrokenPipeError()
    monkeypatch.setattr(cli.sys, "stdout", Broken())
    assert run("inbox", "--agent", ME) == 1
    monkeypatch.undo()
    assert not (tmp_path / "state").exists() or not list((tmp_path / "state").glob("*.json"))
    fake.fail("POST", f"/api/v10/webhooks/{HOOK_ID}/{HOOK_TOKEN}", socket.timeout("timed out"))
    assert run("post", "--agent", ME, "--text", "hi") == 1
    err = capsys.readouterr().err
    assert "may have been posted" in err and HOOK_TOKEN not in err


@pytest.fixture
def setup_run(fake, tmp_path):
    home = tmp_path / "setup-home"

    def invoke(*argv):
        return cli.main(["setup", *argv], env={"HOME": str(home)}, opener=fake)
    invoke.config_dir = home / ".config" / "discord-agent-board"
    return invoke


@pytest.mark.parametrize("webhook", [True, False])
def test_cli_setup_writes_config_the_board_loads(fake, setup_run, capsys, webhook):
    fake.setup["webhook_url"] = WEBHOOK_URL if webhook else None
    setup_run.config_dir.mkdir(parents=True)
    (setup_run.config_dir / "config.json").write_text(json.dumps({"guild_id": "stale", "color": "blue"}))

    assert setup_run("--url", SETUP_URL, "--agent", ME) == 0
    out = capsys.readouterr()
    assert all(secret not in out.out + out.err for secret in (BOT_TOKEN, HOOK_TOKEN, SETUP_CODE))
    assert f"Handle: {ME}" in out.out and ("Posting works" in out.out) == webhook

    cfg = db.load_config({}, env={"HOME": str(setup_run.config_dir.parents[1])})
    assert cfg.guild_id == GUILD
    assert (cfg.channel_id, cfg.agent, cfg.bot_token, cfg.webhook_url) == (
        BOARD, ME, BOT_TOKEN, WEBHOOK_URL if webhook else None)
    credentials = setup_run.config_dir / "credentials.json"
    if sys.platform != "win32":
        assert credentials.stat().st_mode & 0o077 == 0
    saved = json.loads((setup_run.config_dir / "config.json").read_text())
    assert (saved["guild_id"], saved["color"]) == (GUILD, "blue")


def checkout(tmp_path, name="docs-site"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


@pytest.mark.parametrize("person, user, agent, expected", [
    ("Tyler Ho", None, None, "tyler-ho/docs-site"),
    (None, "Ana", None, "ana/docs-site"),
    ("Tyler Ho", None, "ana/search-index", "ana/search-index"),
    (None, None, None, None),
])
def test_default_handle_is_the_person_and_the_checkout_folder(tmp_path, person, user, agent, expected):
    home = tmp_path / "home"
    (home / ".config" / "discord-agent-board").mkdir(parents=True)
    (home / ".config" / "discord-agent-board" / "config.json").write_text(
        json.dumps({"person": person} if person else {}))
    env = {"HOME": str(home), **({"USER": user} if user else {})}

    cfg = db.load_config({"agent": agent}, env=env, cwd=checkout(tmp_path) / "src")

    assert cfg.agent == expected


@pytest.fixture
def setup_run_token(fake, tmp_path, monkeypatch):
    monkeypatch.chdir(checkout(tmp_path))
    home = tmp_path / "setup-home"
    env = {"HOME": str(home), "DISCORD_BOARD_SETUP_URL": f"https://{SETUP_HOST}/setup",
           "DISCORD_BOARD_SETUP_TOKEN": "setup-token-xyz", "DISCORD_BOARD_STATE_DIR": str(tmp_path / "state")}
    fake.setup = {**fake.setup, "person": "tyler"}

    def invoke(*argv):
        return cli.main(list(argv), env=env, opener=fake)
    invoke.config_dir = home / ".config" / "discord-agent-board"
    return invoke


def test_cli_configures_itself_from_the_setup_token_on_first_use(fake, setup_run_token, capsys):
    fake.message(BOARD, sf(5), "@tyler/docs-site can you rerun the build?")

    assert setup_run_token("inbox") == 0
    first = capsys.readouterr()
    assert "rerun the build" in first.out and "configured the Discord board" in first.err
    assert "setup-token-xyz" not in first.out + first.err and BOT_TOKEN not in first.out + first.err
    setup = [r for r in fake.requests if r["path"] == "/setup"]
    assert [r["headers"]["Authorization"] for r in setup] == ["Bearer setup-token-xyz"]

    assert setup_run_token("inbox") == 0
    assert "configured the Discord board" not in capsys.readouterr().err
    assert len([r for r in fake.requests if r["path"] == "/setup"]) == 1


def test_cli_does_not_configure_from_a_refused_setup_token(fake, setup_run_token, capsys):
    fake.setup = 401

    assert setup_run_token("inbox") == 1
    err = capsys.readouterr().err
    assert "automatic setup" in err and "not set up on this machine" in err and "setup-token-xyz" not in err
    assert not setup_run_token.config_dir.exists()


def test_cli_setup_without_a_link_uses_the_setup_token(fake, setup_run_token, capsys):
    assert setup_run_token("setup") == 0
    out = capsys.readouterr().out
    assert "Handle: tyler/docs-site" in out and "Posting works" in out
    assert json.loads((setup_run_token.config_dir / "config.json").read_text())["person"] == "tyler"


@pytest.mark.parametrize("env, expected", [
    ({"DISCORD_BOARD_SETUP_URL": "https://board.example/setup", "DISCORD_BOARD_SETUP_TOKEN": "t"},
     ("https://board.example/setup", "t")),
    ({"DISCORD_BOARD_SETUP_URL": "http://board.example/setup", "DISCORD_BOARD_SETUP_TOKEN": "t"}, None),
    ({"DISCORD_BOARD_SETUP_URL": "https://board.example/setup"}, None),
    ({}, None),
])
def test_setup_target_needs_an_https_url_and_a_token(env, expected):
    assert db.setup_target(env) == expected


def write_manual_config(home, **config):
    folder = home / ".config" / "discord-agent-board"
    folder.mkdir(parents=True)
    (folder / "config.json").write_text(json.dumps(config))
    (folder / "credentials.json").write_text(json.dumps({"bot_token": BOT_TOKEN, "webhook_url": WEBHOOK_URL}))
    return folder


def test_hand_written_files_need_no_server_id_and_the_server_is_saved(fake, tmp_path, capsys):
    home = tmp_path / "home"
    folder = write_manual_config(home, channel_id=BOARD)
    fake.message(BOARD, sf(5), f"{ME} please look")

    assert cli.main(["inbox", "--agent", ME], env={"HOME": str(home)}, opener=fake) == 0
    assert "please look" in capsys.readouterr().out
    assert json.loads((folder / "config.json").read_text()) == {"channel_id": BOARD, "guild_id": GUILD}


def test_a_server_learned_from_an_environment_channel_is_not_saved(fake, tmp_path):
    env = {"HOME": str(tmp_path / "home"), "DISCORD_BOARD_CHANNEL_ID": BOARD,
           "DISCORD_BOARD_BOT_TOKEN": BOT_TOKEN}
    assert cli.main(["read", "--agent", ME], env=env, opener=fake) == 0
    assert not (tmp_path / "home" / ".config").exists()


def test_a_board_channel_outside_any_server_is_an_error(fake, tmp_path, capsys):
    write_manual_config(tmp_path / "home", channel_id=BOARD)
    fake.channels[BOARD]["guild_id"] = None
    assert cli.main(["read", "--agent", ME], env={"HOME": str(tmp_path / "home")}, opener=fake) == 1
    assert "not in a server" in capsys.readouterr().err


def test_cli_setup_without_a_link_or_setup_token_says_what_to_pass(fake, setup_run, capsys):
    assert setup_run() == 1
    assert "--url" in capsys.readouterr().err


def test_cli_setup_rejects_an_unusable_link_without_writing(fake, setup_run, capsys):
    cases = [
        (SETUP_URL.replace("https", "http"), None),
        (SETUP_URL, 401),
        (SETUP_URL, {"channel_id": None, "bot_token": None, "webhook_url": None}),
        (SETUP_URL, {**fake.setup, "webhook_url": "https://example.com/hook"}),
    ]
    original = fake.webhook["channel_id"]
    for url, outcome in cases:
        if outcome is not None:
            fake.setup = outcome
        assert setup_run("--url", url) == 1
    fake.setup = {"channel_id": BOARD, "bot_token": BOT_TOKEN, "webhook_url": WEBHOOK_URL}
    fake.webhook["channel_id"] = OTHER
    assert setup_run("--url", SETUP_URL) == 1
    fake.webhook["channel_id"] = original

    err = capsys.readouterr().err
    assert err.count("error:") == 5 and SETUP_CODE not in err and BOT_TOKEN not in err
    assert not setup_run.config_dir.exists()


# --- verification regressions ----------------------------------------------


def test_reading_positions_never_move_backwards_so_nothing_prints_twice(fake, client, config):
    fake.thread("5000", "plan")
    names = {}

    def say(name, where, minutes, text, **kw):
        names[fake.message(where, sf(minutes), text, **kw)["id"]] = name
    say("b1", BOARD, 60, f"{ME} x")
    say("own", BOARD, 59, "done", author=ME, webhook=True)
    for i, minutes in enumerate((58, 57, 56), start=2):
        say(f"b{i}", BOARD, minutes, f"{ME} b{i}")
    for i, minutes in enumerate((50, 40, 30), start=1):
        say(f"t{i}", "5000", minutes, f"{ME} t{i}")
    board, _ = board_for(client, config)
    printed = []
    for _ in range(10):
        page = board.inbox(agent=ME, limit=2)
        printed += [names[m["id"]] for m in page.messages]
        board.commit(page)
        if not page.has_more:
            break

    assert sorted(printed) == ["b1", "b2", "b3", "b4", "t1", "t2", "t3"]


def test_credentials_travel_as_unredirected_headers(fake, config):
    seen = []

    class Spy:
        def open(self, request, timeout=None):
            seen.append(request)
            return fake.open(request, timeout)
    db.DiscordClient(config, opener=Spy()).get_channel(BOARD)
    db.fetch_setup(SETUP_URL, bearer="setup-bearer", opener=Spy())

    assert len(seen) == 2
    for request, prefix in zip(seen, ("Bot ", "Bearer ")):
        assert "Authorization" not in request.headers
        assert request.unredirected_hdrs["Authorization"].startswith(prefix)


def test_the_default_opener_refuses_redirects():
    import http.server
    import threading
    import urllib.request
    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirect(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/x")
            self.end_headers()
    first = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    servers = [threading.Thread(target=srv.serve_forever, daemon=True) for srv in (target, first)]
    for thread in servers:
        thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{first.server_port}/a")
        request.add_header("Authorization", "Bearer secret")
        with pytest.raises(urllib.error.HTTPError) as err:
            db.default_opener().open(request, timeout=5)
    finally:
        for srv in (target, first):
            srv.shutdown()
            srv.server_close()
    assert err.value.code == 302 and hits == []


@pytest.mark.parametrize("folder", ["clydeyde", "cdiscordlyde", "DISCORDdiscord"])
def test_default_handles_never_contain_the_words_discord_rejects(tmp_path, folder):
    cfg = db.load_config({}, env={"HOME": str(tmp_path / "home"), "USER": "tyler"},
                         cwd=checkout(tmp_path, folder))
    assert db.validate_handle(cfg.agent) == cfg.agent and not db.RESERVED_RE.search(cfg.agent)


@pytest.mark.parametrize("status", [401, 500])
def test_setup_errors_never_show_the_link_or_its_credentials(status):
    class Refuse:
        def open(self, request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, status, "no", {}, io.BytesIO(b"{}"))
    with pytest.raises(db.BoardError) as err:
        db.fetch_setup("https://user:codeXYZ@board.example/setup/codeXYZ?c=codeXYZ", opener=Refuse())
    assert "codeXYZ" not in str(err.value) and "user" not in str(err.value)


def test_a_bare_webhook_token_is_redacted_from_errors(client):
    assert client.redact(f"Discord said {HOOK_TOKEN} is bad") == "Discord said [redacted] is bad"


class OddBody:
    """Delegates to the fake but answers one kind of request with a 200 and the given body."""

    def __init__(self, fake, method, fragment, body):
        self.fake, self.method, self.fragment, self.body = fake, method, fragment, body

    def open(self, request, timeout=None):
        if request.get_method() == self.method and self.fragment in request.full_url and (
                self.method != "GET" or "webhooks" not in request.full_url):
            return FakeResponse(200, self.body, time.time())
        return self.fake.open(request, timeout)


@pytest.mark.parametrize("body", [{}, None, [], "ok"])
def test_a_post_answered_with_an_odd_body_is_reported_as_possibly_posted(fake, config, body):
    client = db.DiscordClient(config, opener=OddBody(fake, "POST", "/webhooks/", body))
    with pytest.raises(db.DeliveryUncertain, match="may have been posted"):
        client.post("hi", agent=ME)


@pytest.mark.parametrize("body", [{"x": 1}, None, "oops"])
def test_a_messages_response_that_is_not_a_list_is_an_error_not_a_crash(fake, config, body):
    client = db.DiscordClient(config, opener=OddBody(fake, "GET", "/messages", body))
    with pytest.raises(db.BoardError, match="unexpected response"):
        client.get_messages(BOARD)


def test_a_negative_or_infinite_retry_after_does_not_crash(fake, config):
    waits = []
    client = db.DiscordClient(config, opener=fake, sleep=waits.append)
    path = f"/api/v10/channels/{BOARD}"
    fake.fail("GET", path, (429, {"retry_after": -1}))
    fake.fail("GET", path, (429, {"retry_after": float("inf")}))

    assert client.get_channel(BOARD)["id"] == BOARD
    assert waits == [0.0, 1.0]


def test_rate_limits_are_retried_three_times_and_no_more(fake, client):
    path = f"/api/v10/channels/{BOARD}"
    for _ in range(4):
        fake.fail("GET", path, (429, {"retry_after": 0}))
    with pytest.raises(db.BoardError, match="rate limit"):
        client.get_channel(BOARD)
    assert len([r for r in fake.sent() if r["path"] == path]) == 4


def test_a_non_ascii_digit_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as err:
        cli.main(["read", "--before", "\u00b2"], env={"HOME": str(tmp_path)})
    assert err.value.code == 2


def test_read_refuses_before_on_a_single_message(fake, client, config):
    msg = fake.message(BOARD, sf(5), "hello")
    board, _ = board_for(client, config)
    with pytest.raises(db.BoardError, match="--before"):
        board.read(f"https://discord.com/channels/{GUILD}/{BOARD}/{msg['id']}", agent=ME, before="5")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_setup_only_restricts_a_directory_it_created(fake, setup_run, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    project.chmod(0o755)

    assert setup_run("--url", SETUP_URL, "--config", str(project / "board.json")) == 0
    assert project.stat().st_mode & 0o777 == 0o755

    assert setup_run("--url", SETUP_URL) == 0
    assert setup_run.config_dir.stat().st_mode & 0o077 == 0


def test_an_invalid_handle_is_refused_before_any_request(fake, setup_run_token, capsys):
    assert setup_run_token("inbox", "--agent", "tyler/discord-x") == 1
    assert "does not allow" in capsys.readouterr().err
    assert fake.requests == []


def test_a_webhook_from_another_server_is_refused(fake, client):
    fake.webhook["guild_id"] = "999"
    with pytest.raises(db.ScopeError):
        client.post("hi", agent=ME)
    assert not fake.sent("POST")


def test_an_announcement_channel_cannot_be_the_board(fake, client):
    fake.channels[BOARD]["type"] = 5
    with pytest.raises(db.ScopeError):
        client.board_channel()


def test_system_messages_never_show_up(fake, client, config):
    system = fake.message(BOARD, sf(5), f"{ME} joined the server")
    system["type"] = 7
    board, _ = board_for(client, config)
    assert board.inbox(agent=ME).messages == [] and board.read(agent=ME).messages == []


def test_threads_archived_across_several_pages_are_all_discovered(fake, client, config, monkeypatch):
    monkeypatch.setattr(db, "FETCH_LIMIT", 2)
    for i, minutes in enumerate((10, 20, 30, 40, 50)):
        fake.thread(str(6000 + i), f"old-{i}", archived_ago=minutes)
    hit = fake.message("6004", sf(55), f"{ME} from the oldest archived thread")["id"]
    board, _ = board_for(client, config)
    assert [m["id"] for m in board.inbox(agent=ME).messages] == [hit]


# --- runtime --------------------------------------------------------------



def test_cli_runs_stdlib_only_from_anywhere(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin",
           "DISCORD_BOARD_STATE_DIR": str(tmp_path / "state")}
    probe = ("import sys; sys.path.insert(0, %r); import discord_agent_board; "
             "heavy = {'numpy', 'pandas', 'torch', 'scipy'} & set(sys.modules); "
             "assert not heavy, heavy" % str(SCRIPTS))
    for argv in (["-c", probe], [str(SCRIPTS / "board.py"), "--help"]):
        done = subprocess.run([sys.executable, "-S", *argv], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
    assert "inbox" in done.stdout and "post" in done.stdout
    assert not (tmp_path / "state").exists() and not (tmp_path / "home").exists()
