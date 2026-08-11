"""
Integration test scaffolding for the relay (main.py).

DATA_DIR must be redirected to a scratch directory *before* main.py is imported anywhere --
it reads os.environ["DATA_DIR"] at module import time to build MAP_ENTRIES_FILE and the bar-
profiles path, so this has to happen at the very top of conftest.py, ahead of any `import main`
(including ones pytest triggers indirectly via test collection).
"""

import os
import shutil
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="jukebar_test_")
os.environ["DATA_DIR"] = _TEST_DATA_DIR

import pytest
from fastapi.testclient import TestClient

import main as relay_main


@pytest.fixture
def client():
    """A TestClient for the real relay app, with a clean in-memory bar registry per test.

    main.py's `_bars` dict is process-global in-memory state (by design -- see BarSession's
    docstring), so tests share the same dict unless explicitly cleared between them.
    """
    with TestClient(relay_main.app) as c:
        yield c
    relay_main._bars.clear()
    relay_main._bartender_lockouts.clear()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_data_dir():
    yield
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
