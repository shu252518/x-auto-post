"""Restore and persist the allow-listed .state files on the state branch."""
from __future__ import annotations
import argparse, shutil, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
ALLOWED = {"post_history.json", "performance_summary.json", "last_post.sha256", "theme_history.json"}

def run(*args: str, cwd: Path = ROOT, check: bool = True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)

def restore() -> bool:
    fetched = run("fetch", "origin", "state", check=False)
    if fetched.returncode:
        return False
    ref = run("rev-parse", "--verify", "origin/state", check=False)
    if ref.returncode:
        return False
    STATE.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", "--format=tar", "origin/state", ".state"], cwd=ROOT, capture_output=True)
    if archive.returncode:
        return False
    import io, tarfile
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
        tar.extractall(ROOT)
    return True

def save() -> bool:
    if not STATE.exists():
        return True
    for attempt in range(2):
        run("fetch", "origin", "state", check=False)
        base = run("rev-parse", "--verify", "origin/state", check=False)
        tmp = Path(tempfile.mkdtemp(prefix="x-state-"))
        try:
            branch_name = f"state-persist-{attempt}"
            if base.returncode:
                run("worktree", "add", "--detach", str(tmp), "HEAD")
                run("checkout", "--orphan", branch_name, cwd=tmp)
            else:
                run("worktree", "add", "--detach", str(tmp), "origin/state")
                run("checkout", "--orphan", branch_name, cwd=tmp)
            for child in tmp.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
            dest = tmp / ".state"; dest.mkdir()
            for item in STATE.iterdir():
                if item.is_file() and item.name in ALLOWED:
                    shutil.copy2(item, dest / item.name)
            run("add", ".state", cwd=tmp)
            run("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", "Update persistent posting state", cwd=tmp, check=False)
            pushed = run("push", "origin", "HEAD:refs/heads/state", cwd=tmp, check=False)
            if pushed.returncode == 0:
                return True
        finally:
            run("worktree", "remove", "--force", str(tmp), check=False)
            shutil.rmtree(tmp, ignore_errors=True)
    return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("restore", "save")); args = parser.parse_args()
    raise SystemExit(0 if (restore() if args.action == "restore" else save()) else 1)
