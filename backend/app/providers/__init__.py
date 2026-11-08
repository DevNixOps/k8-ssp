from ..config import settings
from .base import Provider

_provider: Provider | None = None


def get_provider() -> Provider:
    global _provider
    if _provider is None:
        if settings.mock_mode:
            from .mock import MockProvider

            _provider = MockProvider()
        else:
            from .real import RealProvider

            _provider = RealProvider()
    return _provider
