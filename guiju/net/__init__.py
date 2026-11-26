"""Single outbound layer for guiju."""

from .block import BLOCK_MARKERS, BlockDetector
from .body import BodyClient, finalize
from .endpoints import DEFAULT_METADATA_ENDPOINTS, DEFAULT_METADATA_FORM_HOSTS
from .headers import canonical_host, header_int, parse_content_range
from .metadata import MetadataClient
from .policy import BodyPolicy, MetadataPolicy, normalize_path
from .redirect import RedirectGuard
from .request import DownloadResult, ProbeInfo, ProbeRecord, Request, Response
from .throttle import Throttle, default_throttle
from .transport import Transport, build_opener

__all__ = [
    "BLOCK_MARKERS",
    "DEFAULT_METADATA_ENDPOINTS",
    "DEFAULT_METADATA_FORM_HOSTS",
    "BlockDetector",
    "BodyClient",
    "BodyPolicy",
    "DownloadResult",
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
    "finalize",
    "header_int",
    "normalize_path",
    "parse_content_range",
]
