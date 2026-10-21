import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import own_sources_check as check

BUILT = "2026-10-21T17:26:13Z"


class ChangedSources(unittest.TestCase):
    def test_source_newer_than_the_last_build(self):
        commits = {"mods": "2026-10-21T18:05:00Z", "app": "2026-10-20T21:50:00Z"}
        self.assertEqual(check.changed_sources(commits, BUILT), ["mods"])

    def test_nothing_newer(self):
        commits = {"mods": "2026-10-21T17:00:00Z", "app": "2026-10-20T21:50:00Z"}
        self.assertEqual(check.changed_sources(commits, BUILT), [])

    def test_a_run_started_after_the_commit_keeps_it_quiet(self):
        self.assertEqual(check.changed_sources({"mods": "2026-10-21T18:05:00Z"}, "2026-10-21T18:06:10Z"), [])

    def test_no_build_yet(self):
        self.assertEqual(check.changed_sources({"mods": "2026-10-21T18:05:00Z"}, None), ["mods"])

    def test_missing_file_is_ignored(self):
        self.assertEqual(check.changed_sources({"mods": None}, BUILT), [])


if __name__ == "__main__":
    unittest.main()
