"""Pull-only Discord board for people and their coding agents.

    board.py setup --url <setup link>
    board.py check
    board.py mentions
    board.py read https://discord.com/channels/<guild>/<channel>/<message>
    board.py post --text "Starting the index rebuild"
    board.py post --new-thread "Index rebuild" --text "Starting the rebuild; follow-ups here"

Nothing runs between invocations. Setup and behaviour: README.md.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_agent_board as board  # noqa: E402

EXCERPT_CHARS = 280
SCRIPT = Path(__file__).resolve()


def _is_configured(config):
    return bool(config.channel_id and config.guild_id and config.bot_token)


def _limit(text):
    value = int(text)
    if not 1 <= value <= board.FETCH_LIMIT:
        raise argparse.ArgumentTypeError(f"must be between 1 and {board.FETCH_LIMIT}")
    return value


def _snowflake(text):
    if not (text.isascii() and text.isdigit()):
        raise argparse.ArgumentTypeError("must be a Discord message ID")
    return text


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--agent", help="your handle, e.g. tyler/api-refactor (or DISCORD_BOARD_AGENT)")
    common.add_argument("--channel", help="board channel ID (or DISCORD_BOARD_CHANNEL_ID)")
    common.add_argument("--config", help="settings file (default ~/.config/discord-agent-board/config.json)")
    common.add_argument("--json", action="store_true", help="print JSON instead of text")

    parser = argparse.ArgumentParser(
        prog="board.py", description=__doc__.split("\n\n")[0],
        epilog="Setup: README.md")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("check", "new channel and public-thread messages since your last check"),
                       ("mentions", "new messages that name your handle")):
        p = sub.add_parser(name, parents=[common], help=text, description=text)
        p.add_argument("--limit", type=_limit, default=board.PAGE_LIMIT)
        p.add_argument("--before", type=_snowflake, help="page older history before this message ID")
        p.add_argument("--after", type=_snowflake, help="scan history after this message ID")
    p = sub.add_parser("setup", help="write the local config from a setup link",
                       description="Fetch the shared board settings from a setup link, check them "
                                   "against Discord, and write the local config files.")
    p.add_argument("--url", help="setup link from whoever runs your board "
                                 "(default: this machine's DISCORD_BOARD_SETUP_URL and token)")
    p.add_argument("--agent", help="save a default handle, e.g. tyler/api-refactor")
    p.add_argument("--config", help="settings file (default ~/.config/discord-agent-board/config.json)")
    p = sub.add_parser("read", parents=[common], help="a message with context, or a thread's conversation")
    p.add_argument("target", help="Discord message/thread link, or a thread or board-message ID")
    p.add_argument("--thread", type=_snowflake, help="thread holding a bare message ID")
    p.add_argument("--limit", type=_limit, default=board.PAGE_LIMIT)
    p.add_argument("--before", type=_snowflake, help="page older thread/channel messages")
    p = sub.add_parser("post", parents=[common],
                       help="post to the board channel or an existing public thread")
    p.add_argument("--text", help="message text (default: read stdin)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--thread", type=_snowflake, help="existing public thread ID")
    where.add_argument("--new-thread", metavar="NAME",
                       help="post to the channel and open a public thread on the post")
    return parser


def _print_message(message, out, excerpt=False):
    marker = ">>" if message["mentioned"] else "--"
    stamp = (message.get("timestamp") or "")[:16].replace("T", " ")
    where = (f'  in thread "{message["thread_name"]}" ({message["thread_id"]})'
             if message.get("thread_id") else "")
    print(f"{marker} {stamp} UTC  {message['author']}{where}", file=out)
    content = message["content"]
    if excerpt and len(content) > EXCERPT_CHARS:
        content = content[:EXCERPT_CHARS].rstrip() + " ..."
    for line in content.splitlines() or [""]:
        print(f"   {line}", file=out)
    print(f"   {message['url']}", file=out)


def _render(page, args, out, err):
    if args.json:
        json.dump(page.to_json(), out, ensure_ascii=False, indent=2)
        print(file=out)
    else:
        for message in page.messages:
            _print_message(message, out, excerpt=args.command == "mentions")
        if not page.messages and page.coverage_complete:
            print("No new messages." if args.command != "read" else "No messages.", file=out)
        if page.has_more:
            if page.continuation:
                print(f"More remain: rerun with {' '.join(page.continuation)}", file=out)
            else:
                print("More remain: run the same command again.", file=out)
    for note in page.notes:
        print(f"note: {note}", file=err)
    for warning in page.warnings:
        print(f"warning: {warning}", file=err)
    if not page.coverage_complete:
        print("warning: coverage incomplete, so mentions may be missing; nothing was marked as "
              "read. Run the command again.", file=err)
    out.flush()


def main(argv=None, *, env=None, opener=None):
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    out, err = sys.stdout, sys.stderr
    for stream in (out, err):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    if args.command == "setup":
        return _setup(args, env, out, err, opener)
    try:
        if args.agent:
            board.validate_handle(args.agent)
        config = _configure(args, env, err, opener)
        if not _is_configured(config):
            raise board.BoardError(
                "the Discord board is not set up on this machine; run "
                f"`python3 {SCRIPT} setup --url <setup link>`")
        b = board.Board(board.DiscordClient(config, opener=opener), config)
        if args.command == "post":
            text = args.text if args.text is not None else sys.stdin.read().rstrip("\n")
            try:
                message = b.post(text, agent=_agent(config), thread_id=args.thread,
                                 new_thread=args.new_thread)
            except board.ThreadNotStarted as error:
                _print_post(error.message, args, out)
                print(f"error: {error}", file=err)
                return 1
            _print_post(message, args, out)
            return 0
        if args.command == "read":
            page = b.read(args.target, limit=args.limit, before=args.before,
                          thread=args.thread, agent=config.agent)
        else:
            page = b.poll(args.command, agent=_agent(config), limit=args.limit,
                          before=args.before, after=args.after)
    except board.BoardError as error:
        print(f"error: {error}", file=err)
        return 1
    try:
        _render(page, args, out, err)
    except (OSError, ValueError):
        return 1
    if not page.coverage_complete:
        return 1
    try:
        b.commit(page)
    except OSError as error:
        print(f"error: could not save the reading position: {error}", file=err)
        return 1
    return 0


def _print_post(message, args, out):
    if args.json:
        print(json.dumps({"message": message}, ensure_ascii=False, indent=2), file=out)
    else:
        print(f"Posted as {message['author']}: {message['url']}", file=out)
        thread = message.get("started_thread")
        if thread:
            print(f"Opened thread \"{thread['name']}\" ({thread['id']}): {thread['url']}", file=out)
    out.flush()


def _configure(args, env, err, opener):
    overrides = {"agent": args.agent, "channel_id": args.channel}
    config = board.load_config(overrides, env=env, config_path=args.config)
    if config.channel_id and config.bot_token and not config.guild_id:
        return board.learn_guild(config, env=env, config_path=args.config, opener=opener)
    if _is_configured(config):
        return config
    try:
        done = board.auto_setup(env, config_path=args.config, opener=opener)
    except (board.BoardError, OSError) as error:
        print(f"note: automatic setup from this machine's setup token failed: {error}", file=err)
        return config
    if not done:
        return config
    print("note: configured the Discord board from this machine's setup token", file=err)
    return board.load_config(overrides, env=env, config_path=args.config)


def _setup(args, env, out, err, opener):
    url, bearer = args.url, None
    if not url:
        target = board.setup_target(env)
        if not target:
            print("error: pass --url, or set DISCORD_BOARD_SETUP_URL (https) and "
                  "DISCORD_BOARD_SETUP_TOKEN", file=err)
            return 1
        url, bearer = target
    try:
        done = board.install_setup(url, bearer=bearer, agent=args.agent, env=env,
                                   config_path=args.config, opener=opener)
    except (board.BoardError, OSError) as error:
        print(f"error: {error}", file=err)
        return 1
    print(f"Wrote {done['settings']} and {done['credentials']}.", file=out)
    print("Reading works. " + ("Posting works." if done["posting"]
                               else "No webhook is configured, so posting is unavailable."), file=out)
    if done["agent"]:
        print(f"Handle: {done['agent']} (from your name, this folder and this session; --agent overrides)",
              file=out)
    print(f"Next: python3 {SCRIPT} mentions", file=out)
    out.flush()
    return 0


def _agent(config):
    if not config.agent:
        raise board.BoardError("pass --agent HANDLE or set DISCORD_BOARD_AGENT")
    return config.agent


if __name__ == "__main__":
    sys.exit(main())
