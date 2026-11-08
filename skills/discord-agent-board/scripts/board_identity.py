"""Configuration diagnostics and session identity commands. No Discord writes."""

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import discord_agent_board as board

CAPABILITIES = ("inbox", "read", "peek", "author-metadata", "follow", "unfollow",
                "post", "dry-run", "edit", "delete", "thread", "profiles",
                "status", "doctor", "identity")


def add_commands(sub, common):
    for name, help_text in (("status", "show local configuration without contacting Discord"),
                            ("doctor", "check channel and webhook access without writing"),
                            ("identity", "show or change this session's default handle")):
        parser = sub.add_parser(name, parents=[common], help=help_text)
        if name == "identity":
            change = parser.add_mutually_exclusive_group()
            change.add_argument("--set", dest="set_handle", metavar="HANDLE",
                                help="save this session's default handle (explicit --agent still wins)")
            change.add_argument("--reset", action="store_true", help="clear the session's saved handle")


def _version():
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def run(args, config, *, opener=None, env=None, config_path=None):
    env = os.environ if env is None else env
    config_path = config_path or getattr(args, "config", None)
    if args.command == "identity":
        value = getattr(args, "set_handle", None)
        reset = getattr(args, "reset", False)
        if value is not None or reset:
            board.save_session_handle(env, config.state_dir, None if reset else value)
            config = board.load_config(
                {"agent": getattr(args, "agent", None), "profile": config.profile,
                 "guild_id": config.guild_id, "channel_id": config.channel_id,
                 "state_dir": config.state_dir},
                env=env, config_path=config_path, persist_handle=False)
        return {"agent": config.agent, "session_default": None if reset else value,
                "changed": value is not None or reset}

    if args.command not in ("status", "doctor"):
        raise board.BoardError(f"unsupported identity command {args.command!r}")
    reading = bool(config.channel_id and config.bot_token)
    posting = bool(config.channel_id and board.WEBHOOK_RE.fullmatch(config.webhook_url or ""))
    result = {"agent": config.agent, "profile": config.profile,
              "guild_id": config.guild_id, "channel_id": config.channel_id,
              "state_dir": str(config.state_dir) if config.state_dir else None,
              "config_path": str(board.settings_path_for(env, config_path)),
              "version": _version(),
              "credentials": {"bot_token_present": bool(config.bot_token),
                              "webhook_url_present": bool(config.webhook_url)},
              "capabilities": list(CAPABILITIES),
              "configured": {"read": reading, "post": posting}}
    if args.command == "status":
        return result

    errors, checks = [], {}
    if not config.channel_id or not board._is_id(config.channel_id):
        errors.append("Configure a numeric board channel_id; run board.py setup --url <setup link>.")
    if not config.bot_token:
        errors.append("Configure a bot token in credentials.json or DISCORD_BOARD_BOT_TOKEN to check channel access.")
    if not errors:
        client = board.DiscordClient(config, opener=opener)
        try:
            if not config.guild_id:
                channel = board._expect(client._request(
                    "GET", f"{board.API}/channels/{config.channel_id}"), dict, "a channel")
                guild_id = str(channel.get("guild_id") or "")
                if not guild_id:
                    raise board.BoardError("the board channel is not in a server")
                config = replace(config, guild_id=guild_id)
                client = board.DiscordClient(config, opener=opener)
            client.board_channel()
            checks["channel"] = True
            result["guild_id"] = config.guild_id
        except board.BoardError as err:
            checks["channel"] = False
            errors.append(f"Channel check failed: {err}. Check the selected profile, channel ID and bot access.")
        if config.webhook_url:
            try:
                client.check_webhook()
                checks["webhook"] = True
            except board.BoardError as err:
                checks["webhook"] = False
                errors.append(f"Webhook check failed: {err}. Configure a webhook for this board channel.")
    if not config.webhook_url:
        checks["webhook"] = False
        errors.append("No webhook configured; reads can work, but posting needs this channel's webhook in credentials.json.")
    result.update(ok=not errors, checks=checks, errors=errors)
    return result
