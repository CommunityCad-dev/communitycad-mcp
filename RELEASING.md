# Releasing

Releases are published by GitHub Actions ([`release.yml`](.github/workflows/release.yml)) when a `v*` tag is pushed. PyPI uses [trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token is stored anywhere.

## One-time setup

Do these once, before the first release.

1. **PyPI pending publisher.** Sign in at [pypi.org](https://pypi.org), open *Your account → Publishing*, and under *Add a new pending publisher* choose *GitHub* and enter:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `communitycad-mcp` |
   | Owner | `CommunityCad-dev` |
   | Repository name | `communitycad-mcp` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   The first successful publish creates the project and turns the pending publisher into a normal one.

2. **GitHub environment.** In the repository, *Settings → Environments → New environment*, name it `pypi`. Optionally add yourself under *Required reviewers* so every publish waits for an approval click, and restrict it to tags matching `v*`.

## Cutting a release

1. Pick the version (semantic versioning: `0.1.1` for fixes, `0.2.0` for new tools or behaviour).
2. Set it in every place that names it:
   - `pyproject.toml` (`version`) and `src/communitycad_mcp/__init__.py` (`__version__`), then run `uv lock`
   - `mcpb/manifest.json` (`version`) and `mcpb/pyproject.toml` (`version` and the `communitycad-mcp==` pin)
   - `plugins/communitycad/.claude-plugin/plugin.json` (`version`)
3. In `CHANGELOG.md`, move the *Unreleased* entries under a new `## [x.y.z] - YYYY-MM-DD` heading and update the compare links at the bottom.
4. Check locally:

   ```bash
   uv run pytest
   rm -rf dist && uv build && uvx twine check --strict dist/*
   ```

5. Commit, tag and push:

   ```bash
   git commit -am "Release x.y.z"
   git tag vx.y.z
   git push origin main vx.y.z
   ```

The workflow checks that the tag matches the package version, runs the tests, builds the wheel and sdist, publishes them to PyPI, builds the Claude Desktop extension (`communitycad-mcp-x.y.z.mcpb`), and creates a GitHub release with the changelog section as notes and all of those files attached.

If the publish step fails after PyPI accepted the files, don't reuse the version: PyPI never accepts the same file name twice. Fix the problem and release the next patch version.
