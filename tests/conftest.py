import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src"), os.path.join(ROOT, "domains")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest

from e2e_helpers import make_app


@pytest.fixture()
def build_medical_app():
    return make_app()


@pytest.fixture()
def build_ops_app():
    return make_app()
