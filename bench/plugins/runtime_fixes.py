"""pytest plugin: install this repository's runtime fixes before Graphiti's tests import anything.

    GT_FIXES=1,3 pytest -p bench.plugins.runtime_fixes tests/...

`1` is the invalidation filter, `3` the temporal neighbours. Used to check that the runtime fixes
pass the same regression tests as the upstream patches.
"""

import os


def pytest_configure(config):
    from graphiti_temporal import fix

    wanted = set(os.environ.get('GT_FIXES', '1,3').split(','))
    if '1' in wanted:
        fix.apply_invalidation_filter()
    if '3' in wanted:
        fix.apply_backfill_neighbors()
