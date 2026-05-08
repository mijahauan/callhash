"""``CallHashTable`` — accumulator that maps WSJT-X callsign hashes
back to plaintext from ``<call>`` announcement markers.

Two consumers (psk-recorder, wsprdaemon-client) and the same
mechanism: when a compound callsign is first transmitted, both ends
broadcast it in plaintext between angle brackets — ``<K1ABC/QRP>``;
subsequent packets use a 22-bit (FT8/FT4) or 15-bit (WSPR) hash, and
each side resolves the hash via its in-memory table.  Per-invocation
decoders (jt9, wsprd) start with an empty table, so most hashed
packets surface as the literal placeholder ``<...>``.

This class reconstructs the table at the consumer side by watching
decoded text for ``<call>`` markers — the announcement is itself a
plaintext sighting we can hash and store.  Persisting the table to
JSON lets the resolution survive daemon restarts so the cumulative
mapping grows over time.

The class deliberately stays small and stateless beyond the in-memory
maps so it can be embedded in either client without dragging in an
import graph.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from ._nhash import hash15, hash22

log = logging.getLogger(__name__)


# Plaintext compound-callsign markers in decoded text:
#   - <K1ABC/QRP>   announcement (canonical case — two- or three-segment
#                   call separated by '/').
#   - <K1ABC>       a hashed call already resolved by the decoder
#                   from its session table; reading us the call back.
#   - <...>         literal "unresolved hash" placeholder (no useful
#                   content).
# The regex below matches the first two but not the literal ellipsis
# placeholder.
_BRACKET_CALL_RE = re.compile(r"<([A-Z0-9][A-Z0-9/]{1,15})>")
_LITERAL_UNRESOLVED = "<...>"


class CallHashTable:
    """Per-(client, radiod) cache mapping WSJT-X hashes → plaintext call.

    Thread-safe in the only way that matters here: ``observe`` and
    ``by_hash*`` may be called concurrently by a tailer thread + a
    parser thread without external locking.
    """

    SCHEMA_VERSION = 1     # bump when the on-disk JSON shape changes

    def __init__(self, source_path: Optional[Path] = None) -> None:
        self._source_path: Optional[Path] = (
            Path(source_path) if source_path is not None else None
        )
        self._lock = threading.Lock()
        # Two parallel maps (one per protocol).  Same plaintext call
        # is in both — the cost of double-storage is trivial for the
        # observed table sizes (10²-10³ entries) and avoids per-lookup
        # bit-mask choices on the hot path.
        self._by_h22: Dict[int, str] = {}
        self._by_h15: Dict[int, str] = {}
        # First-seen timestamp per call — useful for operator forensics
        # ("when did this compound call first appear?") and bounded
        # eviction in future versions.
        self._first_seen: Dict[str, str] = {}
        self._observations = 0       # total announcements seen (for stats)
        self._dirty = False          # save() can short-circuit when no change

    # ----- observation -----

    def observe(self, text: str) -> int:
        """Scan ``text`` for ``<call>`` markers and add each to the table.

        Returns the number of NEW entries inserted by this call.
        Multiple calls in one line are all extracted; existing entries
        are left unchanged (announcements are stable per call).
        """
        added = 0
        with self._lock:
            for match in _BRACKET_CALL_RE.findall(text):
                call = match.strip()
                if not call:
                    continue
                if not _looks_like_callsign(call):
                    continue
                self._observations += 1
                if self._add_locked(call):
                    added += 1
        if added:
            log.debug("callhash: +%d new call(s); table now %d (h22) / %d (h15)",
                      added, len(self._by_h22), len(self._by_h15))
        return added

    def add(self, call: str) -> bool:
        """Direct-add a single plaintext callsign.  Returns True if new."""
        with self._lock:
            return self._add_locked(call)

    def _add_locked(self, call: str) -> bool:
        h22 = hash22(call)
        h15 = hash15(call)
        # Collisions are real but rare; a later call that hashes to the
        # same h22 / h15 supersedes the earlier one (matches WSJT-X's
        # session-table "last writer wins" semantics).
        if (
            self._by_h22.get(h22) == call
            and self._by_h15.get(h15) == call
        ):
            return False
        self._by_h22[h22] = call
        self._by_h15[h15] = call
        self._first_seen.setdefault(
            call, datetime.now(tz=timezone.utc).isoformat()
        )
        self._dirty = True
        return True

    # ----- lookup -----

    def by_hash22(self, h: int) -> Optional[str]:
        """Resolve a 22-bit hash (FT8/FT4) to plaintext, or None."""
        with self._lock:
            return self._by_h22.get(h & 0x3FFFFF)

    def by_hash15(self, h: int) -> Optional[str]:
        """Resolve a 15-bit hash (WSPR Type 3) to plaintext, or None."""
        with self._lock:
            return self._by_h15.get(h & 0x7FFF)

    def __contains__(self, call: str) -> bool:
        with self._lock:
            return call in self._first_seen

    def __len__(self) -> int:
        with self._lock:
            return len(self._first_seen)

    @property
    def observations(self) -> int:
        """Total ``<call>`` markers observed (incl. duplicates)."""
        with self._lock:
            return self._observations

    # ----- normalisation helper -----

    @staticmethod
    def normalise_brackets(token: str) -> Optional[str]:
        """Canonicalise a possibly-bracketed callsign token.

        Per the wsprd / jt9 output convention:
          * ``<K1ABC>`` or ``<K1ABC/QRP>`` → strip brackets, return
            the inner call (it has been resolved by the decoder).
          * ``<...>`` → return ``None`` (unresolved hash; no useful
            information at this point — caller may try
            :meth:`by_hash22` / :meth:`by_hash15` if it has a hash to
            look up).
          * Anything else → return as-is.
        """
        if not token:
            return token
        t = token.strip()
        if t == _LITERAL_UNRESOLVED:
            return None
        m = _BRACKET_CALL_RE.fullmatch(t)
        if m:
            return m.group(1)
        return t

    # ----- persistence -----

    @classmethod
    def load_or_new(cls, source_path: Path | str) -> "CallHashTable":
        """Load from JSON if it exists, else return a fresh table.

        The file may be absent, empty, or contain malformed JSON
        (cleared by an operator, half-written by a previous crashed
        save) — in any of those cases we start fresh and log a
        warning.  We never delete the operator's file on load failure.
        """
        path = Path(source_path)
        instance = cls(source_path=path)
        if not path.exists():
            return instance
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            log.warning("callhash: ignoring unreadable %s (%s); starting fresh",
                        path, e)
            return instance
        if not isinstance(data, dict):
            return instance
        if data.get("schema_version") != cls.SCHEMA_VERSION:
            log.warning("callhash: %s has schema_version=%r, expected %d; "
                        "starting fresh", path,
                        data.get("schema_version"), cls.SCHEMA_VERSION)
            return instance
        for call, first_seen in (data.get("calls") or {}).items():
            if not _looks_like_callsign(call):
                continue
            instance._first_seen[call] = first_seen
            instance._by_h22[hash22(call)] = call
            instance._by_h15[hash15(call)] = call
        instance._observations = int(data.get("observations", 0))
        instance._dirty = False
        log.debug("callhash: loaded %d calls from %s", len(instance), path)
        return instance

    def save(self, path: Optional[Path | str] = None) -> None:
        """Atomically persist the table to disk.

        Writes to ``<path>.tmp`` then ``os.replace`` so a partial
        write can't corrupt an existing good file.  No-op when no
        observations have changed since the last save (or load).
        """
        target = Path(path) if path is not None else self._source_path
        if target is None:
            raise ValueError(
                "save(): no path supplied and no source_path set on table"
            )
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "saved_at": datetime.now(tz=timezone.utc).isoformat(),
                "observations": self._observations,
                "calls": dict(self._first_seen),
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(target)
        with self._lock:
            self._dirty = False

    # ----- introspection -----

    def stats(self) -> Tuple[int, int, int]:
        """Return ``(unique_calls, observations, h22_entries)`` snapshot."""
        with self._lock:
            return len(self._first_seen), self._observations, len(self._by_h22)

    def calls(self) -> list[str]:
        """Return a snapshot list of known plaintext calls."""
        with self._lock:
            return sorted(self._first_seen)


# ── helpers ────────────────────────────────────────────────────────────────

# Loose callsign sanity check — both standard ITU calls and compound
# variants we care about.  We let through anything that's mostly
# alphanumerics with at most a few '/' separators; the decoder's own
# bracket markers already filter out garbage upstream.
_CALLSIGN_VALIDATOR = re.compile(
    r"^[A-Z0-9](?:[A-Z0-9/]{0,15})$"
)


def _looks_like_callsign(call: str) -> bool:
    return bool(_CALLSIGN_VALIDATOR.fullmatch(call))
