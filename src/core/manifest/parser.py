import logging

from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.parsers.unknown import UnknownManifestParser

logger = logging.getLogger(__name__)


class ManifestParser:
    def __init__(
        self,
        parsers: list[BaseManifestParser],
        fallback: UnknownManifestParser,
    ) -> None:
        self._parsers = parsers
        self._fallback = fallback

    def parse(self, store: dict[str, str]) -> ManifestInfo:
        for parser in self._parsers:
            if parser.can_handle(store):
                result = parser.parse(store)
                logger.info(
                    f"[ManifestParser] parser={type(parser).__name__}, "
                    f"language={result.language}, "
                    f"runtime={result.runtime_version}, "
                    f"pkg_manager={result.pkg_manager}, "
                    f"entry_point={result.entry_point}, "
                    f"port={result.detected_port}, "
                    f"deps={len(result.dependencies)}"
                )
                return result
        result = self._fallback.parse(store)
        logger.info(f"[ManifestParser] fallback → language={result.language}")
        return result
