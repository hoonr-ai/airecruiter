"""config.py must not assign the same top-level name twice.

A later assignment silently wins. That is how DEFAULT_SCREENING_LEVEL
reverted from L0.5 to L1.5 during the develop merge.
"""
import ast
from pathlib import Path

_CONFIG = Path(__file__).resolve().parents[1].joinpath("core", "config.py")


def _assignments():
    tree = ast.parse(_CONFIG.read_text())
    assigned = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                assigned.append((target.id, node))
    return assigned


def test_config_has_one_assignment_per_top_level_name():
    counts = {}
    for name, _node in _assignments():
        counts[name] = counts.get(name, 0) + 1
    duplicates = {name: count for name, count in counts.items() if count > 1}
    assert duplicates == {}


def test_default_screening_level_falls_back_to_l05():
    nodes = [node for name, node in _assignments() if name == "DEFAULT_SCREENING_LEVEL"]
    assert len(nodes) == 1
    call = nodes[0].value
    assert isinstance(call, ast.Call)
    assert isinstance(call.args[1], ast.Constant)
    assert call.args[1].value == "L0.5"
