"""Read-only modes must not consume the change-detection baselines.

`healthcheck.py report` previously wrote every state snapshot, so previewing a
report before the 07:00 cron ran would silently eat the diff — the scheduled
run then saw "no changes" and stayed quiet about something real.
"""

import json

import pytest

from hc import utils


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    monkeypatch.setattr(utils, "_STATE_FROZEN", False)
    yield tmp_path
    utils.freeze_state(False)


def test_save_and_load_roundtrip(state_dir):
    utils.save_state("ports", ["a", "b"])
    assert utils.load_state("ports") == ["a", "b"]


def test_missing_state_reads_as_none(state_dir):
    assert utils.load_state("never-written") is None


def test_corrupt_state_reads_as_none_rather_than_crashing(state_dir):
    (state_dir / "ports.json").write_text("{not json", encoding="utf-8")
    assert utils.load_state("ports") is None


def test_freeze_blocks_writes(state_dir):
    utils.save_state("ports", ["original"])
    utils.freeze_state(True)
    utils.save_state("ports", ["overwritten"])
    assert utils.load_state("ports") == ["original"]


def test_freeze_does_not_block_reads(state_dir):
    utils.save_state("ports", ["original"])
    utils.freeze_state(True)
    assert utils.load_state("ports") == ["original"]


def test_freeze_can_be_lifted(state_dir):
    utils.freeze_state(True)
    utils.save_state("ports", ["blocked"])
    utils.freeze_state(False)
    utils.save_state("ports", ["written"])
    assert utils.load_state("ports") == ["written"]


def test_freeze_prevents_creating_the_state_directory(tmp_path, monkeypatch):
    target = tmp_path / "does-not-exist"
    monkeypatch.setattr(utils, "STATE_DIR", target)
    utils.freeze_state(True)
    try:
        utils.save_state("ports", ["x"])
        assert not target.exists()
    finally:
        utils.freeze_state(False)


def test_read_only_modes_are_wired_to_freeze_state():
    """Guard the wiring, not just the primitive."""
    source = (utils.SCRIPT_DIR / "healthcheck.py").read_text(encoding="utf-8")
    assert 'if mode in ("report", "text"):' in source
    assert "freeze_state(True)" in source


def test_state_is_written_as_readable_json(state_dir):
    utils.save_state("ports", ["a"])
    assert json.loads((state_dir / "ports.json").read_text(encoding="utf-8")) == ["a"]
