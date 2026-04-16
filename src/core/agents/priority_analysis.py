from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class FilePriority:
    path: str
    priority: int
    reason: str


class PriorityHandler(ABC):
    def __init__(self, next_handler: Optional["PriorityHandler"] = None):
        self._next_handler = next_handler

    def handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        result = self._can_handle(path, basename, ext)
        if result:
            return result
        if self._next_handler:
            return self._next_handler.handle(path, basename, ext)
        return None

    @abstractmethod
    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        raise NotImplementedError


class LanguageDetectionHandler(PriorityHandler):
    LANGUAGE_DETECTION_FILES = {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "requirements.txt",
        "Pipfile",
        "pyproject.toml",
        "poetry.lock",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Cargo.lock",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
    }

    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        if basename in self.LANGUAGE_DETECTION_FILES:
            return FilePriority(
                path=path,
                priority=2,  # FrameworkHandler(1) 다음
                reason=f"language_detection:{basename}",
            )
        return None


class FrameworkHandler(PriorityHandler):
    """프레임워크 특정 설정 파일 - 가장 높은 우선순위"""

    FRAMEWORK_FILES = {
        # Next.js
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
        # Nuxt
        "nuxt.config.js",
        "nuxt.config.ts",
        # Vue
        "vue.config.js",
        "vue.config.ts",
        # Vite
        "vite.config.js",
        "vite.config.ts",
        # Svelte
        "svelte.config.js",
        "svelte.config.ts",
        # Astro
        "astro.config.js",
        "astro.config.mjs",
        "astro.config.ts",
        # Remix
        "remix.config.js",
        # Gatsby
        "gatsby-config.js",
        "gatsby-config.ts",
        # Django
        "manage.py",
        "settings.py",
        "urls.py",
        # Flask
        "wsgi.py",
        "asgi.py",
        # FastAPI
        "main.py",
        # Spring Boot
        "application.properties",
        "application.yml",
        "application.yaml",
        "Application.java",
        "Application.kt",
        # Rails
        "config.ru",
        "Rakefile",
        # Laravel
        "artisan",
        # Express
        "app.js",
        "server.js",
    }

    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        if basename in self.FRAMEWORK_FILES:
            return FilePriority(
                path=path,
                priority=1,
                reason=f"framework:{basename}",
            )
        return None


class EntryPointHandler(PriorityHandler):
    ENTRY_POINT_FILES = {
        "main.py",
        "app.py",
        "wsgi.py",
        "asgi.py",
        "index.js",
        "index.ts",
        "index.mjs",
        "server.js",
        "server.ts",
        "app.js",
        "app.ts",
        "main.go",
        "main.rs",
        "main.java",
        "Application.java",
        "Main.kt",
        "Program.cs",
        "index.php",
        "index.rb",
    }

    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        if basename in self.ENTRY_POINT_FILES:
            return FilePriority(
                path=path,
                priority=3,
                reason=f"entry_point:{basename}",
            )
        return None


class ConfigHandler(PriorityHandler):
    CONFIG_FILES = {
        ".env.example",
        ".env.local",
        ".env.development",
        ".env.production",
        "config.yml",
        "config.yaml",
        "config.json",
        "settings.json",
        "tsconfig.json",
        "jsconfig.json",
        ".eslintrc.js",
        ".eslintrc.json",
        ".prettierrc",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        ".dockerignore",
        "Makefile",
        "Procfile",
    }

    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        if basename in self.CONFIG_FILES:
            return FilePriority(
                path=path,
                priority=4,
                reason=f"config:{basename}",
            )
        return None


class SourceHandler(PriorityHandler):
    SOURCE_EXTENSIONS = {
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".php",
        ".rb",
        ".scala",
        ".swift",
        ".c",
        ".cpp",
        ".cc",
        ".cxx",
        ".h",
        ".hpp",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".prisma",
        ".graphql",
        ".proto",
    }

    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        if ext.lower() in self.SOURCE_EXTENSIONS:
            return FilePriority(
                path=path,
                priority=5,
                reason=f"source:{ext}",
            )
        return None


class DefaultHandler(PriorityHandler):
    def _can_handle(self, path: str, basename: str, ext: str) -> Optional[FilePriority]:
        return FilePriority(
            path=path,
            priority=99,
            reason="default:excluded",
        )


class PriorityAnalysisAgent:
    def __init__(self) -> None:
        self._chain = self._build_chain()

    def _build_chain(self) -> PriorityHandler:
        default = DefaultHandler()
        source = SourceHandler(default)
        config = ConfigHandler(source)
        entry = EntryPointHandler(config)
        lang = LanguageDetectionHandler(entry)
        framework = FrameworkHandler(lang)
        return framework

    def analyze(self, files: dict[str, str]) -> list[FilePriority]:
        priorities: list[FilePriority] = []

        for path in files.keys():
            clean_path = path.lstrip("./")
            p = Path(clean_path)
            basename = p.name
            ext = p.suffix.lower()

            result = self._chain.handle(clean_path, basename, ext)
            if result:
                priorities.append(result)

        priorities.sort(key=lambda x: (x.priority, x.path))
        return priorities

    def get_priority_paths(
        self, files: dict[str, str], max_priority: int = 4
    ) -> list[str]:
        priorities = self.analyze(files)
        return [p.path for p in priorities if p.priority <= max_priority]
