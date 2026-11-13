# communitycad-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents use [CommunityCAD](https://communitycad.dev) the way a person does: search real, dimensionally accurate parts, read their datasheets, comments and accuracy scores, and download the CAD files into a design.

> **Status: in development.** Nothing is published yet. The tool list below is the plan, not a finished feature set.

## What it's for

You're designing an enclosure and need a 24 V power supply that fits. Ask your agent, and it can find one with a STEP file, read the mounting-hole spacing from its datasheet, check what other people said about the fit, then save the file where your CAD tool can insert it.

It works with any MCP-capable agent and any CAD tool that can open the downloaded files.

## Two ways to run it

| | Local (stdio) | Hosted (Streamable HTTP) |
|---|---|---|
| Install | `uvx communitycad-mcp` (planned) | Nothing; add the connector URL (planned) |
| Downloads | Saved to a folder on your machine | A download link |
| Sign-in | Device code, like the CommunityCAD Klipper plugin | OAuth on connect |

## Tools (planned)

- **Account:** `account_status`, `link_account`, `unlink_account`
- **Discovery:** `search_models`, `get_model`, `find_similar`, `get_company`, `search_companies`, `get_creator`
- **Datasheets:** `list_datasheets`, `read_datasheet`, `search_datasheet`
- **Community (read-only):** `get_comments`, `get_scores`
- **Download:** `check_download`, `download_file`

## Downloads and limits

Downloads through this server follow exactly the same rules as downloading on communitycad.dev: the same plan limits, counted against your account, with the same free re-download of a model on the same day. Signed out, you get the same limit as a signed-out visitor to the site. See the [plans page](https://communitycad.dev/subscribe).

Every download comes with the model's licence and, where the licence needs one, an attribution line.

## Privacy and safety

- Your sign-in token is stored in your user config directory, readable only by you, and never appears in tool output or logs.
- The server sees only what your account can see on the site.
- Comments, READMEs and datasheet text are written by other people. The server hands them to the agent as marked, untrusted content, never as instructions.
- Agents can't post comments or ratings through this server.

## Licence

Apache-2.0. See [LICENSE](LICENSE). Models on CommunityCAD carry their own licences, set by their creators.
