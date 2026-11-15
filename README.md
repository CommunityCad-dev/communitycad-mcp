# communitycad-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents use [CommunityCAD](https://communitycad.dev) the way a person does: search real, dimensionally accurate parts, read their datasheets, comments and accuracy scores, and download the CAD files into a design.

> **Status: in development.** Not on PyPI yet.

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

You need [uv](https://docs.astral.sh/uv/). The package isn't on PyPI yet; until it is, run it from a checkout of this repository by replacing `uvx communitycad-mcp` below with `uv run --directory /path/to/communitycad-mcp communitycad-mcp`.

**Claude Code**

```bash
claude mcp add communitycad -- uvx communitycad-mcp
```

**Claude Desktop** (Settings, Developer, Edit Config, then restart the app):

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

Other MCP clients: run `uvx communitycad-mcp` as a stdio server.

**Check it with MCP Inspector:**

```bash
npx @modelcontextprotocol/inspector uvx communitycad-mcp
```

**Sign in** by asking your agent to "link my CommunityCAD account". It shows a short code and a link; approve it on communitycad.dev and ask the agent to finish. The connection is named after the AI app and the computer (e.g. "Claude Code on DESKTOP-ABC"), so you can tell several apart in your account. Searching, reading and downloads within the signed-out limit work without signing in.

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
- **Uploads (once the CommunityCAD API offers them):** `upload_model` (a file or folder, public or private), `upload_version`, `add_images`, `add_datasheets`, `edit_model`. They need your permission once (`link_account` with `allow_uploads=true`), follow the website's daily upload limit (checked before any file is sent, so a refused upload sends nothing) and moderation, and can't delete anything. The agent asks before every upload and lets you choose the visibility, licence and 18+ setting.

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

Apache-2.0. See [LICENSE](LICENSE). Models on CommunityCAD carry their own licences, set by their creators.
