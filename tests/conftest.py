"""Shared test fixtures.

Every check in `hc.checks` reaches the system through exactly two seams —
`run()` for shell commands and `has()` for tool discovery — so faking those two
gives hermetic tests with no root, no network, and no dependence on what
happens to be installed on the machine running them.
"""

import configparser
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hc.utils import load_config  # noqa: E402


class FakeShell:
    """Stand-in for hc.utils.run().

    Commands are matched by substring, first rule wins. Anything unmatched
    returns empty success, which mirrors how the real checks treat a command
    that produced no output.
    """

    def __init__(self):
        self.rules: list = []
        self.calls: list = []

    def expect(self, substring: str, out: str = "", rc: int = 0, err: str = "",
               once: bool = False):
        """Route commands containing `substring` to this result.

        `once=True` consumes the rule after one match, so repeated calls to the
        same command can return different output — which is what it takes to
        test code that deliberately reads a source twice (see the hidden-process
        race check).
        """
        self.rules.append([substring, (rc, out, err), once])
        return self

    def __call__(self, cmd: str, timeout: int = 30):
        self.calls.append(cmd)
        for rule in self.rules:
            substring, result, once = rule
            if substring in cmd:
                if once:
                    self.rules.remove(rule)
                return result
        return (0, "", "")

    def ran(self, substring: str) -> bool:
        return any(substring in c for c in self.calls)


@pytest.fixture
def shell(monkeypatch):
    """A FakeShell wired into hc.checks."""
    import hc.checks
    fake = FakeShell()
    monkeypatch.setattr(hc.checks, "run", fake)
    return fake


@pytest.fixture
def tools(monkeypatch):
    """Control which system tools appear installed. Defaults to none."""
    import hc.checks
    installed: set = set()

    def fake_has(cmd: str) -> bool:
        return cmd in installed

    monkeypatch.setattr(hc.checks, "has", fake_has)
    return installed


@pytest.fixture
def cfg(monkeypatch) -> configparser.ConfigParser:
    """Config built from the built-in defaults only.

    CONFIG_PATH is pointed at a file that does not exist so that a developer's
    own healthcheck.conf can never change a test result.
    """
    import hc.utils
    monkeypatch.setattr(hc.utils, "CONFIG_PATH", pathlib.Path("/nonexistent/healthcheck.conf"))
    return load_config()


@pytest.fixture
def no_state(monkeypatch):
    """Isolate state: reads return a value the test controls, writes are captured."""
    import hc.checks
    store: dict = {}

    monkeypatch.setattr(hc.checks, "load_state", lambda name: store.get(name))
    monkeypatch.setattr(hc.checks, "save_state", lambda name, data: store.__setitem__(name, data))
    return store


def alert_messages(section) -> list:
    return [m for _, m in section.alert_lines]
