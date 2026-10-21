"""Starts a store build when one of our own sources changed after the last build (own-sources.yml).

The mods source (AccretionMods/mods mods.json) and the app source (GETAccretion/accretion-app
app-repo.json) live in other repos, so a publish there does not trigger build.yml. This compares the
time of each file's latest commit on main with the start of the newest build.yml run (any status,
queued included) and dispatches build.yml once when a source is newer. The dispatched run is newer
than the commit, so the next check stays quiet: no loop. Needs GH_TOKEN with actions: write.
"""

import argparse
import json
import os
import sys
import urllib.request

API = "https://api.github.com"
STORE = os.environ.get("GITHUB_REPOSITORY", "GETAccretion/getaccretion.github.io")
WORKFLOW = "build.yml"
SOURCES = [("AccretionMods/mods", "mods.json"), ("GETAccretion/accretion-app", "app-repo.json")]


def api(path, token, data=None):
    request = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def last_commit(repo, file, token):
    """ISO time of the newest commit on main that touched file, or None."""
    commits = api(f"/repos/{repo}/commits?sha=main&path={file}&per_page=1", token)
    return commits[0]["commit"]["committer"]["date"] if commits else None


def last_build(token):
    """ISO start time of the newest build.yml run, or None."""
    runs = api(f"/repos/{STORE}/actions/workflows/{WORKFLOW}/runs?per_page=1", token)["workflow_runs"]
    return runs[0]["created_at"] if runs else None


def changed_sources(commits, built):
    """The sources whose latest commit is newer than the last build (ISO UTC strings compare in order)."""
    return [name for name, when in commits.items() if when and (built is None or when > built)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report, do not start a build")
    args = parser.parse_args()
    token = os.environ["GH_TOKEN"]
    commits = {f"{repo}/{file}": last_commit(repo, file, token) for repo, file in SOURCES}
    built = last_build(token)
    print(f"Last store build started: {built}")
    for name, when in commits.items():
        print(f"{name}: last changed {when}")
    newer = changed_sources(commits, built)
    if not newer:
        print("No own source changed since the last build.")
        return 0
    print(f"Changed since the last build: {', '.join(newer)}")
    if args.dry_run:
        print("Dry run: no build started.")
        return 0
    api(f"/repos/{STORE}/actions/workflows/{WORKFLOW}/dispatches", token, {"ref": "main"})
    print("Started a store build.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
