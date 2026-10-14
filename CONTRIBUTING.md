# Contributing

Thanks for your interest in Neondeck. Bug reports and pull requests are welcome.

## Setup

```bash
uv sync
uv run pytest          # all tests must pass
uv run neondeck run    # run the agent from the checkout
```

Python 3.12+ and macOS are required to run the agent; the tests also run elsewhere.

## Guidelines

- Keep changes small and focused; add or update tests for behavior changes.
- Code comments are in English and sparse. User-facing UI strings are currently Italian; a translation layer is on the roadmap.
- The agent is security-sensitive: never widen what a device can do (it may only list, focus and press predefined buttons), and keep the LAN listener limited to trusted networks.
- If you change `src/neondeck/static/preview.html`, run `python scripts/build_demo.py` to refresh `docs/demo.html` (a test checks it).
- Do not commit personal data (hostnames, MAC addresses, tokens, real project names) in code, tests or screenshots.

## Reporting bugs

Use the bug report template and include `neondeck status` output (redact addresses you do not want to share).
