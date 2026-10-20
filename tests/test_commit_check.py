import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/commit_check.py"
spec = importlib.util.spec_from_file_location("commit_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

OWNER = ("BenwillLopez", "98793822+BenwillLopez@users.noreply.github.com")
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")


class CommitCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        config = base / "gitconfig"
        # a neutral identity for commits the tests make without one (merges); Linux git refuses to guess
        config.write_text(
            "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
            "[user]\n\tname = Fixture\n\temail = fixture@example.test\n",
            encoding="utf-8",
        )
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_GLOBAL=str(config), GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.base = base
        self.repo = base / "repo"
        self.git("init", "-q", str(self.repo), cwd=base)
        self.count = 0

    def git(self, *args, cwd=None, env=None):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env={**os.environ, **(env or {})},
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message="Add a file", author=OWNER, committer=None, repo=None):
        repo = repo or self.repo
        committer = committer or author
        self.count += 1
        (repo / f"f{self.count}.txt").write_text(f"{self.count}\n", encoding="utf-8")
        self.git("add", ".", cwd=repo)
        note = self.base / f"m{self.count}.txt"
        note.write_text(message + "\n", encoding="utf-8")
        identity = {
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_COMMITTER_NAME": committer[0],
            "GIT_COMMITTER_EMAIL": committer[1],
        }
        self.git("commit", "-q", "-F", str(note), cwd=repo, env=identity)
        return self.git("rev-parse", "HEAD", cwd=repo)

    def problems_of(self, sha):
        return [reason for _, reason in check.commit_problems([f"{sha}^!"], cwd=self.repo)]

    def test_revisions_cover_only_what_the_event_adds(self):
        first = self.commit()
        second = self.commit()
        pull = {"pull_request": {"base": {"sha": first}, "head": {"sha": second}}}
        self.assertEqual(check.revisions("pull_request", pull, cwd=self.repo), [second, f"^{first}"])
        push = {"before": first, "after": second}
        self.assertEqual(check.revisions("push", push, cwd=self.repo), [second, f"^{first}"])
        for before in ("0" * 40, "1" * 40, ""):
            with self.subTest(before=before):
                event = {"before": before, "after": second}
                self.assertEqual(check.revisions("push", event, cwd=self.repo), [f"{second}^!"])
        self.assertEqual(check.revisions("workflow_dispatch", {}, cwd=self.repo), ["HEAD^!"])

    def test_owner_with_noreply_bots_and_human_coauthors_pass(self):
        self.assertEqual(self.problems_of(self.commit()), [])
        self.assertEqual(self.problems_of(self.commit("Check links and rebuild", author=BOT)), [])
        message = "Update CLAUDE.md for the Claude Code setup\n\nCo-authored-by: Jane Doe <jane@example.com>"
        self.assertEqual(self.problems_of(self.commit(message)), [])
        web_merge = self.commit("Merge pull request #1", committer=("GitHub", "noreply@github.com"))
        self.assertEqual(self.problems_of(web_merge), [])

    def test_owner_without_noreply_is_flagged(self):
        reasons = self.problems_of(self.commit(author=("BenwillLopez", "owner@example.com")))
        self.assertEqual(reasons, ["the author is the owner without the GitHub noreply address"])

    def test_ai_author_and_committer_are_flagged(self):
        claude = self.commit(author=("Claude", "noreply@anthropic.com"))
        self.assertEqual(self.problems_of(claude), ["the author is an AI tool (Claude)"])
        codex = self.commit(committer=("Codex", "noreply@openai.com"))
        self.assertEqual(self.problems_of(codex), ["the committer is an AI tool (Codex)"])

    def test_ai_attribution_lines_are_flagged(self):
        lines = (
            "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            "Co-authored-by: Codex <noreply@openai.com>",
            "Claude-Session: https://claude.ai/code/session_0123",
            "\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)",
            "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
            "Co-authored-by: aider (gpt-5) <noreply@aider.chat>",
            "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
        )
        for line in lines:
            with self.subTest(line=line):
                reasons = self.problems_of(self.commit(f"Change something\n\n{line}"))
                self.assertEqual(reasons, [f"AI attribution line: {line}"])

    def test_upstream_history_is_skipped_only_with_the_upstream_option(self):
        upstream = self.base / "upstream"
        self.git("init", "-q", str(upstream), cwd=self.base)
        self.commit(
            "Upstream change\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
            author=("Upstream Dev", "dev@upstream.example"),
            repo=upstream,
        )
        base = self.commit()
        self.git("fetch", "-q", str(upstream), "main:refs/tmp/upstream")
        self.git("merge", "-q", "--allow-unrelated-histories", "-m", "Merge upstream", "refs/tmp/upstream")
        self.git("update-ref", "-d", "refs/tmp/upstream")
        revs = ["HEAD", f"^{base}"]
        self.assertEqual(len(check.commit_problems(revs, cwd=self.repo)), 1)
        skipped = revs + check.exclude_upstream(str(upstream), cwd=self.repo)
        self.assertEqual(check.commit_problems(skipped, cwd=self.repo), [])

    def test_unreachable_upstream_warns_and_checks_everything(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check.exclude_upstream(str(self.base / "missing"), cwd=self.repo), [])
        self.assertIn("::warning::", out.getvalue())

    def test_pull_request_description_is_checked(self):
        bad = {
            "pull_request": {
                "body": "Summary\n\n\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)"
            }
        }
        self.assertEqual(len(check.body_problems("pull_request", bad)), 1)
        session = {"pull_request": {"body": "Made in https://claude.ai/code/session_42"}}
        self.assertEqual(len(check.body_problems("pull_request", session)), 1)
        clean = {"pull_request": {"body": "Summary of the change"}}
        self.assertEqual(check.body_problems("pull_request", clean), [])
        self.assertEqual(check.body_problems("push", bad), [])

    def test_main_reports_annotations_and_exit_codes(self):
        first = self.commit()
        bad = self.commit("Change\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        clean = self.commit()
        event_file = self.base / "event.json"
        for before, after, expected in ((first, bad, 1), (bad, clean, 0)):
            with self.subTest(after=after[:7]):
                event_file.write_text(json.dumps({"before": before, "after": after}), encoding="utf-8")
                env = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event_file)}
                with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(check.main([], cwd=self.repo), expected)
                self.assertEqual("::error title=Commit check::" in out.getvalue(), expected == 1)

    def test_annotation_text_is_escaped(self):
        self.assertEqual(check.annotation("50%\r\nnext"), "50%25%0D%0Anext")


if __name__ == "__main__":
    unittest.main()
