"""Session identity, board profiles, and offline status/doctor contracts."""

import argparse
import json

import pytest

from test_board import BOARD, GUILD, OTHER, WEBHOOK_URL, checkout, config, db, fake  # noqa: F401
import board_identity

ME = "tyler/api-refactor"


def write_home(tmp_path, settings=None, creds=None):
    cfg_dir = tmp_path / "home" / ".config" / "discord-agent-board"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps(settings or {}))
    if creds is not None:
        (cfg_dir / "credentials.json").write_text(json.dumps(creds))
    return {"HOME": str(tmp_path / "home")}


def session_handles(state_dir):
    folder = state_dir / "handles"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def test_session_handle_is_not_cached_until_the_board_is_configured(tmp_path):
    state = tmp_path / "state"
    env = {"HOME": str(tmp_path / "home"), "USER": "ana", "DISCORD_BOARD_SESSION": "s-1",
           "DISCORD_BOARD_STATE_DIR": str(state)}
    project, temp = checkout(tmp_path), tmp_path / "temp"
    temp.mkdir()

    assert db.load_config({}, env=env, cwd=project).agent.startswith("ana/docs-site.")
    assert db.load_config({}, env=env, cwd=temp).agent.startswith("ana/temp.")
    assert not session_handles(state)

    write_home(tmp_path, {"person": "ana", "channel_id": BOARD})
    configured = db.load_config({}, env=env, cwd=temp)
    assert json.loads(session_handles(state)[0].read_text())["agent"] == configured.agent
    assert db.load_config({}, env=env, cwd=project).agent == configured.agent


def test_bot_token_alone_does_not_cache_the_provisional_os_name(tmp_path):
    state = tmp_path / "state"
    mcp = tmp_path / "home" / ".config" / "mcp-discord"
    mcp.mkdir(parents=True)
    (mcp / "config.json").write_text(json.dumps({"botToken": "mcp-token"}))
    env = {"HOME": str(tmp_path / "home"), "USER": "ana", "DISCORD_BOARD_SESSION": "s-1",
           "DISCORD_BOARD_STATE_DIR": str(state), "DISCORD_BOARD_CHANNEL_ID": BOARD}
    project = checkout(tmp_path)

    assert db.load_config({}, env=env, cwd=project).agent.startswith("ana/")
    assert not session_handles(state)

    write_home(tmp_path, {"person": "ben", "channel_id": BOARD, "guild_id": GUILD})
    configured = db.load_config({}, env=env, cwd=project)
    assert configured.agent.startswith("ben/") and session_handles(state)


def test_person_change_before_setup_does_not_reuse_a_stale_default(tmp_path):
    state = tmp_path / "state"
    env = {"HOME": str(tmp_path / "home"), "USER": "ana", "DISCORD_BOARD_SESSION": "s-1",
           "DISCORD_BOARD_STATE_DIR": str(state)}
    project = checkout(tmp_path)

    provisional = db.load_config({}, env=env, cwd=project)
    assert provisional.agent.startswith("ana/") and not session_handles(state)

    write_home(tmp_path, {"person": "ben"})
    configured = db.load_config({}, env=env, cwd=project)

    assert configured.agent.startswith("ben/")
    assert json.loads(session_handles(state)[0].read_text())["agent"] == configured.agent


@pytest.mark.parametrize("source", ["PI_SESSION_FILE", "CLAUDE_PROJECT_DIR"])
def test_session_workdir_beats_an_arbitrary_temp_cwd(tmp_path, source):
    project, temp = checkout(tmp_path), tmp_path / "temp"
    temp.mkdir()
    env = {"HOME": str(tmp_path / "home"), "USER": "ana", "DISCORD_BOARD_SESSION": "s-1"}
    if source == "PI_SESSION_FILE":
        session = tmp_path / "session.jsonl"
        session.write_text(json.dumps({"type": "session", "cwd": str(project)}) + "\n")
        env["PI_SESSION_FILE"] = str(session)
    else:
        env["CLAUDE_PROJECT_DIR"] = str(project)

    assert db.load_config({}, env=env, cwd=temp).agent.startswith("ana/docs-site.")
    missing = {**env, source: str(tmp_path / "gone")}
    assert db.load_config({}, env=missing, cwd=temp).agent.startswith("ana/temp.")


def identity_args(*argv):
    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent")
    common.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    board_identity.add_commands(sub, common)
    return parser.parse_args(["identity", *argv])


def test_identity_set_and_reset_change_only_the_session_default(tmp_path):
    settings = {"person": "ana", "channel_id": BOARD, "guild_id": GUILD, "agent": "ana/shared"}
    env = {**write_home(tmp_path, settings), "DISCORD_BOARD_SESSION": "s-1"}
    args = identity_args("--set", "ana/search-index")
    cfg = db.load_config({}, env=env, cwd=checkout(tmp_path))
    assert cfg.agent == "ana/shared"

    assert board_identity.run(args, cfg, env=env) == {
        "agent": "ana/search-index", "session_default": "ana/search-index", "changed": True}
    assert db.load_config({}, env=env).agent == "ana/search-index"
    assert db.load_config({"agent": ME}, env=env).agent == ME

    reset = board_identity.run(identity_args("--reset"), cfg, env=env)
    assert reset["agent"] == "ana/shared" and reset["changed"]
    assert not session_handles(cfg.state_dir)


def test_identity_set_requires_a_session_id(tmp_path):
    env = write_home(tmp_path, {"person": "ana", "channel_id": BOARD, "guild_id": GUILD})
    cfg = db.load_config({}, env=env)
    with pytest.raises(db.BoardError, match="session ID"):
        board_identity.run(identity_args("--set", ME), cfg, env=env)


def test_profile_overlays_destination_and_webhook_and_cli_still_wins(tmp_path):
    settings = {"channel_id": BOARD, "guild_id": GUILD,
                "profiles": {"research": {"channel_id": OTHER, "guild_id": "888", "person": "ana"}}}
    creds = {"bot_token": "base-token", "webhook_url": WEBHOOK_URL,
             "profiles": {"research": {"webhook_url": "https://discord.com/api/webhooks/7000/research-secret"}}}
    env = write_home(tmp_path, settings, creds)

    base = db.load_config({}, env=env)
    selected = db.load_config({"profile": "research"}, env=env)
    from_env = db.load_config({}, env={**env, "DISCORD_BOARD_PROFILE": "research"})
    override = db.load_config({"profile": "research", "channel_id": "3000"}, env=env)

    assert (base.channel_id, base.guild_id, base.profile) == (BOARD, GUILD, None)
    assert base.webhook_url == WEBHOOK_URL
    assert (selected.channel_id, selected.guild_id) == (OTHER, "888")
    assert selected.webhook_url.endswith("research-secret") and selected.agent.startswith("ana/")
    assert from_env.channel_id == OTHER == selected.channel_id
    assert override.channel_id == "3000" and override.webhook_url == selected.webhook_url
    with pytest.raises(db.BoardError, match="unknown board profile"):
        db.load_config({"profile": "nope"}, env=env)


def status_args(*argv):
    parser = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    board_identity.add_commands(sub, common)
    return parser.parse_args(list(argv))


def test_status_reports_configuration_without_secrets_or_network(config, fake, tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    result = board_identity.run(status_args("status"), config, env=env, opener=fake)

    assert result["agent"] == ME
    assert result["configured"] == {"read": True, "post": True}
    assert {"peek", "follow", "edit", "thread", "profiles"} <= set(result["capabilities"])
    assert result["credentials"] == {"bot_token_present": True, "webhook_url_present": True}
    assert result["version"] and result["config_path"].endswith("config.json")
    assert not fake.requests
    flat = json.dumps(result)
    assert config.bot_token not in flat and config.webhook_url not in flat


def test_doctor_checks_read_and_post_without_writing(fake, config):
    result = board_identity.run(status_args("doctor"), config, env={}, opener=fake)

    assert result["ok"] and result["checks"] == {"channel": True, "webhook": True}
    assert not fake.sent("POST") and not fake.sent("PATCH") and not fake.sent("DELETE")


def test_doctor_reports_actionable_errors_instead_of_requests(tmp_path, fake):
    config = db.BoardConfig(guild_id=None, channel_id=None, agent=ME,
                            bot_token=None, webhook_url=None, state_dir=tmp_path / "state")

    result = board_identity.run(status_args("doctor"), config, env={}, opener=fake)

    assert not result["ok"] and len(result["errors"]) == 3
    assert any("setup --url" in error for error in result["errors"])
    assert not fake.requests
