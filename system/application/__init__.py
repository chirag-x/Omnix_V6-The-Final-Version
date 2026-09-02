"""
Omnix V6 — System Application package.

Exposes the resolver-driven application service, the discovery
sources, the catalog, and the resolution data models.  This is the
single import surface for everything that needs to discover or launch
a Windows application.
"""

from .models import ApplicationRecord, Resolution
from .discovery import (
    ApplicationSource,
    PathSource,
    RegistryUninstallSource,
    AppPathsSource,
    StartMenuSource,
    ProcessSource,
    default_sources,
    normalize_name,
)
from .catalog import ApplicationCatalog, CatalogStats
from .resolver import ApplicationResolver
from .app_service import WindowsApplicationService

__all__ = [
    "ApplicationRecord",
    "Resolution",
    "ApplicationSource",
    "PathSource",
    "RegistryUninstallSource",
    "AppPathsSource",
    "StartMenuSource",
    "ProcessSource",
    "default_sources",
    "normalize_name",
    "ApplicationCatalog",
    "CatalogStats",
    "ApplicationResolver",
    "WindowsApplicationService",
]
