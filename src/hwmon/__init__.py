"""hwmon — lightweight self-hosted hardware monitor."""

from .config import Config, package_version

__all__ = ["Config", "package_version"]
__version__ = package_version()
