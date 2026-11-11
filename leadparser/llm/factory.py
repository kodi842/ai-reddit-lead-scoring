from __future__ import annotations

from ..config import LLMConfig
from .base import LLMBackend, LLMError
from .claude_code import ClaudeCodeBackend
from .openai_compat import OpenAICompatBackend


def build_backends(llm_cfg: LLMConfig) -> dict[str, LLMBackend]:
    """Создаёт только те бэкенды, на которые ссылается хотя бы одна ступень."""
    used = {stage.backend for stage in llm_cfg.stages.values()}
    backends: dict[str, LLMBackend] = {}
    for name in sorted(used):
        conf = llm_cfg.backend_conf(name)
        if conf.type == "claude_code":
            backends[name] = ClaudeCodeBackend()
        elif conf.type == "openai_compat":
            if not conf.api_key:
                raise LLMError(
                    f"бэкенд {name!r} требует ключ в переменной {conf.api_key_env} — "
                    "добавьте её в .env"
                )
            backends[name] = OpenAICompatBackend(
                base_url=conf.base_url or "", api_key=conf.api_key
            )
        else:
            raise LLMError(f"неизвестный тип бэкенда: {conf.type!r}")
    return backends
