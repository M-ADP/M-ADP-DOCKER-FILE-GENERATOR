import re


class PortDetector:
    _PATTERNS = [
        r"\.listen\(\s*(\d{4,5})",
        r"uvicorn\.run\(.*?port\s*=\s*(\d+)",
        r"app\.run\(.*?port\s*=\s*(\d+)",
        r"server\.port\s*[=:]\s*(\d+)",
        r"port\s*:=\s*(\d{4,5})",
        r'PORT\s*=\s*["\']?(\d{4,5})',
        r"\.Addr\s*=\s*.*:(\d{4,5})",
    ]

    def detect(self, store: dict[str, str]) -> int | None:
        for content in store.values():
            for pattern in self._PATTERNS:
                m = re.search(pattern, content, re.IGNORECASE | re.DOTALL)
                if m:
                    return int(m.group(1))
        return None
