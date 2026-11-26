from .alerts import AlertRepository
from .monitors import MonitorRepository
from .movies import MovieRepository
from .proxies import ProxyRepository
from .regions import RegionRepository
from .settings import SettingsRepository
from .users import UserRepository
from .watches import WatchRepository

__all__ = [
    "AlertRepository",
    "MonitorRepository",
    "MovieRepository",
    "ProxyRepository",
    "RegionRepository",
    "SettingsRepository",
    "UserRepository",
    "WatchRepository",
]
