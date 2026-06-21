from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
import urllib.error
import urllib.request

from .observability import TokenUsage


@dataclass(frozen=True)
class LLMResponse:
    content: str
    token_usage: TokenUsage
    duration_ms: int
    raw: dict


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.deepseek.com",
        timeout: int = 120,
        max_tokens: int = 4096,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        return self.chat_response(model=model, messages=messages).content

    def chat_response(self, model: str, messages: list[dict[str, str]]) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeek API mode.")
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            started = time.perf_counter()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
            duration_ms = int((time.perf_counter() - started) * 1000)
        except TimeoutError as exc:
            raise RuntimeError(
                f"DeepSeek did not respond within {self.timeout}s at {self.base_url}. "
                "Try increasing --llm-timeout or using a faster model."
            ) from exc
        except socket.timeout as exc:
            raise RuntimeError(
                f"DeepSeek did not respond within {self.timeout}s at {self.base_url}. "
                "Try increasing --llm-timeout or using a faster model."
            ) from exc
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach DeepSeek at {self.base_url}.") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"Unexpected DeepSeek response: {data}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError(f"Unexpected DeepSeek response: {data}")

        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        return LLMResponse(
            content=content,
            token_usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            ),
            duration_ms=duration_ms,
            raw=data,
        )
