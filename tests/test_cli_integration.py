"""CLI seams for the combined agent-friendly commands."""

import json

import pytest

from test_board import BOARD, GUILD, ME, cli, sf
from test_board import fake, run  # noqa: F401


def test_cli_inbox_shows_full_body_and_peek_does_not_consume(fake, run, capsys):
    body = ME + ": " + "context " * 50 + "ACTION_AT_END"
    fake.message(BOARD, sf(5), body)

    assert run("inbox", "--agent", ME, "--peek") == 0
    assert "ACTION_AT_END" in capsys.readouterr().out
    assert run("read", "--agent", ME) == 0
    assert "ACTION_AT_END" in capsys.readouterr().out


def test_cli_post_accepts_thread_link_and_reports_wrong_guild(fake, run, capsys):
    fake.thread("5000", "Topic")
    assert run("post", "--agent", ME, "--thread",
               f"https://discord.com/channels/{GUILD}/5000", "--text", "hello", "--json") == 0
    assert json.loads(capsys.readouterr().out)["message"]["thread_id"] == "5000"

    assert run("post", "--agent", ME, "--thread", "https://discord.com/channels/999/5000",
               "--text", "hello", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and result["delivery"] == "not_sent"
    assert result["error"]["type"] == "ScopeError"
    assert len(fake.sent("POST")) == 1


def test_cli_partial_post_failure_returns_confirmed_message(fake, run, capsys):
    fake.threads_allowed = False
    assert run("post", "--agent", ME, "--new-thread", "Topic", "--text", "report", "--json") == 1
    result = json.loads(capsys.readouterr().out)
    assert result["delivery"] == "confirmed" and result["message"]["id"]
    assert result["error"]["type"] == "ThreadNotStarted"
    assert len(fake.messages[BOARD]) == 1


@pytest.mark.parametrize("target", ["5000", f"https://discord.com/channels/{GUILD}/5000",
                                     f"https://discord.com/channels/{GUILD}/{BOARD}/5000"])
def test_cli_registry_dispatches_follow_and_offline_status(fake, run, capsys, target):
    fake.thread("5000", "Topic")
    assert run("follow", target, "--agent", ME, "--json") == 0
    assert json.loads(capsys.readouterr().out)["subscribed"]
    fake.requests.clear()
    assert run("status", "--agent", ME, "--json") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["channel_id"] == BOARD and result["agent"] == ME
    assert not fake.requests
