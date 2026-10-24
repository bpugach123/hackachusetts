"""Smoke tests for the package scaffold."""

import importlib

import pytest

from siting import run


@pytest.mark.parametrize(
    "module", ["siting", "siting.run", "siting.fetch", "siting.sources", "siting.score"]
)
def test_subpackages_import(module):
    assert importlib.import_module(module) is not None


def test_run_parser_defaults():
    args = run.build_parser().parse_args([])
    assert args.config == "../config/device_config.yaml"
    assert args.out == "../output"


def test_run_stub_returns_nonzero_until_implemented():
    assert run.main(["--config", "x.yaml", "--out", "out"]) == 1
