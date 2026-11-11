"""Test helper: test-only boats (vps/lab/testdata/boats/), never shipped.

The Lab serves vps/lab/boats/ (the image's /srv/boats) as the live boat library, so a made-up
boat there shows up in the Lab and on the iPad picker. Test boats such as `bravo` (the second boat
the modularity tests run, #315) live in testdata/boats/ instead. `install()` points the Lab's boat
library at a temp folder holding the shipped boats plus the test boats, for one test process.
"""
import glob
import json
import os
import shutil
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
TEST_BOATS = os.path.join(_HERE, "testdata", "boats")


def path(bid):
    """The profile file of a test-only boat (e.g. "bravo")."""
    return os.path.join(TEST_BOATS, f"{bid}.json")


def load(bid):
    with open(path(bid)) as f:
        return json.load(f)


def install():
    """The Lab's boat library (app.boats) serves the shipped boats plus the test boats, from a
    temp folder. Returns that folder. Call after `from app import boats`."""
    from app import boats
    tmp = tempfile.mkdtemp(prefix="c4-test-boats-")
    for d in (boats.BUNDLED_DIR, TEST_BOATS):
        for f in sorted(glob.glob(os.path.join(d, "*.json"))):
            shutil.copy(f, tmp)
    boats.BUNDLED_DIR = tmp
    os.environ["BOATS_DIR"] = tmp
    return tmp
