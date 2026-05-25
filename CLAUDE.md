# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**callhash** is a pure-Python implementation of WSJT-X's
compound-callsign hash mechanism — Bob Jenkins lookup3 with seed 146 —
plus a persistent per-station accumulator that resolves the hashes from
`<call>` announcement markers in decoded `jt9` / `wsprd` output.

WSJT-X's FT4 / FT8 / WSPR packet formats compress compound callsigns
(e.g. `K1ABC/QRP`, `VE3/K1ABC`) into 22-bit (FT8/FT4) or 15-bit
(WSPR Type 3) hashes. Per-slot decoder invocations that start with an
empty session table — the typical case for `psk-recorder` and
`wspr-recorder` — surface hashed packets as the literal `<...>`
placeholder. This library reconstructs the table on the consumer side.

Part of the HamSCI sigmond suite — see `/opt/git/sigmond/sigmond/CLAUDE.md`
(orchestrator) and `/opt/git/sigmond/CLAUDE.md` (umbrella) for
cross-repo context. This is a pure library with **no runtime
dependencies** — see `pyproject.toml`'s empty `dependencies = []`.

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/mijahauan/callhash
- `nhash` is a port of WSJT-X's canonical `lib/wsprcode/nhash.c`
  (Bob Jenkins's lookup3, public domain).

## Commands

```bash
# Development
uv sync --extra dev
uv run pytest tests/                    # ~64 tests; no live ClickHouse / WSJT-X
uv run pytest tests/test_nhash.py -v    # one file
uv run pytest -k bracket -v             # by keyword

# Install (consumers typically resolve via [tool.uv.sources] editable path)
pip install callhash                    # PyPI
pip install -e /opt/git/sigmond/callhash  # sibling editable (HamSCI pattern)
```

## Public API

| Symbol | Purpose |
|---|---|
| `nhash(key, initval)` | Bob Jenkins lookup3 32-bit hash. `initval=146` matches WSJT-X. |
| `hash22(call)` | `nhash(call) & 0x3FFFFF` — FT8 / FT4 compound-call width. |
| `hash15(call)` | `nhash(call) & 0x7FFF` — WSPR Type 3 width. |
| `hash12(call)` | 12-bit mask (some ARRL contest variants). |
| `hash10(call)` | 10-bit mask (narrowest WSJT-X variant). |
| `CallHashTable` | Persistent accumulator + lookup + bracket-normalisation helper. |

All public symbols are re-exported from `callhash/__init__.py`.

## Project structure

```
src/callhash/
  _nhash.py     # Bob Jenkins lookup3 port (final-block branches included).
  table.py      # CallHashTable: persistence, observe, lookup, normalisation.
  __init__.py   # public API surface.
tests/          # ~64 tests across two files; covers nhash bit-exactness +
                #   table observe/lookup/persistence/corruption recovery.
```

## Correctness invariants

- `nhash` is **bit-exact** against WSJT-X's canonical
  `lib/wsprcode/nhash.c` (the unmasked variant — note that
  `lib/wsprd/nhash.c` has a 15-bit mask baked into its return). 23
  reference vectors verified, including 11 / 12 / 13-byte boundary
  cases that exercise distinct final-block branches in the C code.
- `CallHashTable` uses **atomic JSON persistence** (write tempfile +
  rename) and degrades safely on corrupt JSON / schema mismatch by
  rebuilding the table rather than refusing to start.
- Concurrent `observe()` / `by_hash22()` / `by_hash15()` from multiple
  threads is safe.

When extending the library, the test suite expects:

1. Any change to `_nhash.py` keeps the bit-exactness vectors green.
   They are the contract between this library and decoded WSJT-X
   output — drift here means downstream consumers can't resolve
   hashes from announcement markers.
2. New `hashN` masks should follow the existing convention
   (`nhash(call) & mask`).

## Consumers

- `wspr-recorder/wspr_recorder/callsign_db.py` — persistent
  cross-decoder hash resolution; the persistent table lives at
  `/var/lib/wsprdaemon-client/callhash/wspr-callhash.json`.
- `psk-recorder` — vendored as a sibling dep via
  `[tool.uv.sources]` (used for FT8/FT4 hash resolution paths).

## Library lockfile policy

`uv.lock` for libraries doesn't bind downstream consumers. Each
consumer pins callhash via its own `uv.lock` (and via
`[tool.uv.sources]` editable path during dev).
