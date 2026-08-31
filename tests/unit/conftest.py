import asyncio

import pytest


@pytest.fixture(autouse=True)
def run_asyncio_to_thread_inline(monkeypatch):
    """Keep unit tests deterministic and independent of thread executors."""

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)
