"""Cross-validate the Python `nhash` port against WSJT-X's canonical C.

Reference vectors are produced by compiling
`/tmp/wsjtx-mirror/lib/wsprcode/nhash.c` (the unmasked variant — the
``lib/wsprd/nhash.c`` copy in WSJT-X has a 15-bit mask baked into the
return) with seed 146 against the inputs below.  Build instructions
are in `_nhash_ref.c.txt` next to this file for re-validation when
upstream's algorithm changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from callhash._nhash import (
    MASK10,
    MASK12,
    MASK15,
    MASK22,
    WSJTX_INITVAL,
    hash10,
    hash12,
    hash15,
    hash22,
    nhash,
)


# (input, expected unmasked 32-bit nhash with seed=146).  Generated
# from the canonical lib/wsprcode/nhash.c.  Includes 11/12/13-byte
# inputs that exercise the final-block branches.
CANONICAL_VECTORS = [
    ("K1ABC",         1916672377),
    ("K1ABC/QRP",     1929662631),
    ("VE3/K1ABC",     2619248985),
    ("G0XYZ",         1921121372),
    ("AC0G",          3760672000),
    ("PA0SKT/2",      3787176897),
    ("VK6PG",         254697905),
    ("JA1AAA",        3055449057),
    ("WA9WTK",        3151482076),
    ("ON4UN",         2093051374),
    ("K1JT",          1743927727),
    ("AB1CDE",        3892971890),
    ("<...>",         2734662379),
    ("RR73",          1722024420),
    ("CQ",            4127026124),
    ("0",             1429153227),
    ("1A",            195575650),
    ("ABC",           605543192),
    ("AC0G/M",        82780779),
    ("12345",         3479070102),
    ("ABCDEFGHIJK",   1237789768),    # 11 bytes — case-11 final block
    ("ABCDEFGHIJKL",  2776114584),    # 12 bytes — case-12 final block
    ("ABCDEFGHIJKLM", 4189178763),    # 13 bytes — loop iter + case-1
]


# 15-bit masked outputs from the wsprd-side variant (lib/wsprd/nhash.c
# with its built-in `c = 32767 & c` line) — confirms our hash15()
# matches the WSPR Type 3 packet field width.
WSPRD_H15_VECTORS = [
    ("K1ABC",      6521),
    ("K1ABC/QRP",  20647),
    ("VE3/K1ABC",  4441),
    ("G0XYZ",      31836),
    ("AC0G",       19712),
    ("PA0SKT/2",   15297),
    ("VK6PG",      25009),
    ("JA1AAA",     29665),
    ("RR73",       484),
    ("CQ",         27596),
]


class TestSeed:

    def test_default_seed_is_146(self):
        assert WSJTX_INITVAL == 146

    def test_alternate_seed_changes_output(self):
        """Sanity: a different seed should change the hash."""
        assert nhash("K1ABC", initval=42) != nhash("K1ABC", initval=146)


@pytest.mark.parametrize("call,expected", CANONICAL_VECTORS)
def test_unmasked_nhash_matches_canonical_c(call: str, expected: int):
    assert nhash(call) == expected
    # Bytes input should match string input.
    assert nhash(call.encode("ascii")) == expected


@pytest.mark.parametrize("call,expected", WSPRD_H15_VECTORS)
def test_hash15_matches_wsprd_side_c(call: str, expected: int):
    """hash15(call) should match what wsprd's lib/wsprd/nhash.c
    returns (it has the 15-bit mask baked in, so its return value
    equals nhash(call) & 0x7FFF — what hash15() computes)."""
    assert hash15(call) == expected


class TestHashWidths:
    """Each width is just the canonical hash masked to N bits."""

    def test_h22_is_low_22_bits(self):
        for call, h32 in CANONICAL_VECTORS:
            assert hash22(call) == (h32 & MASK22)

    def test_h15_is_low_15_bits(self):
        for call, h32 in CANONICAL_VECTORS:
            assert hash15(call) == (h32 & MASK15)

    def test_h12_is_low_12_bits(self):
        for call, h32 in CANONICAL_VECTORS:
            assert hash12(call) == (h32 & MASK12)

    def test_h10_is_low_10_bits(self):
        for call, h32 in CANONICAL_VECTORS:
            assert hash10(call) == (h32 & MASK10)


class TestEdgeCases:

    def test_empty_string(self):
        # Empty input bypasses both loop and final mixing — returns
        # the initial c verbatim.  Per nhash.c:
        #   c = 0xdeadbeef + 0 + 146 = 0xDEADBF81 = 3735928961
        assert nhash("") == (0xDEADBEEF + 0 + 146) & 0xFFFFFFFF

    def test_single_char_inputs_distinct(self):
        # Different single-char inputs MUST hash to different values
        # (no length collision in the mixing).
        h_a = nhash("A")
        h_b = nhash("B")
        h_z = nhash("Z")
        assert len({h_a, h_b, h_z}) == 3

    def test_bytes_and_str_agree(self):
        for call, _ in CANONICAL_VECTORS[:5]:
            assert nhash(call) == nhash(call.encode())
