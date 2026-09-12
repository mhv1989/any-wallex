#!/usr/bin/env python3
"""Sync the GitHub shadow copy from the main working repo.

The shadow folder (shadow-any-wallex) is the ONLY thing that gets pushed to
GitHub. It always mirrors main-repo HEAD exactly — clean, audited, no user
data — because it is built with `git archive HEAD` (committed files only;
.gitignore'd data/keys/logs can never enter).

Usage:
  python scripts/sync_shadow.py          # sync + local commit (no push)
  python scripts/sync_shadow.py --push   # sync + commit + push to GitHub
"""
import io
import os
import subprocess
import sys
import stat
import tarfile

MAIN = r"F:\works\bours\Code BA CHATGPT\Hermes Workplace\Any WALLEX"
SHADOW = r"F:\works\bours\Code BA CHATGPT\Hermes Workplace\shadow-any-wallex"
REMOTE = "https://github.com/mhv1989/any-wallex.git"


def git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{r.stderr}")
    return r.stdout.strip()


def main():
    push = "--push" in sys.argv

    # 0) main repo must be committed/clean — uncommitted work cannot be synced
    if git(MAIN, "status", "--short"):
        sys.exit("Main repo has uncommitted changes. Commit there first, then sync.")

    # 1) wipe shadow working tree (keep .git)
    for root, dirs, files in os.walk(SHADOW):
        if ".git" in dirs:
            dirs.remove(".git")
        for f in files:
            fp = os.path.join(root, f)
            try:
                os.chmod(fp, stat.S_IWRITE)
                os.remove(fp)
            except (PermissionError, FileNotFoundError):
                print(f"  ! could not remove {fp} — close apps using it")

    for root, dirs, files in os.walk(SHADOW, topdown=False):
        if ".git" in root:
            continue
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass

    # 2) re-export HEAD (committed files only — inherently clean)
    ar = subprocess.run(["git", "-C", MAIN, "archive", "HEAD"], capture_output=True)
    if ar.returncode != 0:
        sys.exit("git archive failed")
    tf = tarfile.open(fileobj=io.BytesIO(ar.stdout))
    tf.extractall(SHADOW)
    tf.close()

    # 3) restore the shadow-only note (never in main repo)
    with open(os.path.join(SHADOW, "_README_SHADOW.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(
            "# SHADOW EXPORT — the ONLY folder pushed to GitHub\n\n"
            "Mirrors main-repo HEAD exactly. Contains no user data, no API\n"
            "keys, no logs (built from committed files only).\n"
            "Target: https://github.com/mhv1989/any-wallex\n"
            "Original working copy: Any WALLEX folder (never push from there).\n")

    # 4) commit if anything changed
    if not git(SHADOW, "status", "--short"):
        print("Shadow already up to date — nothing to do.")
        return
    git(SHADOW, "add", "-A")
    head_msg = git(MAIN, "log", "-1", "--pretty=%s")
    git(SHADOW, "commit", "-m", f"sync: {head_msg}")
    print("Shadow synced:", git(SHADOW, "log", "-1", "--oneline"))

    # 5) push if requested
    if push:
        if REMOTE not in git(SHADOW, "remote", "-v"):
            git(SHADOW, "remote", "add", "origin", REMOTE)
        print(git(SHADOW, "push", "origin", "master"))
    else:
        print("Review changes, then: git -C shadow push origin master")


if __name__ == "__main__":
    main()
