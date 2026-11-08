"""Message recovery commands for the board CLI."""

import re
import sys
from urllib.parse import urlencode

import discord_agent_board as board

_LINK = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)(?:/(\d+))?/?")


def add_commands(sub, common):
    for command, help_text in (("edit", "edit a message sent by the configured webhook"),
                               ("delete", "delete a message sent by the configured webhook"),
                               ("thread", "open a public thread on a board message")):
        parser = sub.add_parser(command, parents=[common], help=help_text)
        parser.add_argument("target", help="Discord message link or message ID")
        parser.add_argument("--thread", help="public thread ID or link containing a bare message ID")
        if command == "edit":
            parser.add_argument("--text", help="replacement text (default: read stdin)")
        elif command == "thread":
            parser.add_argument("--name", required=True, help="public thread name")


class _ActionClient(board.DiscordClient):
    """Marks mutations as writes, so an uncertain delivery is reported, never retried."""

    UNCERTAIN = "; the change may have been applied, so check the message before trying again"

    def _request(self, method, url, *, body=None, bot=True, write=False):
        try:
            return super()._request(method, url, body=body, bot=bot,
                                    write=write or method in ("POST", "PATCH", "DELETE"))
        except board.DeliveryUncertain as err:
            head = str(err).partition("; the message may have")[0]
            raise board.DeliveryUncertain(head + self.UNCERTAIN) from None


def _thread_id(value, config):
    link = _LINK.fullmatch(value)
    if link:
        guild, channel, _ = link.groups()
        if guild != config.guild_id:
            raise board.ScopeError("the thread link is not in the configured guild")
        return channel
    if not board._is_id(value):
        raise board.BoardError("--thread takes a public thread ID or link")
    return value


def _target(client, target, thread):
    config = client.config
    link = _LINK.fullmatch(target)
    if link:
        guild, channel_id, message_id = link.groups()
        if guild != config.guild_id:
            raise board.ScopeError("the message link is not in the configured guild")
        if not message_id:
            raise board.BoardError("TARGET must name a message, not just a channel or thread")
    elif board._is_id(target):
        channel_id, message_id = config.channel_id, target
    else:
        raise board.BoardError("TARGET takes a Discord message link or numeric message ID")
    if thread:
        tid = _thread_id(thread, config)
        location = client.get_channel(tid)
        if location.get("type") != board.PUBLIC_THREAD:
            raise board.ScopeError("--thread must name a public thread of the board channel")
        if link and tid != channel_id:
            raise board.ScopeError("--thread does not match the message link")
        channel_id = tid
    client.board_channel()
    client.get_channel(channel_id)
    message = client.get_message(channel_id, message_id)
    if str(message.get("id")) != message_id or str(message.get("channel_id")) != channel_id:
        raise board.BoardError("Discord returned a different message than TARGET")
    return channel_id, message_id, message


def _edit_body(text):
    if not text or not text.strip():
        raise board.BoardError("refusing to edit to an empty message")
    if len(text) > board.MAX_CONTENT_CHARS:
        raise board.BoardError(
            f"message is {len(text)} characters; Discord allows {board.MAX_CONTENT_CHARS}")
    users = board.mentioned_user_ids(text)
    if len(users) > board.MAX_MENTIONS_PER_POST:
        raise board.BoardError(
            f"message mentions {len(users)} users; Discord allows {board.MAX_MENTIONS_PER_POST}")
    allowed = {"parse": []}
    if users:
        allowed["users"] = users
    return {"content": text, "allowed_mentions": allowed}


def run(args, config, *, opener=None):
    command = args.command
    if command not in ("edit", "delete", "thread"):
        raise board.BoardError(f"unknown recovery command: {command}")
    body = None
    if command == "edit":
        text = args.text if args.text is not None else sys.stdin.read().rstrip("\n")
        body = _edit_body(text)
    elif command == "thread":
        name = " ".join(args.name.split())
        if not 1 <= len(name) <= board.THREAD_NAME_CHARS:
            raise board.BoardError(f"a thread name needs 1 to {board.THREAD_NAME_CHARS} characters")
    client = _ActionClient(config, opener=opener)
    channel_id, message_id, message = _target(client, args.target, getattr(args, "thread", None))
    result = {"action": command, "id": message_id, "channel_id": channel_id,
              "url": f"https://discord.com/channels/{config.guild_id}/{channel_id}/{message_id}"}
    if command == "thread":
        if channel_id != config.channel_id:
            raise board.ScopeError("threads can only be opened on board-channel messages")
        existing = message.get("thread")
        opened = not (isinstance(existing, dict) and existing.get("id"))
        if opened:
            thread = client.start_thread(message_id, name)
            if (str(thread.get("id")) != message_id
                    or thread.get("type") != board.PUBLIC_THREAD):
                raise board.DeliveryUncertain(
                    "Discord returned an unexpected thread; check the message before trying again")
        else:
            thread = existing
        result["thread"] = {"id": str(thread["id"]), "name": thread.get("name"),
                            "url": f"https://discord.com/channels/{config.guild_id}/{thread['id']}"}
        if opened and config.agent and config.state_dir:
            try:
                board.Board(client, config)._remember_thread(config.agent, str(thread["id"]), message_id)
            except OSError:
                pass
        return result
    hook_url = client.check_webhook()
    hook_id = board.WEBHOOK_RE.fullmatch(config.webhook_url).group(1)
    if str(message.get("webhook_id")) != hook_id:
        raise board.ScopeError("TARGET was not sent by the configured webhook")
    url = f"{hook_url}/messages/{message_id}"
    if channel_id != config.channel_id:
        url += "?" + urlencode({"thread_id": channel_id})
    response = client._request("PATCH" if command == "edit" else "DELETE", url,
                               body=body, bot=False, write=True)
    if command == "edit":
        if not (isinstance(response, dict) and str(response.get("id")) == message_id
                and str(response.get("channel_id")) == channel_id):
            raise board.DeliveryUncertain(
                "Discord returned an unexpected edit response; check the message before trying again")
        result["content"] = response.get("content")
    else:
        result["deleted"] = True
    return result
