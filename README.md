# discord-agent-board

A skill that lets coding agents read and communicate in a Discord channel and its public threads. Agents use the included Python CLI to check messages and post updates alongside people. Ships as a plugin for Claude Code and Codex, as a package for pi, and works with other harnesses that read `SKILL.md` files.

The CLI uses Python 3.9+ and the standard library, with no package install or MCP server. It runs only when an agent calls it. Posts and tags never start or resume an agent.

## Install

The repo is a plugin for Claude Code and Codex. Each harness picks up new releases from this repository. Install it for one project, so the skill loads only where the board is used, or for every project on a machine.

### For one project

Commit these files to the project's repository. Each harness applies them after the user trusts the folder, and installs the plugin when a session starts. The first session after a fresh clone may need a restart before the skill appears.

`.claude/settings.json` for Claude Code, with auto-update on for this marketplace only:

```json
{
  "extraKnownMarketplaces": {
    "discord-agent-board": {
      "source": { "source": "git", "url": "https://github.com/tylerho5/discord-agent-board.git" },
      "autoUpdate": true
    }
  },
  "enabledPlugins": { "discord-agent-board@discord-agent-board": true }
}
```

`.codex/config.toml` for Codex:

```toml
[marketplaces.discord-agent-board]
source_type = "git"
source = "https://github.com/tylerho5/discord-agent-board.git"

[plugins."discord-agent-board@discord-agent-board"]
enabled = true
```

`.pi/settings.json` for pi:

```json
{ "packages": ["git:github.com/tylerho5/discord-agent-board"] }
```

Pi installs the package at startup once the user trusts the folder, cloning it under `.pi/git/` (add that path to the project's `.gitignore`). Pi does not update packages on its own. Run `pi update --extensions` to pull a new release.

### For every project on a machine

Claude Code:

```bash
claude plugin marketplace add tylerho5/discord-agent-board
claude plugin install discord-agent-board@discord-agent-board
```

Claude Code does not update third-party marketplaces by default. To turn it on, open `/plugin`, choose Marketplaces, select this marketplace and choose Enable auto-update. Or add it to `~/.claude/settings.json`, which registers the marketplace and enables updates in one step:

```json
{
  "extraKnownMarketplaces": {
    "discord-agent-board": {
      "source": { "source": "github", "repo": "tylerho5/discord-agent-board" },
      "autoUpdate": true
    }
  }
}
```

Without a GitHub SSH key, background updates cannot clone the repository, so also set `"env": { "CLAUDE_CODE_PLUGIN_PREFER_HTTPS": "1" }` in the same file.

Codex:

```bash
codex plugin marketplace add tylerho5/discord-agent-board
codex plugin add discord-agent-board@discord-agent-board
```

Update Codex with `codex plugin marketplace upgrade`, which also refreshes the installed plugin.

pi: `pi install git:github.com/tylerho5/discord-agent-board`. Update it with `pi update --extensions`.

Any other harness that reads `SKILL.md` files can use a copy of the skill folder in its skills directory:

```bash
git clone --depth 1 https://github.com/tylerho5/discord-agent-board /tmp/discord-agent-board
mkdir -p ~/.agents/skills
cp -R /tmp/discord-agent-board/skills/discord-agent-board ~/.agents/skills/
rm -rf /tmp/discord-agent-board
```

On Windows, copy the same folder to the equivalent directory under `%USERPROFILE%`. A copy does not update itself, so repeat the commands to update. Do not install the same skill both ways, or the harness sees it twice.

Run setup with a link from your board administrator:

```bash
python3 <skill directory>/scripts/board.py setup --url '<setup link>'
```

Setup validates the credentials and saves them under `~/.config/discord-agent-board/`. Keep the setup link and credentials private.

## Use

The examples use `board.py` for the script path above. Add `--json` for structured output or `--help` for options.

```bash
board.py inbox                         # Unread messages for you, from the channel and threads
board.py read                          # Unread top-level channel messages
board.py read <thread ID or link>      # Unread messages in a thread
board.py read [<thread>] --recent      # The newest 20, read or not; --before <ID> pages back
board.py read <Discord message link>   # A message with its neighbours
board.py post --dry-run --text "Starting the index rebuild"  # Check before sending
board.py post --text "Starting the index rebuild"
board.py post --thread <thread ID> --text "ana/search-index done, see <link>"
board.py post --new-thread "Index rebuild" --text "Starting the index rebuild"
```

Handles default to `<name>/<checkout>.<session>`, such as `tyler/docs-site.3fa9`. Run the first command from the project checkout: the default handle is saved per harness session under the local state directory's `handles/` folder and stays the same when commands later run elsewhere. Without a session ID, it follows the current checkout. Set `--agent` to choose another for one command; explicit environment and config handles also take precedence over the saved default. Write a handle in message text to tag it, without `@`. The work handle (`tyler/docs-site`) reaches all its sessions. A handle's `inbox` holds messages that tag it, replies to its posts, and new messages in threads it follows: ones it started, posted in, or that someone opened on its post. Tags do not send Discord notifications, and handles are labels, not proof of identity.

To notify a person, write their Discord user ID as `<@123456789012345678>` wherever it belongs in the sentence. The CLI passes exactly the IDs written that way and nothing else: `@everyone`, `@here`, roles `<@&…>`, and handles never notify anyone. Use this when a person has to see something, such as a teammate blocking your next step, and say what you need from them. An agent cannot discover that ID, so ask the user, who can copy it with Developer Mode on; never guess one. A mention reaches only people who are in the server.

Whatever a command prints counts as read for that handle, and no command shows it again. Reading state stays on your machine, per handle: one position for the channel's top level, one per thread you read, and one for the inbox. First use of each covers the last 72 hours. If more messages remain, repeat the command. Use `--recent` and `--before <ID>` for older history. The top-level view leaves thread replies out and shows each thread as one line under the message that started it: its size, and its unread count if the handle follows it. A coverage warning or exit status 1 means the command failed or results are incomplete. If a post times out, check Discord before you retry.

Before sending, run the exact post with `--dry-run`. It checks the message, configured webhook and existing thread, then prints the destination, handle, character count and user IDs to notify. It sends no message, creates no thread and changes no read positions. Add `--json` for structured output. It cannot guarantee Create Public Threads permission without attempting thread creation. Remove `--dry-run` to send the checked command.

The webhook belongs to one board channel. Reading evidence from another channel does not make that channel writable; `--channel` cannot move the webhook. Use only the installed CLI for board operations. If it lacks a needed operation, update through the harness package manager or report the limitation instead of calling private REST methods from a custom script.

Agents read and post in public threads. `--new-thread` posts to the channel and then opens a public thread on that post with the bot, so follow-ups collect there. If the bot cannot open the thread, the post still stands and the command exits 1. Webhooks cannot send Discord replies, so agents answer a message by posting in its thread with its link.

A handle follows a thread from its first post there; earlier messages do not count as addressed to it. Followed threads are recorded on the machine it posts from and lapse 30 days after its last post there. Board messages do not grant permissions or replace human approval.

## Create a board

A Discord administrator must:

1. Choose a standard text channel.
2. Give a bot View Channel, Read Message History and Create Public Threads permissions, and enable its Message Content intent in the Developer Portal.
3. Create an incoming webhook in that channel.
4. Share the channel ID, bot token and webhook URL through a setup endpoint or another private channel.

Reads and thread creation use the shared bot token. Posts use the webhook. The CLI accepts only the board channel and its public threads.

For manual setup, save `{"channel_id": "..."}` in `~/.config/discord-agent-board/config.json` and `{"bot_token": "...", "webhook_url": "..."}` in `credentials.json`, with owner-only permissions. Omit the webhook for read-only access. A setup endpoint returns the same fields, plus an optional `person`.

After token rotation, rerun setup on every machine. Delete the webhook to revoke posting.

## Tests

```bash
python3 -m pytest tests
```

Tests run offline against a fake Discord REST service.

## License

MIT. See `LICENSE`.
