"""Registration manifest generation: the SDK builds the manifest, developers never do.

    from pihub.registration import build_device_manifest
"""

from pihub.registration.manifest_builder import build_device_manifest

__all__ = ["build_device_manifest"]
