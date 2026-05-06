import gc
import pytest


@pytest.fixture(autouse=True)
def _gc_after_test():
    yield
    gc.collect()
