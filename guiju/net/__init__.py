"""Single outbound layer for guiju.

C1 exports only symbols that exist in C1. BodyClient/DownloadResult are added
when C2 creates body.py.
"""

from .block import BLOCK_MARKERS, BlockDetector
from .endpoints import DEFAULT_METADATA_ENDPOINTS
from .headers import canonical_host, header_int, parse_content_range
from .metadata import MetadataClient
from .policy import BodyPolicy, MetadataPolicy, normalize_path
from .redirect import RedirectGuard
from .request import ProbeInfo, ProbeRecord, Request, Response
from .throttle import Throttle, default_throttle
from .transport import Transport, build_opener

__all__ = [
    "BLOCK_MARKERS",
    "BlockDetector",
    "BodyPolicy",
    "DEFAULT_METADATA_ENDPOINTS",
    "MetadataClient",
    "MetadataPolicy",
    "ProbeInfo",
    "ProbeRecord",
    "RedirectGuard",
    "Request",
    "Response",
    "Throttle",
    "Transport",
    "build_opener",
    "canonical_host",
    "default_throttle",
    "header_int",
    "normalize_path",
    "parse_content_range",
]
