"""Install any Graphiti revision into its own virtualenv, so scenarios run against it in isolation.

A revision is one of:

- `main`, a branch, or a commit sha of getzep/graphiti;
- `pr:N`, pull request N merged onto `--base` (default `main`), which is what would land. A merge
  conflict is reported as such, never as a scenario failure;
- `patch:PATH`, a diff applied onto `--base`;
- `path:DIR`, a local checkout as it is, e.g. the PR checkout in CI (a fork's head commit is
  not in the mirror).

Everything lives under `.gate/` (a mirror clone, one worktree and one venv per revision). Merges are
left uncommitted in throwaway worktrees; nothing is ever pushed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO_URL = 'https://github.com/getzep/graphiti.git'
ROOT = Path(__file__).resolve().parent.parent
# A source checkout of this repository (development), or an installed copy (CI, `uv tool install`).
DEV = (ROOT / 'pyproject.toml').exists()
GATE = Path(os.environ.get('GRAPHITI_GATE_HOME', ROOT / '.gate' if DEV else Path.cwd() / '.gate'))
MIRROR = GATE / 'graphiti'


class RevisionError(RuntimeError):
    """The revision could not be built (network, git, or install failure)."""


class MergeConflict(RevisionError):
    """The PR does not merge onto its base. A result, not a failure of the gate."""


@dataclass(frozen=True)
class Revision:
    spec: str
    base_sha: str
    head_sha: str | None  # the PR head, if any
    patch_sha: str | None  # sha256 of the applied patch, if any
    worktree: Path
    python: Path

    @property
    def label(self) -> str:
        parts = [self.spec, f'base {self.base_sha[:7]}']
        if self.head_sha:
            parts.append(f'head {self.head_sha[:7]}')
        if self.patch_sha:
            parts.append(f'patch {self.patch_sha[:7]}')
        return ', '.join(parts)


def _git(*args: str, cwd: Path = MIRROR, check: bool = True) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RevisionError(f'git {" ".join(args)} failed: {result.stderr.strip()[-500:]}')
    return result.stdout.strip()


_fetched = False


def ensure_mirror() -> None:
    """Clone once; fetch main once per process, so every revision in a run shares one base."""
    global _fetched
    if not MIRROR.exists():
        GATE.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ['git', 'clone', '--quiet', '--filter=blob:none', REPO_URL, str(MIRROR)], check=True
        )
    if _fetched:
        return
    for attempt in range(4):
        try:
            _git('fetch', '--quiet', 'origin', 'main')
            break
        except RevisionError:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))  # transient network errors happen
    _fetched = True


def _sha(ref: str) -> str:
    try:
        return _git('rev-parse', '--verify', f'{ref}^{{commit}}')
    except RevisionError:
        return _git('rev-parse', '--verify', f'origin/{ref}^{{commit}}')


def prepare(spec: str, base: str = 'main') -> Revision:
    """Check out and install `spec`; reuse the venv if this exact revision was built before."""
    ensure_mirror()
    base_sha = _sha(base)
    head_sha = patch_sha = None
    patch_path: Path | None = None

    if spec.startswith('path:'):
        local = Path(spec[5:]).expanduser().resolve()
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=local, capture_output=True, text=True).stdout.strip()  # fmt: skip
        dirty = subprocess.run(['git', 'diff', 'HEAD'], cwd=local, capture_output=True, text=True).stdout  # fmt: skip
        key = hashlib.sha256(f'path|{local}|{head}|{dirty}'.encode()).hexdigest()[:16]
        venv = GATE / 'venvs' / key
        python = venv / 'bin' / 'python'
        revision = Revision(spec, head or 'local', None, None, local, python)
        if not (venv / '.gate-ready').exists():
            _install(local, venv, python, revision)
        return revision
    if spec.startswith('pr:'):
        number = int(spec[3:])
        _git('fetch', '--quiet', 'origin', f'pull/{number}/head:gate-pr-{number}', '--force')
        head_sha = _sha(f'gate-pr-{number}')
    elif spec.startswith('patch:'):
        patch_path = Path(spec[6:]).expanduser().resolve()
        patch_sha = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    else:
        base_sha = _sha(spec)

    key = hashlib.sha256(f'{base_sha}|{head_sha}|{patch_sha}'.encode()).hexdigest()[:16]
    worktree = GATE / 'worktrees' / key
    venv = GATE / 'venvs' / key
    python = venv / 'bin' / 'python'
    revision = Revision(spec, base_sha, head_sha, patch_sha, worktree, python)
    if python.exists() and (venv / '.gate-ready').exists():
        return revision

    if worktree.exists():
        _git('worktree', 'remove', '--force', str(worktree), check=False)
        shutil.rmtree(worktree, ignore_errors=True)
    _git('worktree', 'add', '--quiet', '--detach', str(worktree), base_sha)
    if head_sha:
        merged = subprocess.run(
            ['git', 'merge', '--no-commit', '--no-ff', '--quiet', head_sha],
            cwd=worktree,
            capture_output=True,
            text=True,
            env={
                'GIT_AUTHOR_NAME': 'gate',
                'GIT_AUTHOR_EMAIL': 'gate@localhost',
                'GIT_COMMITTER_NAME': 'gate',
                'GIT_COMMITTER_EMAIL': 'gate@localhost',
                'PATH': '/usr/bin:/bin:/opt/homebrew/bin',
            },  # fmt: skip
        )
        if merged.returncode != 0:
            raise MergeConflict(f'{spec} does not merge onto {base}: {merged.stdout[-400:]}')
    if patch_path:
        _git('apply', str(patch_path), cwd=worktree)

    _install(worktree, venv, python, revision)
    return revision


def _install(source: Path, venv: Path, python: Path, revision: Revision) -> None:
    shutil.rmtree(venv, ignore_errors=True)
    subprocess.run(['uv', 'venv', '--quiet', '--python', '3.14', str(venv)], check=True)
    subprocess.run(
        ['uv', 'pip', 'install', '--quiet', '--python', str(python), str(source),
         'httpx>=0.27', 'pyyaml>=6'],
        check=True,
    )  # fmt: skip
    if DEV:
        subprocess.run(
            ['uv', 'pip', 'install', '--quiet', '--python', str(python), '--no-deps', '-e', str(ROOT)],
            check=True,
        )  # fmt: skip
    else:
        # Installed copy: put this package's own modules (not its graphiti-core) into the venv.
        purelib = subprocess.run(
            [str(python), '-c', 'import sysconfig; print(sysconfig.get_paths()["purelib"])'],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        for package in ('graphiti_gate', 'graphiti_temporal'):
            shutil.copytree(ROOT / package, Path(purelib) / package, dirs_exist_ok=True)
    (venv / '.gate-ready').write_text(revision.label)
