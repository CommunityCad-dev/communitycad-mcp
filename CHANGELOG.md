# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.2] - 2026-11-15

### Removed

- The daily upload cap. Uploads no longer check how many are left before sending files, and results and messages no longer report uploads left today; a missing or null `uploads_left_today` from the API is ignored.

### Changed

- An HTTP 429 from the API (rate limited or limit reached) passes on the API's own message and reset time, and tells the agent not to retry.

## [0.1.1] - 2026-11-15

### Added

- One-click sign-in: `link_account` opens the approval page in your default browser (not over SSH, on a Linux machine without a display, or with `COMMUNITYCAD_NO_BROWSER=1`) and its answer starts with a clickable "Approve CommunityCAD sign-in" link, with the code as a fallback. The approval is picked up in the background and saved, so the next tool call is signed in without telling the agent you're done. Asking again while it waits returns the same link; `account_status` shows a sign-in that's waiting. Once approved: "Signed in as <username>. You can close the browser tab."
- `install`, `uninstall` and `doctor` now also cover OpenCode, Codex, Gemini CLI, GitHub Copilot CLI and Zed, the same set of apps SolidCraft's agent setup configures. Codex's `config.toml` is edited in place.
- README: an Apps table, and how to use CommunityCAD inside SolidCraft's in-app chat.

### Changed

- Config files may contain comments and trailing commas (JSONC). Only the `communitycad` entry is changed; every other byte stays as it was.
- An app counts as installed when its config folder exists. Having `claude` on the PATH is no longer enough for Claude Code.
- `install` keeps settings you changed in the entry, such as OpenCode's `enabled` or Copilot's `tools`. `doctor` reports a server that's turned off.

## [0.1.0] - 2026-11-15

First public release.

### Added

- Local stdio MCP server, run with `uvx communitycad-mcp`.
- Account: `account_status`, `link_account` (device-code sign-in, optional upload permission) and `unlink_account`.
- Discovery: `search_models` (keyword or semantic, with size, file type, CAD source, tag, licence, company and creator filters), `get_model`, `show_images`, `find_similar`, `get_company`, `search_companies`, `get_creator`.
- Datasheets: `list_datasheets`, `read_datasheet`, `search_datasheet`.
- Community (read-only): `get_comments`, `get_scores`.
- Downloads: `check_download` and `download_file`, counted exactly like downloads on communitycad.dev, with the model's licence and attribution line.
- Uploads, each only after the user's explicit yes: `upload_model`, `upload_version`, `add_images`, `add_datasheets`, `edit_model`.
- Prompts `find-a-part` and `review-a-model`; resources for model READMEs and datasheets.
- Claude Code plugin marketplace in this repository, with a `find-a-part` skill.
- Claude Desktop extension (`.mcpb`) attached to each GitHub release.

[Unreleased]: https://github.com/CommunityCad-dev/communitycad-mcp/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/CommunityCad-dev/communitycad-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/CommunityCad-dev/communitycad-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/CommunityCad-dev/communitycad-mcp/releases/tag/v0.1.0
