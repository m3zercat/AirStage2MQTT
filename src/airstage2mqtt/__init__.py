"""AirStage2MQTT package."""

import os
from importlib.metadata import PackageNotFoundError, version

try:
    _PACKAGE_VERSION = version("airstage2mqtt")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    _PACKAGE_VERSION = "0.1.0"

__version__ = os.getenv("A2M_VERSION", _PACKAGE_VERSION)

__all__ = ["__version__"]
