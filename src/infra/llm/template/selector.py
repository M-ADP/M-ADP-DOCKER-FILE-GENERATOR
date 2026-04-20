"""
BuildParams → 렌더링 함수 선택.
None 반환 시 LLM fallback 경로 사용.
"""
from collections.abc import Callable

from src.core.params.models import BuildParams
from src.infra.llm.template.base import (
    render_go,
    render_java,
    render_nextjs,
    render_nextjs_standalone,
    render_node_server,
    render_nuxt,
    render_python,
    render_static,
)

_STATIC_FRAMEWORKS = frozenset({"node-static", "vite-static", "astro", "vue", "svelte"})
_JAVA_FRAMEWORKS   = frozenset({"java-gradle", "java-maven"})

RenderFn = Callable[[BuildParams], str]


def select(params: BuildParams) -> RenderFn | None:
    """적합한 렌더링 함수를 반환. 미지원 스택이면 None."""
    d  = params.detected
    fw = d.framework

    if fw == "nextjs":
        return render_nextjs_standalone if d.standalone else render_nextjs
    if fw == "nuxt":
        return render_nuxt
    if fw in _STATIC_FRAMEWORKS:
        return render_static
    if fw in ("python-fastapi", "python-flask", "python"):
        return render_python
    if fw == "go":
        return render_go
    if fw in _JAVA_FRAMEWORKS:
        return render_java
    if fw == "node-server":
        return render_node_server

    return None  # → LLM fallback
