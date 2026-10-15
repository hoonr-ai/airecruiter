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


def test_default_screening_level_falls_back_to_l05(monkeypatch):
    """A plain import-and-assert exercising the real get_env_with_default
    fallback is simpler and more direct than parsing the AST to guess which
    call argument holds the default literal (PR #823 review) — it checks
    actual runtime behavior, not just what the source code appears to say."""
    import importlib
    import core.config as config

    monkeypatch.delenv("DEFAULT_SCREENING_LEVEL", raising=False)
    try:
        importlib.reload(config)
        assert config.DEFAULT_SCREENING_LEVEL == "L0.5"
    finally:
        monkeypatch.undo()
        importlib.reload(config)  # restore the shared module to the real environment
