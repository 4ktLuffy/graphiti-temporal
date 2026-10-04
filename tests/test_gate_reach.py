"""Reach counts a changed statement as run only if it executed, not because its module imported."""

import ast  # noqa: F401  (used through reach)
from pathlib import Path

from graphiti_gate.reach import _ran, _statements_for

SOURCE = """\
def handler(x):
    # a comment
    '''a docstring'''
    value = [
        'prompt text',
        x,
    ]
    return value


CONSTANT = 3
"""


def test_mapping_and_runtime_rule(tmp_path: Path):
    f = tmp_path / 'm.py'
    f.write_text(SOURCE)
    # changed: the def line, a comment, the docstring, a line inside the multi-line list, the constant
    units = _statements_for(f, [1, 2, 3, 5, 11])
    kinds = sorted(type(u).__name__ for u in units)
    assert kinds == ['Assign', 'Assign', 'FunctionDef']  # comment and docstring are not counted
    import_only = {1, 11}  # what an import executes: the def line and the module constant
    assert [_ran(u, import_only) for u in units if type(u).__name__ == 'FunctionDef'] == [False]
    called = import_only | {4, 8}
    assert all(_ran(u, called) for u in units)
