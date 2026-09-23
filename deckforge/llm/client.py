"""Клиент к OpenAI-совместимому inference API; модели по ролям — из `.env`: LLM_MODEL, VLM_MODEL, T2I_MODEL."""

from __future__ import annotations

import base64
import copy
import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

from .skills import Skill


@dataclass
class LLMCall:
    """Запись об одном вызове — для manifest.json и отладки."""

    skill: str
    model: str
    duration_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ok: bool = True
    error: str | None = None


@dataclass
class LLMClient:
    base_url: str = field(default_factory=lambda: os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"))
    api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""))
    text_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b-20260814"))
    vision_model: str = field(default_factory=lambda: os.environ.get("VLM_MODEL", "qwen/qwen3.8-27b-20260814"))
    # text-to-image: по умолчанию тот же провайдер и ключ; можно указать другой с /images/generations
    image_base_url: str = field(default_factory=lambda: os.environ.get("T2I_BASE_URL", ""))
    image_api_key: str = field(default_factory=lambda: os.environ.get("T2I_API_KEY", ""))
    image_model: str = field(default_factory=lambda: os.environ.get("T2I_MODEL", "black-forest-labs/flux.2-klein-4b"))
    timeout_s: float = 120.0
    retries: int = 2
    # общий лимит одновременных запросов к провайдеру (все колоды прогона вместе); 0 — без лимита
    max_concurrency: int = field(default_factory=lambda: _env_int("LLM_MAX_CONCURRENCY", 0))
    calls: list[LLMCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._parent: LLMClient | None = None
        self._sem = threading.BoundedSemaphore(self.max_concurrency) if self.max_concurrency > 0 else None
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key or "missing", timeout=self.timeout_s)
        # пустые T2I_* = использовать LLM-провайдер и его ключ
        self.image_base_url = self.image_base_url or self.base_url
        self.image_api_key = self.image_api_key or (self.api_key if self.image_base_url == self.base_url else "")
        self._image_client = OpenAI(base_url=self.image_base_url, api_key=self.image_api_key or "missing",
                                    timeout=self.timeout_s)

    @property
    def images_enabled(self) -> bool:
        return bool(self.image_api_key and self.image_model)

    def model_for(self, role: str) -> str:
        return {"text": self.text_model, "vision": self.vision_model, "image": self.image_model}[role]

    def fork(self) -> LLMClient:
        """Тот же клиент (соединения, лимит `max_concurrency`), но свой журнал `calls` — для manifest колоды.

        Колоды прогона собираются параллельно: у каждой свой форк, а вызов пишется и в журнал родителя,
        так что у родителя по-прежнему все вызовы прогона."""
        child = copy.copy(self)
        child.calls = []
        child._parent = self
        return child

    def _log(self, call: LLMCall) -> None:
        self.calls.append(call)
        if self._parent is not None:
            self._parent._log(call)

    @contextmanager
    def _slot(self, deadline: float | None) -> Iterator[None]:
        """Место под запрос в общем лимите `max_concurrency`; в очереди ждём не дольше дедлайна."""
        if self._sem is None:
            yield
            return
        wait_s = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self._sem.acquire(timeout=wait_s):
            raise TimeoutError("бюджет времени исчерпан в очереди к провайдеру (LLM_MAX_CONCURRENCY)")
        try:
            yield
        finally:
            self._sem.release()

    @staticmethod
    def no_think_extra() -> dict[str, Any]:
        """Параметры, выключающие «размышления» модели; переопределяются через LLM_NO_THINK_JSON."""
        raw = os.environ.get("LLM_NO_THINK_JSON")
        if raw:
            return json.loads(raw)
        return {"reasoning": {"enabled": False}, "chat_template_kwargs": {"enable_thinking": False}}

    # ── основной вызов скилла ────────────────────────────────────────────────
    def run_skill(self, skill: Skill, images: list[Path] | None = None, deadline: float | None = None,
                  **inputs: Any) -> dict | str:
        """Выполняет скилл; если у скилла есть schema — возвращает распарсенный и валидный JSON.

        `deadline` — момент `time.monotonic()`, к которому ответ должен быть: таймаут запроса урезается до остатка,
        повторных попыток после дедлайна нет (бюджет времени прогона важнее ответа). При `max_concurrency`
        запрос ждёт места в очереди не дольше дедлайна.
        """
        system, user = skill.render(**{k: _as_text(v) for k, v in inputs.items()})
        content: Any = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": _data_url(p)}} for p in images
            ]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        model = self.model_for(skill.model_role)
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": skill.temperature,
                                  "max_tokens": skill.max_tokens}
        if not skill.reasoning:
            kwargs["extra_body"] = self.no_think_extra()
        if skill.schema:
            # json_schema поддерживают не все провайдеры — валидируем сами
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            t0 = time.perf_counter()
            if deadline is not None and deadline - time.monotonic() <= 0:
                last_err = TimeoutError("бюджет времени исчерпан" + (f" (после: {last_err})" if last_err else ""))
                self._log(LLMCall(skill.id, model, 0.0, ok=False, error=str(last_err)[:200]))
                break
            try:
                with self._slot(deadline):
                    # остаток до дедлайна — после ожидания в очереди лимита
                    resp = self._until(deadline).chat.completions.create(**kwargs)
                text = resp.choices[0].message.content or ""
                usage = resp.usage
                self._log(LLMCall(skill.id, model, time.perf_counter() - t0,
                                  usage.prompt_tokens if usage else 0,
                                  usage.completion_tokens if usage else 0))
                if skill.schema:
                    return _parse_json(text)
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                self._log(LLMCall(skill.id, model, time.perf_counter() - t0, ok=False, error=str(e)[:200]))
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"skill {skill.id} failed after {self.retries + 1} attempts: {last_err}")

    def _until(self, deadline: float | None) -> OpenAI:
        """Клиент с таймаутом запроса, урезанным до остатка до дедлайна."""
        if deadline is None:
            return self._client
        left = deadline - time.monotonic()
        return self._client if left >= self.timeout_s else self._client.with_options(timeout=max(1.0, left))

    def generate_image(self, prompt: str, out_path: Path, size: str = "1024x576",
                       deadline: float | None = None) -> Path:
        """Text-to-image; расширение out_path подгоняется под media_type ответа. `deadline` — как у run_skill."""
        if not self.images_enabled:
            raise RuntimeError("генерация изображений отключена (нет ключа или T2I_MODEL пуст)")
        if deadline is not None and deadline - time.monotonic() <= 0:
            raise TimeoutError("бюджет времени исчерпан")
        t0 = time.perf_counter()
        with self._slot(deadline):
            raw, media = self._t2i(prompt, size, deadline)
        ext = ".jpg" if "jpeg" in media or "jpg" in media else ".png"
        out_path = out_path.with_suffix(ext)
        out_path.write_bytes(raw)
        self._log(LLMCall("image_gen", self.image_model, time.perf_counter() - t0))
        return out_path

    def _t2i(self, prompt: str, size: str, deadline: float | None) -> tuple[bytes, str]:
        """Запрос к T2I-провайдеру → (байты, media_type); таймаут — не дальше дедлайна."""
        timeout = self.timeout_s
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("бюджет времени исчерпан")
            timeout = min(timeout, max(1.0, left))
        if "openrouter.ai" in self.image_base_url:
            import httpx

            r = httpx.post(
                self.image_base_url.rstrip("/") + "/images",
                headers={"Authorization": f"Bearer {self.image_api_key}"},
                json={"model": self.image_model, "prompt": prompt, "size": size},
                timeout=timeout,
            )
            r.raise_for_status()
            item = r.json()["data"][0]
            raw = base64.b64decode(item["b64_json"])
            media = item.get("media_type", "image/png")
        else:
            client = self._image_client if timeout == self.timeout_s else self._image_client.with_options(timeout=timeout)
            resp = client.images.generate(model=self.image_model, prompt=prompt, size=size, n=1,
                                          response_format="b64_json")
            item = resp.data[0]
            if item.b64_json:
                raw = base64.b64decode(item.b64_json)
            else:
                import httpx

                raw = httpx.get(item.url, timeout=60).content
            media = "image/png"
        return raw, media


# ── утилиты ───────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _as_text(v: Any) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, indent=1)
    return str(v)


def _data_url(p: Path) -> str:
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def _parse_json(text: str) -> dict:
    """Вырезать первый JSON-объект из ответа с обёрткой или лишним текстом."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
