import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DOCKER_HUB_API = "https://hub.docker.com/v2"

DEPRECATED_IMAGES = {
    "openjdk:17-jdk-slim": "eclipse-temurin:17-jdk",
    "openjdk:17-jre-slim": "eclipse-temurin:17-jre",
    "openjdk:11-jdk-slim": "eclipse-temurin:11-jdk",
    "openjdk:11-jre-slim": "eclipse-temurin:11-jre",
    "openjdk:17-slim": "eclipse-temurin:17-jdk",
    "openjdk:11-slim": "eclipse-temurin:11-jdk",
    "openjdk": "eclipse-temurin:17-jdk",
}

LANGUAGE_TO_IMAGE = {
    "python": ("python", "3.12-slim"),
    "node": ("node", "22-alpine"),
    "nodejs": ("node", "22-alpine"),
    "javascript": ("node", "22-alpine"),
    "typescript": ("node", "22-alpine"),
    "java": ("eclipse-temurin", "17-jdk"),
    "kotlin": ("eclipse-temurin", "17-jdk"),
    "go": ("golang", "1.22-alpine"),
    "golang": ("golang", "1.22-alpine"),
    "rust": ("rust", "1.75-slim"),
    "ruby": ("ruby", "3.3-slim"),
    "php": ("php", "8.2-fpm"),
    "dotnet": ("mcr.microsoft.com/dotnet/sdk", "8.0"),
    "csharp": ("mcr.microsoft.com/dotnet/sdk", "8.0"),
    "c#": ("mcr.microsoft.com/dotnet/sdk", "8.0"),
    "elixir": ("elixir", "1.16-otp-26"),
    "erlang": ("erlang", "26"),
    "scala": ("eclipse-temurin", "17-jdk"),
    "clojure": ("clojure", "latest"),
    "swift": ("swift", "5.9"),
    "dart": ("dart", "3.2"),
    "flutter": ("ghcr.io/nextevo/flutter", "latest"),
    "perl": ("perl", "5.40-slim"),
    "lua": ("lua", "5.4"),
    "haskell": ("haskell", "9.6"),
    "c": ("gcc", "13"),
    "c++": ("gcc", "13"),
    "cpp": ("gcc", "13"),
    "zig": ("zig", "0.12"),
    "nim": ("nimrod", "2.0"),
    "crystal": ("crystal", "1.11"),
    "deno": ("deno", "1.40"),
    "bun": ("oven/bun", "1.0"),
    "r": ("r-base", "4.3"),
    "julia": ("julia", "1.10"),
    "kotlin": ("eclipse-temurin", "17-jdk"),
    "groovy": ("gradle", "8.5"),
}

# 파일 패턴에서 언어 추론
FILE_PATTERN_TO_LANGUAGE = {
    "Cargo.toml": "rust",
    "Gemfile": "ruby",
    "composer.json": "php",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "*.csproj": "dotnet",
    "*.sln": "dotnet",
    "mix.exs": "elixir",
    "Package.swift": "swift",
    "pubspec.yaml": "dart",
    "CMakeLists.txt": "c++",
    "Makefile": "c",
    "setup.py": "python",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "package.json": "node",
    "*.rs": "rust",
    "*.rb": "ruby",
    "*.php": "php",
    "*.go": "go",
    "*.java": "java",
    "*.kt": "kotlin",
    "*.cs": "dotnet",
    "*.ex": "elixir",
    "*.swift": "swift",
    "*.dart": "dart",
    "*.c": "c",
    "*.cpp": "c++",
    "*.h": "c++",
    "*.zig": "zig",
    "*.nim": "nim",
    "*.cr": "crystal",
    "*.r": "r",
    "*.jl": "julia",
    "*.clj": "clojure",
}


class DockerHubVerifier:
    def __init__(self) -> None:
        self.api_base = DOCKER_HUB_API

    async def verify(self, image: str, tag: str) -> dict:
        """Docker Hub에서 이미지 태그 존재 여부 검증"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.api_base}/repositories/library/{image}/tags/{tag}"
                )
                if response.status_code == 200:
                    data = response.json()
                    return {
                        "exists": True,
                        "image": f"{image}:{tag}",
                        "last_updated": data.get("last_updated", "unknown"),
                        "size": data.get("full_size", 0),
                    }
                elif response.status_code == 404:
                    return {
                        "exists": False,
                        "image": f"{image}:{tag}",
                        "error": "Tag not found",
                        "suggestion": f"Try: docker pull {image} --all-tags to see available",
                    }
                else:
                    return {
                        "exists": False,
                        "image": f"{image}:{tag}",
                        "error": f"HTTP {response.status_code}",
                    }
        except Exception as e:
            logger.error(f"Docker Hub API error: {e}")
            return {
                "exists": False,
                "image": f"{image}:{tag}",
                "error": str(e),
            }

    def get_replacement(self, deprecated_image: str) -> Optional[str]:
        """폐기된 이미지의 대체 이미지 반환"""
        return DEPRECATED_IMAGES.get(deprecated_image.lower())

    def is_deprecated(self, image: str) -> bool:
        """폐기된 이미지인지 확인"""
        return image.lower() in DEPRECATED_IMAGES

    def suggest_image(self, language: str) -> Optional[str]:
        """언어 이름으로 권장 이미지 제안"""
        lang_lower = language.lower()
        if lang_lower in LANGUAGE_TO_IMAGE:
            image, tag = LANGUAGE_TO_IMAGE[lang_lower]
            return f"{image}:{tag}"
        return None

    def detect_language_from_files(self, files: set[str]) -> Optional[str]:
        """파일 목록에서 언어 추론"""
        for pattern, lang in FILE_PATTERN_TO_LANGUAGE.items():
            if pattern.startswith("*."):
                # 확장자 패턴
                ext = pattern[1:]  # "*.rs" -> ".rs"
                if any(f.endswith(ext) for f in files):
                    return lang
            else:
                # 정확한 파일명 패턴
                if pattern in files:
                    return lang
        return None
