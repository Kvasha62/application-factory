"""Factory-defined canonical artifact representations."""

from .container_image import build_container_image_canonical
from .source_package import build_source_package_canonical

__all__ = [
    "build_container_image_canonical",
    "build_source_package_canonical",
]
