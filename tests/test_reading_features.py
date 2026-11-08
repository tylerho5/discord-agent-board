"""Offline reading and subscription contracts."""

import argparse

import pytest

from test_board import BOARD, GUILD, OTHER, board_for, client, config, db, fake, sf

ME = "tyler/api-refactor"


@pytest.mark.parametrize("view", ["inbox", "new", "recent", "context"])
def test_peek_does_not_consume_messages(fake, client, config, view):
    message = fake.message(BOARD, sf(5), f"@{ME} please check")
    board, _ = board_for(client, config)
    if view == "inbox":
        peek = board.inbox(agent=ME, peek=True)
    else:
        target = message["id"] if view == "context" else None
        peek = board.read(target, agent=ME, recent=view == "recent", peek=True)
    assert [m["id"] for m in peek.messages] == [message["id"]]
    assert not peek.writes
    board.commit(peek)
    assert not config.state_dir.exists()
    normal = board.inbox(agent=ME)
    assert [m["id"] for m in normal.messages] == [message["id"]]
    board.commit(normal)
    assert not board.inbox(agent=ME).messages


def test_read_preserves_discord_identity_metadata(fake, client, config):
    message = fake.message(BOARD, sf(5), "<@42> hello", author="ana-login")
    message["author"]["global_name"] = "Ana Display"
    message["mentions"] = [{"id": "42", "username": "sam-login", "global_name": "Sam Display"},
                           {"id": "43", "username": "lee-login", "global_name": None}]
    board, _ = board_for(client, config)
    normalized = board.read(agent=ME, peek=True).messages[0]
    assert (normalized["author"], normalized["author_id"], normalized["username"],
            normalized["display_name"]) == ("Ana Display", "1", "ana-login", "Ana Display")
    assert normalized["mentioned_users"] == [
        {"id": "42", "username": "sam-login", "display_name": "Sam Display"},
        {"id": "43", "username": "lee-login", "display_name": "lee-login"}]


@pytest.mark.parametrize("as_link", [False, True])
def test_follow_starts_after_latest_preserves_marker_and_unfollows(
        fake, client, config, as_link):
    fake.thread("5000", "Topic")
    old = fake.message("5000", sf(10), "old reply")["id"]
    target = f"https://discord.com/channels/{GUILD}/5000" if as_link else "5000"
    board, _ = board_for(client, config)
    followed = board.follow(target, agent=ME)
    assert followed == {"agent": ME, "thread_id": "5000", "thread_name": "Topic",
                        "url": f"https://discord.com/channels/{GUILD}/5000",
                        "subscribed": True, "since": old}
    new = fake.message("5000", sf(5), "new reply")["id"]
    board, _ = board_for(client, config)
    assert board.follow(target, agent=ME)["since"] == old
    assert [m["id"] for m in board.inbox(agent=ME, peek=True).messages] == [new]
    assert board.unfollow(target, agent=ME) == dict(followed, subscribed=False, since=None)
    board, _ = board_for(client, config)
    assert not board.inbox(agent=ME, peek=True).messages
    assert board.unfollow(target, agent=ME)["subscribed"] is False
    assert not fake.sent("POST")


@pytest.mark.parametrize("target", [BOARD, OTHER, "6000"])
@pytest.mark.parametrize("command", ["follow", "unfollow"])
def test_subscriptions_require_an_existing_scoped_thread(fake, client, config, target, command):
    fake.thread("6000", "Private", private=True)
    board, _ = board_for(client, config)
    with pytest.raises(db.BoardError):
        getattr(board, command)(target, agent=ME)
    assert not config.state_dir.exists() and not fake.sent("POST")


def test_reading_command_module_dispatches_without_printing(fake, config, capsys):
    import board_reading

    fake.thread("5000", "Topic")
    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent")
    sub = parser.add_subparsers(dest="command", required=True)
    board_reading.add_commands(sub, common)
    for command, subscribed in [("follow", True), ("unfollow", False)]:
        args = parser.parse_args([command, "5000", "--agent", ME])
        result = board_reading.run(args, config, opener=fake)
        assert result["thread_id"] == "5000" and result["subscribed"] is subscribed
    assert not capsys.readouterr().out and not fake.sent("POST")
