"""Once-a-day check that this clone of Haplocaller is up to date, with an optional fast-forward update."""
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
INTERVAL = 24 * 3600


def _git(*args, timeout=20, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_SSH_COMMAND='ssh -o BatchMode=yes')
    return subprocess.run(['git', *args], cwd=cwd or PROJECT, capture_output=True, text=True, timeout=timeout, env=env)


def _stamp_path():
    return Path(os.environ.get('HAPLOCALLER_HOME') or Path.home() / '.local/share/haplocaller') / 'last_update_check'


def check(repo=None, stamp=None, now=None):
    """Return the number of new commits on the tracked branch, or 0 when nothing applies.
    Quiet on every failure: offline, not a git clone, detached head, no upstream, local changes, checked today."""
    repo = Path(repo or PROJECT)
    stamp = Path(stamp or _stamp_path())
    now = time.time() if now is None else now
    try:
        if not (repo / '.git').exists():
            return 0
        if stamp.is_file() and now - stamp.stat().st_mtime < INTERVAL:
            return 0
        if _git('symbolic-ref', '-q', 'HEAD', cwd=repo).returncode or _git('rev-parse', '@{u}', cwd=repo).returncode:
            return 0
        if _git('diff', '--quiet', 'HEAD', cwd=repo).returncode:
            return 0  # tracked files were edited here; never touch them
        if _git('fetch', '-q', timeout=15, cwd=repo).returncode:
            return 0  # offline or no access: try again next time
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        ahead = _git('rev-list', '--count', 'HEAD..@{u}', cwd=repo)
        diverged = _git('rev-list', '--count', '@{u}..HEAD', cwd=repo)
        if ahead.returncode or diverged.returncode or int(diverged.stdout) > 0:
            return 0
        return int(ahead.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0


def apply(repo=None):
    result = _git('merge', '--ff-only', '-q', '@{u}', cwd=Path(repo or PROJECT))
    return result.returncode == 0


def offer(ask, say, skip=False):
    """Interactive terminals only. Returns True when the program was updated (the caller should restart)."""
    if skip or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    count = check()
    if not count:
        return False
    if not ask(f'Haplocaller has {count} new update{"s" if count != 1 else ""}. Update now?'):
        return False
    if apply():
        say('Updated. Starting again with the new version.')
        return True
    say('Update failed; continuing with the current version.')
    return False
