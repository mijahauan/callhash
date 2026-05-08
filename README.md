# callhash

Pure-Python WSJT-X compound-callsign hash resolution.

WSJT-X's FT4/FT8/WSPR packet formats compress compound callsigns
(e.g. `K1ABC/QRP`, `VE3/K1ABC`) into 22-bit (FT8/FT4) or 15-bit
(WSPR Type 3) hashes. Receivers maintain a session table that maps
the hash back to plaintext when the call has been observed in a
`<call>` first-occurrence message. Both `jt9` and `wsprd` use the
same hash function — Bob Jenkins lookup3, seed 146.

When a per-slot decoder invocation starts with an empty session
table (the typical case for `psk-recorder`, `wsprdaemon-client`,
or any consumer that drives the decoder one cycle at a time), most
hashed packets surface as the literal `<...>` placeholder. This
library reconstructs the table on the consumer side from the same
announcement markers WSJT-X uses.

## Install

```
pip install callhash
```

Or from a sibling checkout (HamSCI deployment pattern):

```
pip install -e /opt/git/sigmond/callhash
```

## Usage

```python
from callhash import CallHashTable, hash22, hash15, nhash

# Persistent per-station cache.
table = CallHashTable.load_or_new("/var/lib/myclient/callhash.json")

# Feed any text containing <call> markers; the table extracts them.
table.observe("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8")

# Look a hash up if you happen to have one.
table.by_hash22(hash22("K1ABC/QRP"))   # → "K1ABC/QRP"
table.by_hash15(hash15("K1ABC/QRP"))   # → "K1ABC/QRP"

# Normalise a token at parse time (no instance needed).
CallHashTable.normalise_brackets("<K1ABC/QRP>")  # → "K1ABC/QRP"
CallHashTable.normalise_brackets("<...>")        # → None
CallHashTable.normalise_brackets("K1ABC")        # → "K1ABC"

# Persist for the next invocation.
table.save()
```

## Public API

| Symbol                | Purpose                                                                |
| --------------------- | ---------------------------------------------------------------------- |
| `nhash(key, initval)` | Bob Jenkins lookup3 32-bit hash. `initval=146` matches WSJT-X.         |
| `hash22(call)`        | Convenience: `nhash(call) & 0x3FFFFF` (FT8/FT4 compound-call width).   |
| `hash15(call)`        | Convenience: `nhash(call) & 0x7FFF` (WSPR Type 3 width).               |
| `hash12(call)`        | Convenience: 12-bit mask (some ARRL contest variants).                 |
| `hash10(call)`        | Convenience: 10-bit mask (narrowest WSJT-X variant).                   |
| `CallHashTable`       | Persistent accumulator + lookup + bracket-normalisation helper.        |

## Correctness

`nhash` is bit-exact against WSJT-X's canonical
`lib/wsprcode/nhash.c` (the unmasked variant — note that
`lib/wsprd/nhash.c` has a 15-bit mask baked into its return). 23
reference vectors verified, including 11/12/13-byte boundary cases
that exercise distinct final-block branches in the C code.

The `CallHashTable` covers `<call>` announcement extraction,
single-byte and multi-byte hash lookups, atomic JSON persistence
(write-tempfile + rename), corrupt-JSON / schema-mismatch
recovery, and concurrent-observe / concurrent-lookup safety. 64
tests total; no live ClickHouse / WSJT-X server required to run
them.

## Why this lives in its own repo

The hash function and bracket-resolution logic are WSJT-X's, not
sigmond's. They're useful for any FT8/FT4/WSPR consumer — the
sigmond client suite is the primary user today, but a future
`hs-uploader` library or any independent log analyser will
benefit equally. Pure stdlib, no runtime deps, ~200 lines of
code; small enough to live cleanly on its own.

## License

MIT.
