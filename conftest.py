import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(settings):
    """Tests use an in-process cache, so throttling works without Redis and resets per test."""
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    from django.core.cache import cache

    cache.clear()
