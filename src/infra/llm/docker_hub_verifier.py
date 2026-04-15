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
