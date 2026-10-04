"""Remove local paths from result files before publishing: the repository root becomes `.` and
the home directory `~`, in file contents and file names. Counts and verdicts are untouched.

    uv run python bench/sanitize_results.py [--check]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = str(Path.home())
SKIP = ('.venv', '.gate', '.git', '.ruff_cache')


TEMP = re.compile(r'/(?:private/)?(?:tmp|var/folders)/[^\s"\']*')


def scrub(text: str) -> str:
    return TEMP.sub('<tmp>', text.replace(str(ROOT), '.').replace(HOME, '~'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='only report; exit 1 if anything leaks')
    args = ap.parse_args()
    leaks = 0
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file() or any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        if path.name.startswith('junit-'):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        clean = scrub(text)
        dirty = clean != text or (HOME in str(path.name) or 'Users_' in path.name)
        if not dirty:
            continue
        leaks += 1
        print(('leak: ' if args.check else 'scrubbed: ') + str(path.relative_to(ROOT)))
        if not args.check:
            path.write_text(clean)
            if 'Users_' in path.name:
                path.rename(path.with_name(path.name.split('_results_gate_mutation_')[-1]))
    return 1 if (args.check and leaks) else 0


if __name__ == '__main__':
    sys.exit(main())
