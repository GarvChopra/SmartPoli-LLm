import gc

import pytest


@pytest.fixture(autouse=True)
def _free_tk_objects_on_the_main_thread():
    """Tk objects must be garbage-collected on the thread that created them. If a Tk window from one
    test is left for the collector, a later test's worker thread can trigger the collection and Tcl
    kills the whole process ("Windows fatal exception 0x80000003"). Collecting here, on the main
    thread right after each test, rules that out."""
    yield
    gc.collect()
