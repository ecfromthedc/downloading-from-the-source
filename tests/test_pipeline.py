"""Unit tests for pure logic in voice-memo-pipeline.py and classify-alexandria-entry.py.

Run with:
    python3 -m pytest tests/
  or:
    python3 -m unittest discover -s tests

Covers:
  - Cocoa-epoch date conversion (the inline arithmetic in lookup_titles_from_db)
  - slugify_for_filename
  - merge_into_frontmatter (from classify-alexandria-entry.py)
  - lookup_titles_from_db using a small in-memory fixture DB (no CloudKit needed)
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Import helpers — bring in the two scripts without running their top-level
# side-effects (logging setup etc.).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(rel_path: str, module_name: str):
    """Load a repo script as a module regardless of whether it is on sys.path."""
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    # Stash in sys.modules so cross-imports work if needed.
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# We load these once at module import; if the files move we get a clear error.
pipeline = _load_module("voice-memo-pipeline.py", "voice_memo_pipeline")
classifier = _load_module("classify-alexandria-entry.py", "classify_alexandria_entry")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The Cocoa epoch offset is the only "magic number" in the date math.
_COCOA_EPOCH_OFFSET = pipeline.COCOA_EPOCH_OFFSET  # 978307200


def _cocoa_to_unix(zdate: float | None) -> float:
    """Reproduce the exact inline arithmetic from lookup_titles_from_db."""
    return float(zdate or 0) + _COCOA_EPOCH_OFFSET if zdate else 0.0


# ---------------------------------------------------------------------------
# 1. Cocoa epoch conversion
# ---------------------------------------------------------------------------


class TestCocoaEpochConversion(unittest.TestCase):
    """The ZDATE column is seconds since 2001-01-01 00:00:00 UTC.

    COCOA_EPOCH_OFFSET = 978307200 is the difference between the Unix epoch
    (1970-01-01) and the Cocoa epoch (2001-01-01).
    """

    def test_offset_value(self):
        """The constant must be exactly 978 307 200 seconds."""
        self.assertEqual(_COCOA_EPOCH_OFFSET, 978_307_200)

    def test_zero_zdate_returns_zero(self):
        """None or 0 zdate → 0.0 (caller falls back to mtime)."""
        self.assertEqual(_cocoa_to_unix(None), 0.0)
        self.assertEqual(_cocoa_to_unix(0), 0.0)

    def test_known_date(self):
        """2024-06-15 12:00:00 UTC expressed as Cocoa seconds → correct Unix timestamp.

        Unix: datetime(2024,6,15,12,0,0, tzinfo=UTC).timestamp() = 1718445600
        Cocoa zdate = 1718445600 - 978307200 = 740138400
        """
        cocoa_zdate = 740_138_400.0
        expected_unix = 1_718_445_600.0
        result = _cocoa_to_unix(cocoa_zdate)
        self.assertAlmostEqual(result, expected_unix, places=0)
        dt = datetime.fromtimestamp(result, tz=timezone.utc)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 6)
        self.assertEqual(dt.day, 15)

    def test_cocoa_epoch_is_2001(self):
        """zdate=0 would represent 2001-01-01 00:00:00 UTC if treated as Unix time,
        but our function guards against it by returning 0.0 (not +978307200).
        This test confirms the guard: a fractional positive zdate ≠ 0 IS converted."""
        tiny_positive = 1.0  # 1 second into Cocoa time (2001-01-01 00:00:01 UTC)
        result = _cocoa_to_unix(tiny_positive)
        expected = _COCOA_EPOCH_OFFSET + 1
        self.assertEqual(result, expected)


# ---------------------------------------------------------------------------
# 2. slugify_for_filename
# ---------------------------------------------------------------------------


class TestSlugifyForFilename(unittest.TestCase):

    def test_plain_ascii(self):
        self.assertEqual(pipeline.slugify_for_filename("Hello World"), "Hello World")

    def test_strips_special_chars(self):
        result = pipeline.slugify_for_filename("Ideas: (2024) / top & bottom!")
        # Colons, parens, slashes, ampersands, exclamations are removed.
        self.assertNotIn(":", result)
        self.assertNotIn("/", result)
        self.assertNotIn("!", result)
        self.assertNotIn("(", result)
        self.assertNotIn("&", result)
        # Spaces should be preserved (collapsed to single space).
        self.assertIn("Ideas", result)

    def test_empty_string_returns_fallback(self):
        self.assertEqual(pipeline.slugify_for_filename(""), "Untitled Memo")

    def test_whitespace_only_returns_fallback(self):
        self.assertEqual(pipeline.slugify_for_filename("   "), "Untitled Memo")

    def test_unicode_word_chars_preserved(self):
        # Letters, digits, underscores are "word" characters in re.UNICODE.
        result = pipeline.slugify_for_filename("Café idea_42")
        self.assertIn("Caf", result)
        self.assertIn("42", result)

    def test_truncated_at_120_chars(self):
        long_title = "A" * 200
        result = pipeline.slugify_for_filename(long_title)
        self.assertLessEqual(len(result), 120)

    def test_collapses_internal_whitespace(self):
        result = pipeline.slugify_for_filename("hello   world")
        self.assertEqual(result, "hello world")

    def test_hyphens_preserved(self):
        # Hyphens are allowed by the regex ([^\w\s\-]).
        result = pipeline.slugify_for_filename("rising-tides recap")
        self.assertIn("-", result)

    def test_strips_leading_trailing_whitespace(self):
        result = pipeline.slugify_for_filename("  memo title  ")
        self.assertEqual(result, "memo title")


# ---------------------------------------------------------------------------
# 3. merge_into_frontmatter (from classify-alexandria-entry.py)
# ---------------------------------------------------------------------------


class TestMergeIntoFrontmatter(unittest.TestCase):
    """Tests for the frontmatter key-upsert helper."""

    def test_insert_new_key_into_empty_fm(self):
        result = classifier.merge_into_frontmatter(None, {"context": "rising-tides"})
        self.assertIn("context: rising-tides", result)

    def test_insert_key_into_existing_fm(self):
        fm = "date: 2024-01-01\nsource: voice"
        result = classifier.merge_into_frontmatter(fm, {"context": "personal"})
        self.assertIn("context: personal", result)
        self.assertIn("date: 2024-01-01", result)

    def test_update_existing_key(self):
        fm = "date: 2024-01-01\ncontext: personal"
        result = classifier.merge_into_frontmatter(fm, {"context": "rising-tides"})
        # Only one context line, updated.
        lines = [l for l in result.splitlines() if l.startswith("context:")]
        self.assertEqual(len(lines), 1)
        self.assertIn("rising-tides", lines[0])

    def test_multiple_updates_at_once(self):
        fm = "date: 2024-01-01"
        result = classifier.merge_into_frontmatter(
            fm,
            {"context": "mon-rovia", "team_share": "false", "applies_to": "mon-rovia"},
        )
        self.assertIn("context: mon-rovia", result)
        self.assertIn("team_share: false", result)
        self.assertIn("applies_to: mon-rovia", result)
        self.assertIn("date: 2024-01-01", result)

    def test_preserves_other_keys(self):
        fm = "url: https://example.com\ntags: [idea, inbox]"
        result = classifier.merge_into_frontmatter(fm, {"context": "personal"})
        self.assertIn("url: https://example.com", result)
        self.assertIn("tags: [idea, inbox]", result)

    def test_empty_fm_string(self):
        result = classifier.merge_into_frontmatter("", {"context": "rising-tides"})
        self.assertIn("context: rising-tides", result)

    def test_non_key_lines_not_confused(self):
        # Lines without a key: pattern should pass through unmodified.
        fm = "# comment line\ndate: 2024-06-01"
        result = classifier.merge_into_frontmatter(fm, {"context": "personal"})
        self.assertIn("# comment line", result)


# ---------------------------------------------------------------------------
# 4. lookup_titles_from_db using a fixture SQLite DB
# ---------------------------------------------------------------------------


def _build_fixture_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal ZCLOUDRECORDING table with the columns the pipeline reads."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ZCLOUDRECORDING (
            ZPATH TEXT,
            ZCUSTOMLABEL TEXT,
            ZENCRYPTEDTITLE TEXT,
            ZDURATION REAL,
            ZDATE REAL
        )
        """
    )
    conn.executemany(
        "INSERT INTO ZCLOUDRECORDING VALUES (?,?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


class TestLookupTitlesFromDb(unittest.TestCase):
    """lookup_titles_from_db reads ZCLOUDRECORDING. We inject a fixture path
    by temporarily replacing the module-level CLOUD_DB constant."""

    def _call_with_db(self, db_path: Path) -> dict:
        """Monkey-patch CLOUD_DB to point at our fixture, then call the function."""
        original = pipeline.CLOUD_DB
        try:
            pipeline.CLOUD_DB = db_path
            return pipeline.lookup_titles_from_db()
        finally:
            pipeline.CLOUD_DB = original

    def test_custom_label_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("20240101 120000-AABBCCDD.m4a", "My Custom Title", "Encrypted", 30.0, 740_138_400.0),
            ])
            titles = self._call_with_db(db)
        self.assertIn("20240101 120000-AABBCCDD", titles)
        title, duration, recorded = titles["20240101 120000-AABBCCDD"]
        self.assertEqual(title, "My Custom Title")

    def test_encrypted_title_fallback(self):
        """When ZCUSTOMLABEL is NULL, falls back to ZENCRYPTEDTITLE."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("20240201 090000-DEADBEEF.m4a", None, "EncryptedTitle", 45.0, 740_138_400.0),
            ])
            titles = self._call_with_db(db)
        title, _, _ = titles["20240201 090000-DEADBEEF"]
        self.assertEqual(title, "EncryptedTitle")

    def test_stem_fallback_when_both_labels_null(self):
        """When both label fields are NULL, the stem (uuid) is used as title."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("20240301 080000-FACEFEED.m4a", None, None, 60.0, 740_138_400.0),
            ])
            titles = self._call_with_db(db)
        title, _, _ = titles["20240301 080000-FACEFEED"]
        self.assertEqual(title, "20240301 080000-FACEFEED")

    def test_duration_returned_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("stem-1234.m4a", "Short", None, 17.5, 740_000_000.0),
            ])
            titles = self._call_with_db(db)
        _, duration, _ = titles["stem-1234"]
        self.assertAlmostEqual(duration, 17.5, places=3)

    def test_cocoa_date_converted_to_unix(self):
        """ZDATE (Cocoa epoch) must become a Unix timestamp in the output."""
        cocoa_zdate = 740_138_400.0  # 2024-06-15 12:00:00 UTC
        expected_unix = cocoa_zdate + _COCOA_EPOCH_OFFSET
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("stem-DATE.m4a", "Date Test", None, 10.0, cocoa_zdate),
            ])
            titles = self._call_with_db(db)
        _, _, recorded = titles["stem-DATE"]
        self.assertAlmostEqual(recorded, expected_unix, places=0)

    def test_null_zdate_results_in_zero(self):
        """NULL ZDATE → 0.0; caller (discover_memos) falls back to file mtime."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("stem-NODATE.m4a", "No Date", None, 5.0, None),
            ])
            titles = self._call_with_db(db)
        _, _, recorded = titles["stem-NODATE"]
        self.assertEqual(recorded, 0.0)

    def test_rows_without_zpath_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                (None, "No Path", None, 10.0, 740_000_000.0),
                ("valid-stem.m4a", "Valid", None, 20.0, 740_000_000.0),
            ])
            titles = self._call_with_db(db)
        self.assertEqual(len(titles), 1)
        self.assertIn("valid-stem", titles)

    def test_missing_db_returns_empty_dict(self):
        """If the DB doesn't exist the function returns {} without raising."""
        result = self._call_with_db(Path("/nonexistent/path/CloudRecordings.db"))
        self.assertEqual(result, {})

    def test_multiple_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "CloudRecordings.db"
            _build_fixture_db(db, [
                ("stem-A.m4a", "Alpha", None, 10.0, 740_000_000.0),
                ("stem-B.m4a", "Beta", None, 20.0, 740_000_001.0),
                ("stem-C.m4a", "Gamma", None, 30.0, 740_000_002.0),
            ])
            titles = self._call_with_db(db)
        self.assertEqual(len(titles), 3)
        self.assertEqual(titles["stem-A"][0], "Alpha")
        self.assertEqual(titles["stem-B"][0], "Beta")
        self.assertEqual(titles["stem-C"][0], "Gamma")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
