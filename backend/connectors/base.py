from abc import ABC, abstractmethod

from backend.models import Account, Daily, Resource


class AdvertisingConnector(ABC):
    @abstractmethod
    def discover(self) -> list[Account]: ...

    @abstractmethod
    def campaigns(self, account: Account) -> list[Resource]: ...

    @abstractmethod
    def content(self, account: Account) -> dict[str, list[Resource]]: ...

    @abstractmethod
    def performance(self, account: Account, start, end) -> tuple[list[Daily], list[Resource]]: ...


class MetaAdsConnector(AdvertisingConnector):
    """Adapter contract reserved for a future Meta implementation."""


class TikTokAdsConnector(AdvertisingConnector):
    """Adapter contract reserved for a future TikTok implementation."""
