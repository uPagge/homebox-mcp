# Contributing

Thanks for your interest in improving `homebox-mcp`. The fork retains upstream tools and adds small typed-model,
runtime and OAuth resource-verification modules. Keep changes focused and tested.

## Development setup

You need [`uv`](https://docs.astral.sh/uv/). Use `uv sync --frozen` for the checked lock, then `uv run --frozen pytest`.
Deployment requirements are exported with hashes from `uv.lock`; update them with
`uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock`.
The PEP 723 clone entrypoint remains available, but installing the package includes
all sibling modules reliably.

```bash
git clone https://github.com/uPagge/homebox-mcp
cd homebox-mcp
```

## Tests

Tests use `pytest` with [`respx`](https://lundberg.github.io/respx/) to mock the
Homebox HTTP API — **no live instance is required or should be used** for the
suite:

```bash
uv run --with pytest --with respx --with . pytest
```

> **Stale-cache trap:** `--with .` caches the built package keyed on the
> version string. If you edit `homebox_mcp.py` without bumping `__version__`,
> pytest may import the stale cached build and miss your changes. Force a
> rebuild with:
>
> ```bash
> uv run --refresh-package homebox-mcp --reinstall-package homebox-mcp \
>   --with pytest --with respx --with . pytest
> ```

## Lint

Code is linted with [`ruff`](https://docs.astral.sh/ruff/) (config in
`pyproject.toml`; line length 100):

```bash
uv run --with ruff ruff check .
```

## Pull requests

- **Add tests for behavior changes.** The full-body-PUT preserve logic and the
  typed custom-field handling are exactly the kind of thing that regresses
  silently — cover new or changed behavior with a mocked-httpx test.
- **Update [`CHANGELOG.md`](CHANGELOG.md)** under the `Unreleased` section
  ([Keep a Changelog](https://keepachangelog.com/) format).
- **Keep it lint-clean** (`ruff check .` passes).
- **Keep tool docstrings accurate** — they are the source of truth for the tool
  reference in the README and for how MCP clients present each tool.

## Testing against a live Homebox instance

If you validate a change against a real Homebox instance, **use disposable data
only** — create clearly-marked throwaway items/locations and delete them
afterward. Never test against inventory you care about, and never point the
suite or ad-hoc scripts at a production instance's real data.

By contributing you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
