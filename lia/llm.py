"""Cliente de LLM compatível com a API de chat da OpenAI.

Funciona com qualquer provedor que aceite POST /chat/completions, incluindo
Pollinations (grátis e sem chave), Ollama, Groq, Gemini, LM Studio etc.
"""

from __future__ import annotations

import json
import threading
from typing import Iterator

import requests

from .config import Provider, Settings, all_providers


class LLMError(RuntimeError):
    pass


def _headers(provider: Provider) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    headers.update(provider.extra_headers)
    return headers


def _extract(obj: dict) -> str:
    """Extrai o texto de uma resposta (streaming ou completa)."""
    choices = obj.get("choices") or []
    for choice in choices:
        delta = choice.get("delta") or {}
        if isinstance(delta, dict) and delta.get("content"):
            return delta["content"]
        message = choice.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            return message["content"]
        if choice.get("text"):
            return choice["text"]
    # Alguns servidores devolvem {"error": {...}} com status 200.
    if obj.get("error"):
        raise LLMError(str(obj["error"])[:300])
    return ""


def stream_chat(
    provider: Provider,
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 320,
    cancel: threading.Event | None = None,
) -> Iterator[str]:
    if provider.needs_key and not provider.api_key:
        raise LLMError(f"{provider.name}: falta a chave de API")

    payload = {
        "model": provider.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    try:
        response = requests.post(
            provider.url,
            json=payload,
            headers=_headers(provider),
            stream=True,
            timeout=(10, 90),
        )
    except requests.RequestException as exc:
        raise LLMError(f"{provider.name}: sem conexão ({exc.__class__.__name__})") from exc

    with response:
        if response.status_code >= 400:
            snippet = response.text[:300].replace("\n", " ")
            raise LLMError(f"{provider.name}: HTTP {response.status_code} {snippet}")

        content_type = response.headers.get("content-type", "")
        if "json" in content_type and "event-stream" not in content_type:
            # Servidor ignorou stream=True e devolveu tudo de uma vez.
            try:
                data = response.json()
            except ValueError as exc:
                raise LLMError(f"{provider.name}: resposta inválida") from exc
            text = _extract(data)
            if text:
                yield text
            return

        emitted = False
        for raw in response.iter_lines(decode_unicode=True):
            if cancel is not None and cancel.is_set():
                break
            if not raw:
                continue
            line = raw.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            if line.startswith(":"):  # comentário/keep-alive de SSE
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Alguns provedores mandam texto puro em vez de SSE.
                if not line.startswith("{"):
                    emitted = True
                    yield line
                continue

            text = _extract(obj)
            if text:
                emitted = True
                yield text
            finish = obj.get("choices") or [{}]
            if finish[0].get("finish_reason"):
                # Não interrompe: alguns servidores mandam um último pedaço depois.
                pass

        if not emitted:
            raise LLMError(f"{provider.name}: resposta vazia")


class LLM:
    """Encadeia provedores: se um falhar, tenta o próximo."""

    def __init__(self, settings: Settings):
        self.settings = settings
        registry = all_providers()
        names = [settings.provider.strip()]
        names += [n.strip() for n in settings.fallback.split(",") if n.strip()]
        # remove duplicados/vazios mantendo a ordem
        self.order: list[str] = []
        for name in names:
            if name and name not in self.order:
                self.order.append(name)

        self.providers = registry
        self.history: list[dict] = []
        self._lock = threading.Lock()

        for name in self.order:
            if name not in registry:
                raise LLMError(
                    f"provedor desconhecido: {name!r} "
                    f"(use: {', '.join(registry)})"
                )

    @property
    def active_label(self) -> str:
        first = self.order[0]
        provider = self.providers[first]
        return f"{provider.name} / {provider.model}"

    def build_messages(self, user_text: str) -> list[dict]:
        with self._lock:
            history = list(self.history)
        return (
            [{"role": "system", "content": self.settings.system_prompt}]
            + history
            + [{"role": "user", "content": user_text}]
        )

    def stream(
        self,
        user_text: str,
        cancel: threading.Event | None = None,
    ) -> Iterator[str]:
        messages = self.build_messages(user_text)
        errors: list[str] = []

        for name in self.order:
            provider = self.providers[name]
            produced = False
            try:
                for token in stream_chat(
                    provider,
                    messages,
                    temperature=self.settings.temperature,
                    max_tokens=self.settings.max_tokens,
                    cancel=cancel,
                ):
                    if cancel is not None and cancel.is_set():
                        return
                    produced = True
                    yield token
                if produced:
                    return
                errors.append(f"{name}: vazio")
            except LLMError as exc:
                errors.append(str(exc))
                if produced:
                    # Já falamos parte da resposta; trocar de modelo duplicaria texto.
                    raise
            except Exception as exc:  # rede, timeout, JSON inesperado...
                errors.append(f"{name}: {exc.__class__.__name__}: {exc}")
                if produced:
                    raise LLMError(str(exc)) from exc

        raise LLMError(" | ".join(errors))

    def commit(self, user_text: str, assistant_text: str) -> None:
        if not assistant_text.strip():
            return
        with self._lock:
            self.history.append({"role": "user", "content": user_text})
            self.history.append({"role": "assistant", "content": assistant_text.strip()})
            limit = max(0, self.settings.history_turns) * 2
            if limit and len(self.history) > limit:
                self.history = self.history[-limit:]

    def reset(self) -> None:
        with self._lock:
            self.history.clear()


def check_provider(name: str, settings: Settings | None = None) -> str:
    """Teste rápido usado pelo verify.py e pela interface."""
    provider = all_providers()[name]
    text = "".join(
        stream_chat(
            provider,
            [{"role": "user", "content": "Responda apenas: ok"}],
            max_tokens=16,
        )
    )
    return text.strip()
