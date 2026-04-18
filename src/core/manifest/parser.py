from src.core.manifest.models import ManifestInfo
from src.core.manifest.parsers.base import BaseManifestParser
from src.core.manifest.parsers.unknown import UnknownManifestParser


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
                return parser.parse(store)
        return self._fallback.parse(store)
