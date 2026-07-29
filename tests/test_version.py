"""
The package version lives in two files and they have to agree.

0.7.0 shipped with pyproject.toml bumped and ``__version__`` left a release behind, so the wheel on
PyPI reported the wrong version of itself. Nothing failed — which is exactly why it needs a test.
"""

import tomllib
from pathlib import Path

import django_tasks_concurrent

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_dunder_version_matches_pyproject():
    declared_version = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    assert django_tasks_concurrent.__version__ == declared_version
