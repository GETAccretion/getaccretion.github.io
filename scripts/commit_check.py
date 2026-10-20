"""Fail CI when new commits or a pull request would publish AI attribution or the owner's real email.

Equivalent copies live in the store, app and mods repositories; keep them identical.
Only the commits this event adds are judged (a pull request's commits, a push's new commits),
so history that is already published is never re-checked. Commits that an upstream project
already has (--upstream) are its history, not ours, and are skipped. Holds no personal data:
the owner is known by GitHub login and must commit with the GitHub noreply address.

  python3 commit_check.py [--upstream URL]
"""

import argparse
import json
import os
import re
import subprocess
import sys

OWNER_NAMES = {"benwilllopez", "benwill lopez"}
NOREPLY = "@users.noreply.github.com"
AI_EMAIL = re.compile(
    r"@anthropic\.com$|@openai\.com$|^cursoragent@cursor\.com$|copilot@users\.noreply\.github\.com$"
    r"|copilot-swe-agent|devin-ai-integration|google-labs-jules",
    re.IGNORECASE,
)
AI_NAME = re.compile(r"^(claude|codex|chatgpt|copilot|cursor|cursor agent)$|\(aider\)$", re.IGNORECASE)
AI_LINE = re.compile(
    r"^[ \t]*co-authored-by:[ \t]*(claude|codex|chatgpt|copilot|cursor|aider)"
    r"|^[ \t]*co-authored-by:.*(@anthropic\.com|@openai\.com|cursoragent@cursor\.com|copilot"
    r"|devin-ai-integration|google-labs-jules)"
    r"|^[ \t]*claude-session:|generated with \[?claude code",
    re.IGNORECASE | re.MULTILINE,
)
UPSTREAM_REFS = "refs/commit-check/upstream"


def git(*args, cwd=None):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", check=False
    )
    return result.returncode, result.stdout


def is_zero(sha):
    return not sha or set(sha) == {"0"}


def revisions(event_name, event, cwd=None):
    """The rev-list arguments for the commits this event adds."""
    if event_name in ("pull_request", "pull_request_target"):
        pull = event["pull_request"]
        return [pull["head"]["sha"], f"^{pull['base']['sha']}"]
    if event_name == "push":
        before, after = event.get("before", ""), event.get("after", "") or "HEAD"
        if not is_zero(before) and git("cat-file", "-e", f"{before}^{{commit}}", cwd=cwd)[0] == 0:
            return [after, f"^{before}"]
        return [f"{after}^!"]  # new branch or force push: the tip only
    return ["HEAD^!"]


def exclude_upstream(url, cwd=None):
    """Fetch the upstream project's branches so its commits can be left out; [] when unreachable."""
    code, _ = git("fetch", "--quiet", "--no-tags", url, f"+refs/heads/*:{UPSTREAM_REFS}/*", cwd=cwd)
    if code:
        print(f"::warning::Could not fetch {url}; upstream commits in this range are checked too")
        return []
    return ["--not", f"--glob={UPSTREAM_REFS}/*"]


def identity_problems(what, name, email):
    lowered = email.lower()
    if name.lower() in OWNER_NAMES and not lowered.endswith(NOREPLY):
        return [f"the {what} is the owner without the GitHub noreply address"]
    if AI_EMAIL.search(lowered) or AI_NAME.search(name.strip()):
        return [f"the {what} is an AI tool ({name})"]
    return []


def commit_problems(revs, cwd=None):
    """(short id, reason) for each problem in the commits revs selects."""
    code, output = git("log", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", *revs, cwd=cwd)
    if code:
        raise SystemExit(f"git log failed for {' '.join(revs)}")
    found = []
    for record in output.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 6:
            continue
        sha, a_name, a_mail, c_name, c_mail, body = fields
        short = sha[:7]
        reasons = identity_problems("author", a_name, a_mail)
        if (c_name, c_mail) != (a_name, a_mail):
            reasons += identity_problems("committer", c_name, c_mail)
        reasons += [f"AI attribution line: {line}" for line in ai_lines(body)]
        found += [(short, reason) for reason in reasons]
    return found


def ai_lines(text):
    """The lines of a commit message or PR description that attribute the work to an AI tool."""
    return [
        line.strip()[:90]
        for line in text.splitlines()
        if AI_LINE.search(line) or "claude.ai/code/session" in line.lower()
    ]


def body_problems(event_name, event):
    if event_name not in ("pull_request", "pull_request_target"):
        return []
    body = (event.get("pull_request") or {}).get("body") or ""
    return [("PR", f"AI attribution in the description: {line}") for line in ai_lines(body)]


def annotation(text):
    """GitHub workflow commands need %, CR and LF escaped."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv=None, cwd=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", help="upstream project whose commits are skipped")
    args = parser.parse_args(argv)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    event = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
    revs = revisions(event_name, event, cwd=cwd)
    if args.upstream:
        revs += exclude_upstream(args.upstream, cwd=cwd)
    found = commit_problems(revs, cwd=cwd) + body_problems(event_name, event)
    for short, reason in found:
        print(f"::error title=Commit check::{annotation(f'{short}: {reason}')}")
    if found:
        print(
            "New commits may not carry AI attribution, and the owner commits with the GitHub noreply "
            "address. Rewrite the listed commits (or edit the PR description) and push again."
        )
        return 1
    print("Commit check passed: no AI attribution and no private owner address in the new commits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
