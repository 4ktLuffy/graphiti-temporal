"""How much of a change does the gate actually exercise?

A revision that passes every scenario is only verified where the scenarios ran its code. For a
PR, this measures which of the lines it changes in `graphiti_core` the gate executed, so a
scorecard can say "untested" instead of letting "no change" read as a pass.

    python -m graphiti_gate.reach SCENARIOS...   # inside a revision's venv; prints executed lines
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path


def executed_lines(paths: list[str]) -> dict[str, dict[str, list[int]]]:
    """Run the scenarios once under coverage: per graphiti_core file (relative path), the lines
    executed and every executable statement (so comments and blank lines never count)."""
    import importlib.util

    import coverage

    # Locate graphiti_core without importing it: lines that run at import time must be measured.
    package = Path(importlib.util.find_spec('graphiti_core').origin).parent
    cov = coverage.Coverage(source=[str(package)], data_file=None)
    cov.start()
    try:
        from graphiti_gate import worker

        sys.argv = ['worker', *paths, '--repeats', '1']
        asyncio.run(worker.main())
    finally:
        cov.stop()
    out = {}
    for f in package.rglob('*.py'):
        rel = f.resolve().relative_to(package.parent.resolve()).as_posix()
        _, statements, _, missing, _ = cov.analysis2(str(f))
        out[rel] = {
            'statements': sorted(statements),
            'executed': sorted(set(statements) - set(missing)),
        }
    return out


def changed_lines(worktree: Path, base_sha: str) -> dict[str, list[int]]:
    """Lines added or modified in graphiti_core/ relative to the base (new-side numbers)."""
    diff = subprocess.run(
        ['git', 'diff', '-U0', base_sha, '--', 'graphiti_core/'],
        cwd=worktree, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    out: dict[str, list[int]] = {}
    current = None
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            current = line[6:]
        elif line.startswith('@@') and current and current.endswith('.py'):
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(current, []).extend(range(start, start + count))
    return out


def _statements_for(path: Path, lines: list[int]) -> list[ast.stmt]:
    """The innermost statement containing each changed line, deduplicated.

    A change inside a multi-line call or a prompt string belongs to its statement. A change in a
    function's header belongs to the function, which counts as run only if its body ran. Blank
    lines, comments and docstrings are not behaviour and are not counted.
    """
    try:
        source = path.read_text()
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return []
    text = source.splitlines()
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    found: dict[int, ast.stmt] = {}
    for line in lines:
        if line > len(text) or not text[line - 1].strip() or text[line - 1].strip().startswith('#'):
            continue
        spans = [n for n in statements if n.lineno <= line <= (n.end_lineno or n.lineno)]
        if not spans:
            continue
        best = min(spans, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
        is_docstring = isinstance(best, ast.Expr) and isinstance(best.value, ast.Constant) and isinstance(best.value.value, str)  # fmt: skip
        if not is_docstring:
            found[id(best)] = best
    return list(found.values())


def _ran(stmt: ast.stmt, executed: set[int]) -> bool:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return any(_ran(child, executed) for child in stmt.body)
    return any(n in executed for n in range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))


def reach(spec: str, scenarios: Path, base: str = 'main') -> dict:
    """{'changed': n, 'executed': k, 'by_file': {...}} for one revision against its base."""
    from graphiti_gate.revisions import prepare

    rev = prepare(spec, base=base)
    changed = changed_lines(rev.worktree, rev.base_sha)
    if not changed:
        return {'revision': spec, 'changed': 0, 'executed': 0, 'by_file': {}}
    subprocess.run(
        ['uv', 'pip', 'install', '--quiet', '--python', str(rev.python), 'coverage>=7'], check=True
    )
    out = subprocess.run(
        [str(rev.python), '-m', 'graphiti_gate.reach', str(scenarios)],
        capture_output=True, text=True,
    )  # fmt: skip
    measured = json.loads(out.stdout.strip().splitlines()[-1])
    by_file = {}
    for f, lines in changed.items():
        executed = set(measured.get(f, {}).get('executed', []))
        units = _statements_for(rev.worktree / f, lines)
        if not units:
            continue
        ran = [u for u in units if _ran(u, executed)]
        by_file[f] = {'changed': len(units), 'executed': len(ran)}
    return {
        'revision': spec,
        'changed': sum(v['changed'] for v in by_file.values()),
        'executed': sum(v['executed'] for v in by_file.values()),
        'by_file': by_file,
    }


if __name__ == '__main__':
    import io
    from contextlib import redirect_stdout

    with redirect_stdout(io.StringIO()):  # the worker's per-scenario lines are not wanted here
        result = executed_lines(sys.argv[1:])
    print(json.dumps(result))
