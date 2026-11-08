"""Recovery commands against the synthetic Discord REST seam."""

import argparse
import io
import socket

import pytest

from test_board import BOARD, GUILD, HOOK_ID, HOOK_TOKEN, WEBHOOK_URL, FakeDiscord
import board_actions as actions
import discord_agent_board as db


class RecoveryDiscord(FakeDiscord):
    def route(self, method, path, query, body):
        parts = path.split("/")[3:]
        if parts[:3] == ["webhooks", HOOK_ID, HOOK_TOKEN] and len(parts) == 5:
            assert parts[3] == "messages"
            channel = query.get("thread_id", [BOARD])[0]
            message = next((m for m in self.messages[channel] if m["id"] == parts[4]), None)
            if not message or message.get("webhook_id") != HOOK_ID:
                return 404, {"message": "Unknown Message"}
            if method == "PATCH":
                message["content"] = body["content"]
                return 200, dict(message)
            assert method == "DELETE"
            self.messages[channel].remove(message)
            return 204, None
        return super().route(method, path, query, body)


def invoke(fake, tmp_path, argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    actions.add_commands(sub, argparse.ArgumentParser(add_help=False))
    config = db.BoardConfig(GUILD, BOARD, "tyler/recovery", "bot-token", WEBHOOK_URL, tmp_path)
    return actions.run(parser.parse_args(argv), config, opener=fake)


@pytest.mark.parametrize("command,in_thread", [("edit", False), ("edit", True), ("delete", True)])
def test_webhook_mutation_and_mentions(tmp_path, monkeypatch, command, in_thread):
    fake = RecoveryDiscord()
    fake.thread("4000", "work")
    channel = "4000" if in_thread else BOARD
    fake.message(channel, "5000", "old", webhook=True)
    argv = [command, "5000"]
    if in_thread:
        argv += ["--thread", f"https://discord.com/channels/{GUILD}/4000"]
    content = "fixed <@123456789012345678> @everyone <@&234567890123456789>"
    monkeypatch.setattr(actions.sys, "stdin", io.StringIO(content + "\n"))
    result = invoke(fake, tmp_path, argv)
    assert result == {"action": command, "id": "5000", "channel_id": channel,
                      "url": f"https://discord.com/channels/{GUILD}/{channel}/5000",
                      **({"content": content} if command == "edit" else {"deleted": True})}
    request = fake.requests[-1]
    assert request["path"] == f"/api/v10/webhooks/{HOOK_ID}/{HOOK_TOKEN}/messages/5000"
    assert "Authorization" not in request["headers"]
    assert request["query"] == ({"thread_id": [channel]} if in_thread else {})
    if command == "edit":
        assert request["body"] == {"content": content,
                                   "allowed_mentions": {"parse": [], "users": ["123456789012345678"]}}
    else:
        assert fake.messages[channel] == []


@pytest.mark.parametrize("case", ["foreign-guild", "foreign-channel", "private-thread", "not-own", "empty"])
def test_rejects_unsafe_mutations_before_write(tmp_path, case):
    fake = RecoveryDiscord()
    fake.thread("4000", "private", private=True)
    fake.message(BOARD, "5000", "human post")
    fake.message("4000", "5000", "private", webhook=True)
    target = {"foreign-guild": f"https://discord.com/channels/999/{BOARD}/5000",
              "foreign-channel": f"https://discord.com/channels/{GUILD}/2000/5000",
              "private-thread": f"https://discord.com/channels/{GUILD}/4000/5000"}.get(case, "5000")
    argv = ["edit", target, "--text", " " if case == "empty" else "fixed"]
    with pytest.raises(db.BoardError):
        invoke(fake, tmp_path, argv)
    assert all(r["method"] == "GET" for r in fake.requests)


@pytest.mark.parametrize("command,uncertain", [("thread", False), ("thread", True),
                                               ("edit", True), ("delete", True)])
def test_confirmed_thread_and_uncertain_writes_not_retried(tmp_path, command, uncertain):
    fake = RecoveryDiscord()
    fake.message(BOARD, "5000", "old", webhook=True)
    method = {"thread": "POST", "edit": "PATCH", "delete": "DELETE"}[command]
    path = (f"/api/v10/channels/{BOARD}/messages/5000/threads" if command == "thread"
            else f"/api/v10/webhooks/{HOOK_ID}/{HOOK_TOKEN}/messages/5000")
    argv = [command, f"https://discord.com/channels/{GUILD}/{BOARD}/5000"]
    argv += ["--name", "  repair   discussion "] if command == "thread" else []
    argv += ["--text", "fixed"] if command == "edit" else []
    if uncertain:
        fake.fail(method, path, socket.timeout("response lost"))
        with pytest.raises(db.DeliveryUncertain, match="check the message before trying again"):
            invoke(fake, tmp_path, argv)
    else:
        result = invoke(fake, tmp_path, argv)
        assert result["thread"] == {"id": "5000", "name": "repair discussion",
                                    "url": f"https://discord.com/channels/{GUILD}/5000"}
        assert fake.channels["5000"]["type"] == db.PUBLIC_THREAD
        assert list(tmp_path.glob("*-threads-*.json"))
    assert len([r for r in fake.requests if r["method"] == method]) == 1


def test_thread_already_open_returns_it_without_writing(tmp_path):
    fake = RecoveryDiscord()
    message = fake.message(BOARD, "5000", "old", webhook=True)
    message["thread"] = {"id": "6000", "name": "earlier"}
    result = invoke(fake, tmp_path, ["thread", "5000", "--name", "replacement"])
    assert result["thread"] == {"id": "6000", "name": "earlier",
                                "url": f"https://discord.com/channels/{GUILD}/6000"}
    assert all(r["method"] == "GET" for r in fake.requests)
    assert list(tmp_path.glob("*-threads-*.json")) == []
