"""CLI registration and dispatch for local thread subscriptions."""

from discord_agent_board import Board, BoardError, DiscordClient


def add_commands(sub, common):
    for command, description in (
            ("follow", "follow future messages in an existing public board thread"),
            ("unfollow", "stop following a public board thread")):
        parser = sub.add_parser(command, parents=[common], help=description,
                                description=description)
        parser.add_argument("target", metavar="TARGET", help="public board thread ID or link")


def run(args, config, *, opener=None):
    if args.command not in ("follow", "unfollow"):
        raise BoardError("reading command must be follow or unfollow")
    board = Board(DiscordClient(config, opener=opener), config)
    operation = getattr(board, args.command)
    return operation(args.target, agent=getattr(args, "agent", None) or config.agent)
