"""Shared real-service graph fixture and optional runtime gate."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from kg.graph._native import NativeError, _engine

spec = importlib.util.spec_from_file_location(
    "graph_example", Path(__file__).parents[2] / "examples" / "graph_build.py",
)
assert spec is not None and spec.loader is not None
example = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = example
spec.loader.exec_module(example)
fixture, verify, query = example.fixture, example.verify, example.query


def require_native():
    try:
        _engine()
    except NativeError:
        if os.environ.get("KG_REQUIRE_NATIVE") == "1":
            pytest.fail("Required Ladybug 0.20.4 runtime unavailable")
        pytest.skip("Optional Ladybug runtime/platform unavailable; separate local gate required")
