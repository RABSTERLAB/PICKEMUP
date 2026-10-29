import os
import pathlib

# Must precede any pickemup_api import (settings read env at import time).
os.environ["PICKEMUP_ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///./data/test_pickemup.db"
os.environ.pop("PICKEMUP_API_KEY", None)

import pytest  # noqa: E402

from pickemup_api.db import engine  # noqa: E402
from pickemup_api.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
