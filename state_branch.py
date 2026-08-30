"""Persist allow-listed runtime state on a durable git branch."""
from __future__ import annotations
import argparse, io, shutil, subprocess, tarfile, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / ".state"
ALLOWED = {"post_history.json", "performance_summary.json", "last_post.sha256", "theme_history.json"}
BOT = ("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com")

def run(*args: str, cwd: Path | None = None, check: bool = True):
    return subprocess.run(["git", *args], cwd=cwd or ROOT, check=check, capture_output=True, text=True)

def _failure(action, result):
    detail = (result.stderr or result.stdout or "").strip()
    print(f"state {action} failed" + (f": {detail}" if detail else ""))

def restore() -> bool:
    fetched = run("fetch", "origin", "state", check=False)
    ref = run("rev-parse", "--verify", "refs/remotes/origin/state", check=False)
    if ref.returncode:
        STATE.mkdir(parents=True, exist_ok=True); print("state branch not found; starting with empty state"); return True
    if fetched.returncode: _failure("fetch", fetched); return False
    archive = subprocess.run(["git", "archive", "--format=tar", "origin/state", ".state"], cwd=ROOT, capture_output=True)
    if archive.returncode: _failure("restore", archive); return False
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar: tar.extractall(ROOT)
    print("state restored from origin/state"); return True

def _snapshot():
    snap = Path(tempfile.mkdtemp(prefix="x-state-snapshot-"))
    if STATE.exists():
        for name in ALLOWED:
            source = STATE / name
            if source.is_file(): shutil.copy2(source, snap / name)
    return snap

def save() -> bool:
    if not STATE.exists(): return True
    snapshot = _snapshot()
    try:
        for attempt in range(3):
            fetched = run("fetch", "origin", "state", check=False)
            if fetched.returncode: _failure("fetch", fetched); continue
            base = run("rev-parse", "--verify", "refs/remotes/origin/state", check=False)
            exists = base.returncode == 0
            tmp = Path(tempfile.mkdtemp(prefix="x-state-"))
            try:
                result = run("worktree", "add", "--detach", str(tmp), "origin/state" if exists else "HEAD", check=False)
                if result.returncode: _failure("worktree creation", result); continue
                if not exists:
                    result = run("checkout", "--orphan", f"state-persist-{attempt}", cwd=tmp, check=False)
                    if result.returncode: _failure("orphan checkout", result); continue
                for child in tmp.iterdir():
                    if child.name != ".git": shutil.rmtree(child) if child.is_dir() else child.unlink()
                dest = tmp / ".state"; dest.mkdir()
                for item in snapshot.iterdir(): shutil.copy2(item, dest / item.name)
                staged = run("add", "-A", ".state", cwd=tmp, check=False)
                if staged.returncode: _failure("stage", staged); continue
                if run("diff", "--cached", "--quiet", cwd=tmp, check=False).returncode == 0:
                    print("state unchanged; nothing to commit"); return True
                committed = run(*BOT, "commit", "-m", "Update persistent posting state", cwd=tmp, check=False)
                if committed.returncode: _failure("commit", committed); continue
                pushed = run("push", "origin", "HEAD:refs/heads/state", cwd=tmp, check=False)
                if pushed.returncode == 0: print("state saved to origin/state"); return True
                _failure("push", pushed); print(f"state push attempt {attempt + 1}/3 will retry from latest origin/state")
            finally:
                run("worktree", "remove", "--force", str(tmp), check=False); shutil.rmtree(tmp, ignore_errors=True)
        return False
    finally: shutil.rmtree(snapshot, ignore_errors=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("action", choices=("restore", "save")); args = parser.parse_args()
    raise SystemExit(0 if (restore() if args.action == "restore" else save()) else 1)
