# communitycad-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents use [CommunityCAD](https://communitycad.dev) the way a person does: search real, dimensionally accurate parts, read their datasheets, comments and accuracy scores, and download the CAD files into a design.

> **Status: beta.** On [PyPI](https://pypi.org/project/communitycad-mcp/) as `communitycad-mcp`. Changes are listed in the [changelog](https://github.com/CommunityCad-dev/communitycad-mcp/blob/main/CHANGELOG.md).

<p align="center">
  <a href="https://youtu.be/_drNAL4Haj0"><img src="https://img.youtube.com/vi/_drNAL4Haj0/maxresdefault.jpg" width="800" alt="CommunityCAD MCP demo in SolidCraft: find a part, read its datasheet, download it and place it in a design"></a>
  <br>
  <sub>Find a part, read its datasheet, download it and place it in a design, in SolidCraft. <a href="https://youtu.be/_drNAL4Haj0">Watch the demo on YouTube</a>.</sub>
</p>

## What it's for

You're designing an enclosure and need a 24 V power supply that fits. Ask your agent, and it can find one with a STEP file, read the mounting-hole spacing from its datasheet, check what other people said about the fit, then save the file where your CAD tool can insert it.

It works with any MCP-capable agent and any CAD tool that can open the downloaded files.

## Two ways to run it

| | Local (stdio) | Hosted (Streamable HTTP) |
|---|---|---|
| Install | `uvx communitycad-mcp` | Nothing; add the connector URL (planned) |
| Downloads | Saved to a folder on your machine | A download link |
| Sign-in | Device code, like the CommunityCAD Klipper plugin | OAuth on connect |

## Setup (local)

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (it fetches Python 3.10 or newer if needed). Then run:

```bash
uvx communitycad-mcp install
```

It finds the AI apps below on Windows, macOS and Linux and adds CommunityCAD to each one. An app counts as found when its config folder exists; nothing is started to check. Only the `communitycad` entry changes: your other servers, settings and comments stay as they are, and every file it changes is backed up first (`<file>.bak-<date>`). Running it again changes nothing. Restart the app, then ask: *find me an M8 screw with a STEP file*.

### Apps

| App | `--app` | Config file it edits | Entry |
|---|---|---|---|
| Claude Code | `claude-code` | `~/.claude.json` (through `claude mcp add --scope user` when `claude` is on the PATH) | `mcpServers`, `"type": "stdio"` |
| Claude Desktop | `claude-desktop` | `claude_desktop_config.json` in `%APPDATA%\Claude` (and the Microsoft Store copy), `~/Library/Application Support/Claude`, `~/.config/Claude` | `mcpServers` |
| OpenCode | `opencode` | `~/.config/opencode/opencode.json` (or `opencode.jsonc` if that's the one you have) on every OS; `$XDG_CONFIG_HOME` if set | `mcp`, `"type": "local"`, `"command": [...]` |
| Codex | `codex` | `~/.codex/config.toml` (`$CODEX_HOME` if set) | `[mcp_servers.communitycad]` |
| Gemini CLI | `gemini` | `~/.gemini/settings.json` | `mcpServers` |
| GitHub Copilot CLI | `copilot` | `~/.copilot/mcp-config.json` | `mcpServers`, `"type": "local"`, `"tools": ["*"]` |
| Cursor | `cursor` | `~/.cursor/mcp.json` | `mcpServers` |
| VS Code (Copilot Chat) | `vscode` | user `mcp.json` in `%APPDATA%\Code\User`, `~/Library/Application Support/Code/User`, `~/.config/Code/User` | `servers`, `"type": "stdio"` |
| Windsurf | `windsurf` | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| Zed | `zed` | `settings.json` in `%APPDATA%\Zed`, or `~/.config/zed` on macOS and Linux | `context_servers` |

Claude Desktop and Zed get the full path to `uvx`, because apps started from a desktop launcher may not see your shell's PATH. If you turned the server off in an app (OpenCode's `"enabled": false`) or narrowed Copilot's `tools`, `install` keeps that and `doctor` tells you it's off.

| Command | What it does |
|---|---|
| `uvx communitycad-mcp install` | Set up every AI app it finds. `--app opencode` (or any name in the `--app` column) sets up just that one, even if it wasn't found. `--dry-run` shows what would change. |
| `uvx communitycad-mcp doctor` | Checks uv, the connection to CommunityCAD, your sign-in and which apps are set up, and says how to fix each problem. |
| `uvx communitycad-mcp uninstall` | Removes only the CommunityCAD entry from each app. |

Running `communitycad-mcp` with no command starts the server over stdio; that's how the apps launch it.

**Claude Code plugin.** Instead of `install`, you can add CommunityCAD as a plugin: the same server plus a `find-a-part` skill that tells Claude how to use it well. In Claude Code:

```text
/plugin marketplace add CommunityCad-dev/communitycad-mcp
/plugin install communitycad@communitycad
```

Then run `/reload-plugins` (or restart Claude Code). From a shell it's `claude plugin marketplace add CommunityCad-dev/communitycad-mcp` and `claude plugin install communitycad@communitycad`. Update with `/plugin marketplace update communitycad`; remove with `/plugin uninstall communitycad@communitycad`. Use either the plugin or `install` for Claude Code, not both.

**Claude Desktop extension.** Download `communitycad-mcp-<version>.mcpb` from the [latest release](https://github.com/CommunityCad-dev/communitycad-mcp/releases/latest), open it (or drag it onto Settings, Extensions) and click Install. You can choose the download folder and the largest download size there. The extension is a few kilobytes: Claude Desktop sets up Python and the `communitycad-mcp` package itself, so it doesn't need uv.

<details><summary>Set it up by hand</summary>

**Claude Code**

```bash
claude mcp add --scope user communitycad -- uvx communitycad-mcp
```

**Claude Desktop, Cursor, Windsurf** (`claude_desktop_config.json`, `~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`):

```json
{
  "mcpServers": {
    "communitycad": {
      "command": "uvx",
      "args": ["communitycad-mcp"]
    }
  }
}
```

**VS Code** (user `mcp.json`): the same entry under `"servers"`, with `"type": "stdio"`.

**Gemini CLI** (`~/.gemini/settings.json`): the same `mcpServers` entry. **GitHub Copilot CLI** (`~/.copilot/mcp-config.json`): the same, plus `"type": "local"` and `"tools": ["*"]`. **Zed** (`settings.json`): the same entry under `"context_servers"`.

**OpenCode** (`~/.config/opencode/opencode.json`):

```json
{
  "mcp": {
    "communitycad": {
      "type": "local",
      "command": ["uvx", "communitycad-mcp"],
      "enabled": true
    }
  }
}
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.communitycad]
command = "uvx"
args = ["communitycad-mcp"]
```

Other MCP clients: run `uvx communitycad-mcp` as a stdio server. To run an unreleased checkout instead, run `uv run --directory /path/to/communitycad-mcp communitycad-mcp install --from-path /path/to/communitycad-mcp`, or replace `uvx communitycad-mcp` with `uv run --directory /path/to/communitycad-mcp communitycad-mcp`.

</details>

### Use it inside SolidCraft

[SolidCraft](https://github.com/tangency-labs/solidcraft)'s in-app chat runs your own coding agent and gives it SolidCraft's tools. The agents checked below also load the MCP servers from their own config, so CommunityCAD set up for the agent is there in SolidCraft's chat too, next to SolidCraft's tools:

1. Set up your agent for SolidCraft's chat as SolidCraft's docs describe.
2. Run `uvx communitycad-mcp install` (or `--app claude-code`, `--app codex`, `--app opencode`, `--app gemini` for just that agent).
3. Start a new chat in SolidCraft, so the agent is started again and reads its config.
4. Ask: *find me an M8 screw with a STEP file*.

| Agent in SolidCraft's chat | Loads CommunityCAD from its own config |
|---|---|
| Codex (`codex-acp`) | Yes: it reads your Codex config and adds SolidCraft's server to it |
| OpenCode (`opencode acp`) | Yes: OpenCode's ACP docs say sessions use your config, MCP servers included |
| Gemini CLI | Yes: it starts from the servers in your settings and adds SolidCraft's |
| Claude Code (`claude-agent-acp`) | Expected: the adapter loads your user, project and local settings and adds SolidCraft's server to them; not yet tested end to end |
| Others (Copilot, Qwen, goose, Grok, Mistral Vibe, Antigravity) | Not checked yet |

CommunityCAD's tools are asked about through your agent's usual permission prompts. Downloads count against your limit as anywhere else; files are saved to your download folder (see [Settings](#settings)) and the agent is told where.

**Check it with MCP Inspector:**

```bash
npx @modelcontextprotocol/inspector uvx communitycad-mcp
```

**Sign in** by asking your agent to "link my CommunityCAD account". Your browser opens on the approval page (the agent also gives you the link, and a short code as a fallback); approve it on communitycad.dev and you're signed in, with no need to tell the agent. Set `COMMUNITYCAD_NO_BROWSER=1` to never open a browser; it also stays closed over SSH. The connection is named after the AI app and the computer (e.g. "Claude Code on DESKTOP-ABC"), so you can tell several apart in your account. Searching, reading and downloads within the signed-out limit work without signing in.

### Settings

All optional, as environment variables:

| Variable | Default | What it does |
|---|---|---|
| `COMMUNITYCAD_DOWNLOAD_DIR` | `~/CommunityCAD` | Files are saved to `<dir>/<model-slug>/` |
| `COMMUNITYCAD_MAX_DOWNLOAD_MB` | `500` | Larger files are refused before anything is counted |
| `COMMUNITYCAD_CONFIG_DIR` | your user config dir | Where the sign-in is kept |
| `COMMUNITYCAD_API_URL` | `https://communitycad.dev/api` | The API to talk to |
| `COMMUNITYCAD_SITE_URL` | the API URL without `/api` | The website for page links the API doesn't return itself. Set it when the API isn't served under the site's `/api` path (e.g. `http://localhost:8080`) |

## Tools

- **Account:** `account_status` (plan, downloads left and reset times, signed in or out), `link_account`, `unlink_account`
- **Discovery:** `search_models` (keywords or meaning; filters for size, file types, CAD source, tags, licence family, company, creator; sort by newest, downloads, likes, prints or accuracy), `get_model`, `show_images` (preview pictures inline; never uses a download), `find_similar`, `get_company`, `search_companies`, `get_creator`
- **Datasheets:** `list_datasheets`, `read_datasheet`, `search_datasheet`
- **Community (read-only):** `get_comments`, `get_scores`
- **Download:** `check_download`, `download_file` (optionally extracts one file from a ZIP). Works signed out, within the signed-out limit.
- **Uploads (once the CommunityCAD API offers them):** `upload_model` (a file or folder, public or private), `upload_version`, `add_images`, `add_datasheets`, `edit_model`. They need your permission once (`link_account` with `allow_uploads=true`), follow the website's moderation, and can't delete anything. The agent asks before every upload and lets you choose the visibility, licence and 18+ setting.

Prompts: `find-a-part`, `review-a-model`. Resources: `communitycad://model/{id}/readme`, `communitycad://model/{id}/datasheet/{n}`.

## Downloads and limits

Downloads through this server follow exactly the same rules as downloading on communitycad.dev: the same plan limits, counted against your account, with the same free re-download of a model on the same day. Signed out, you get the same limit as a signed-out visitor to the site. See the [plans page](https://communitycad.dev/subscribe).

Every download comes with the model's licence and, where the licence needs one, an attribution line.

## Privacy and safety

- Your sign-in token is stored in your user config directory, readable only by you, and never appears in tool output or logs.
- The server sees only what your account can see on the site.
- Comments, READMEs and datasheet text are written by other people. The server hands them to the agent as marked, untrusted content, never as instructions.
- Agents can't post comments or ratings through this server. They upload or edit models only after you say yes, and never delete anything.

## Development

```bash
uv sync
uv run pytest
```

Tests run against recorded, scrubbed API responses in `tests/fixtures/`; nothing touches the network.

## Licence

Apache-2.0. See [LICENSE](https://github.com/CommunityCad-dev/communitycad-mcp/blob/main/LICENSE). Models on CommunityCAD carry their own licences, set by their creators.
