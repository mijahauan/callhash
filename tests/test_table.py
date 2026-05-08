"""Tests for ``callhash.CallHashTable``.

Covers:
  * `<call>` announcement parsing from decoded text — single, multi,
    bracketed-with-suffix, the literal `<...>` placeholder.
  * Hash-based lookups (h22 + h15) round-trip.
  * `normalise_brackets` static helper for token-level cleanup.
  * Persistence: load → observe → save → reload → state preserved.
  * Schema-version mismatch and corrupt-JSON cases start fresh
    without losing the operator's file.
  * Thread safety: concurrent observe + lookup doesn't corrupt.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from callhash import CallHashTable, hash15, hash22


class TestObserve:

    def test_extracts_single_announcement(self):
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> CQ FT8")
        assert added == 1
        assert "K1ABC/QRP" in t
        assert t.by_hash22(hash22("K1ABC/QRP")) == "K1ABC/QRP"
        assert t.by_hash15(hash15("K1ABC/QRP")) == "K1ABC/QRP"

    def test_multiple_announcements_in_one_line(self):
        # Both ends of a QSO each in brackets — typical of an
        # exchange where both calls are non-standard.
        t = CallHashTable()
        added = t.observe(
            "260507 1234 -12 +0.45 1250 ` <K1ABC/QRP> <VE3/W1XYZ> 73 FT8"
        )
        assert added == 2
        assert "K1ABC/QRP" in t and "VE3/W1XYZ" in t

    def test_duplicate_announcement_only_adds_once(self):
        t = CallHashTable()
        first  = t.observe("foo <K1ABC/QRP> bar")
        second = t.observe("foo <K1ABC/QRP> baz")
        assert first == 1
        assert second == 0          # already known
        assert len(t) == 1
        # observations counter still reflects both sightings:
        assert t.observations == 2

    def test_literal_unresolved_placeholder_is_skipped(self):
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <...> 73 FT8")
        assert added == 0
        assert len(t) == 0

    def test_bracketed_standard_call_still_extracted(self):
        # Even a standard non-compound call may appear in brackets
        # when the decoder resolved it from a hash.  We treat that
        # exactly like an announcement and store it.
        t = CallHashTable()
        added = t.observe("260507 1234 -12 +0.45 1250 ` <K1JT> CQ FT8")
        assert added == 1
        assert "K1JT" in t

    def test_garbage_and_lowercase_not_treated_as_calls(self):
        t = CallHashTable()
        added = t.observe("foo <hello> <abc!@#> bar")
        assert added == 0
        assert len(t) == 0


class TestNormaliseBrackets:

    def test_strips_brackets_around_call(self):
        assert CallHashTable.normalise_brackets("<K1ABC>") == "K1ABC"

    def test_strips_brackets_around_compound_call(self):
        assert CallHashTable.normalise_brackets("<K1ABC/QRP>") == "K1ABC/QRP"

    def test_unresolved_returns_none(self):
        assert CallHashTable.normalise_brackets("<...>") is None

    def test_passthrough_for_non_bracketed(self):
        assert CallHashTable.normalise_brackets("K1ABC") == "K1ABC"
        assert CallHashTable.normalise_brackets("CQ") == "CQ"

    def test_handles_whitespace(self):
        assert CallHashTable.normalise_brackets("  <K1ABC>  ") == "K1ABC"


class TestLookup:

    def test_lookup_with_extraneous_high_bits_masks_correctly(self):
        """by_hash22 should mask its argument to 22 bits before lookup."""
        t = CallHashTable()
        t.add("K1ABC")
        h = hash22("K1ABC")
        # Same lookup works whether the caller passes a clean 22-bit
        # value or a 32-bit superset (e.g. the unmasked nhash output).
        assert t.by_hash22(h) == "K1ABC"
        assert t.by_hash22(h | 0xFFC00000) == "K1ABC"

    def test_unknown_hash_returns_none(self):
        t = CallHashTable()
        t.add("K1ABC")
        assert t.by_hash22(0xDEADBE & 0x3FFFFF) is None


class TestPersistence:

    def test_save_then_load_roundtrip(self, tmp_path):
        path = tmp_path / "hashtable.json"
        t = CallHashTable.load_or_new(path)
        t.observe("foo <K1ABC/QRP> bar")
        t.observe("foo <VE3/W1XYZ> bar")
        t.save()
        assert path.exists()

        # Fresh load — same content.
        t2 = CallHashTable.load_or_new(path)
        assert "K1ABC/QRP" in t2
        assert "VE3/W1XYZ" in t2
        assert t2.by_hash22(hash22("K1ABC/QRP")) == "K1ABC/QRP"
        assert t2.observations == 2

    def test_save_no_op_when_unchanged(self, tmp_path):
        """Saving a clean table should be a no-op (no file write)."""
        path = tmp_path / "hashtable.json"
        # Initial save with one call.
        t = CallHashTable.load_or_new(path)
        t.observe("<K1ABC/QRP>")
        t.save()
        mtime_initial = path.stat().st_mtime_ns

        # Reload (clean) and save again — file shouldn't be rewritten.
        t2 = CallHashTable.load_or_new(path)
        t2.save()
        assert path.stat().st_mtime_ns == mtime_initial

    def test_atomic_write_uses_tempfile(self, tmp_path):
        """save() writes via .tmp + replace; no half-written file."""
        path = tmp_path / "hashtable.json"
        t = CallHashTable.load_or_new(path)
        t.observe("<K1ABC/QRP>")
        t.save()
        # After save, the .tmp file should NOT exist (renamed away).
        assert not (tmp_path / "hashtable.json.tmp").exists()
        assert path.exists()
        # And the JSON parses.
        data = json.loads(path.read_text())
        assert data["schema_version"] == 1
        assert "K1ABC/QRP" in data["calls"]

    def test_corrupt_json_starts_fresh_without_deleting(self, tmp_path):
        path = tmp_path / "hashtable.json"
        path.write_text("{not valid json")
        t = CallHashTable.load_or_new(path)
        assert len(t) == 0
        # Operator's file is left alone (not auto-deleted).
        assert path.read_text() == "{not valid json"

    def test_schema_mismatch_starts_fresh(self, tmp_path):
        path = tmp_path / "hashtable.json"
        path.write_text(json.dumps({
            "schema_version": 99,
            "calls": {"K1ABC/QRP": "2025-01-01T00:00:00+00:00"},
            "observations": 1,
        }))
        t = CallHashTable.load_or_new(path)
        assert len(t) == 0
        # Saving doesn't clobber yet — would only fire if dirty.

    def test_save_without_path_raises(self):
        t = CallHashTable()
        with pytest.raises(ValueError, match="no path supplied"):
            t.save()


class TestThreadSafety:

    def test_concurrent_observe_and_lookup(self):
        """Hammer the table from multiple threads; assert no corruption."""
        t = CallHashTable()
        calls = [f"K{i:04d}AA" for i in range(50)]
        # Pre-add a couple so lookups have something to find.
        for c in calls[:5]:
            t.add(c)

        stop = threading.Event()
        errors: list[str] = []

        def writer():
            for c in calls:
                if stop.is_set():
                    return
                t.observe(f"<{c}>")

        def reader():
            while not stop.is_set():
                for c in calls[:5]:
                    if t.by_hash22(hash22(c)) != c:
                        errors.append(f"lookup miss: {c}")
                        return

        threads = [
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=writer, daemon=True),
            threading.Thread(target=reader, daemon=True),
        ]
        for th in threads:
            th.start()
        # Let them race.
        threads[0].join(timeout=2.0)
        threads[1].join(timeout=2.0)
        stop.set()
        threads[2].join(timeout=2.0)

        assert not errors, errors
        # All 50 calls should be in the table.
        assert len(t) == 50


class TestStats:

    def test_stats_snapshot(self):
        t = CallHashTable()
        t.observe("<K1ABC/QRP> <VE3/W1XYZ>")
        t.observe("<K1ABC/QRP> 73")     # duplicate → adds an observation, not a call
        unique, observations, h22_entries = t.stats()
        assert unique == 2
        assert observations == 3
        assert h22_entries == 2

    def test_calls_returns_sorted_snapshot(self):
        t = CallHashTable()
        t.observe("<VE3/W1XYZ> <K1ABC/QRP> <AC0G>")
        assert t.calls() == ["AC0G", "K1ABC/QRP", "VE3/W1XYZ"]
