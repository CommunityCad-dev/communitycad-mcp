# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/CommunityCad-dev/communitycad-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/CommunityCad-dev/communitycad-mcp/releases/tag/v0.1.0
