"""Minimal OpenAI-compatible /v1 chat client (stdlib urllib).

Talks to whatever serves the contract — Ollama (:11434) today, MLC or anything else later. The
copilot only needs two things: a single chat round that may return tool calls, and a way to
report reachability. Kept dependency-free so it runs on a bare Orin.
"""
import json
import urllib.error
import urllib.request

from shed import guard as shed

from . import config


class LLMUnavailable(Exception):
    pass


class PromptTooLong(LLMUnavailable):
    """The request would not fit the context window. Retrying can't help."""


class NarrationPaused(LLMUnavailable):
    """The Orin is shedding load (#248, level 1+): no LLM call; callers fall back to the engine."""


def check_shed():
    """Refuse any LLM call while the load-shed guard has narration paused (pi/orin/shed). Raising
    LLMUnavailable sends every caller down its deterministic fallback, same as a too-long prompt."""
    why = shed.paused("narration")
    if why:
        raise NarrationPaused(why)


def estimate_tokens(messages, tools=None) -> int:
    """Rough prompt size in tokens (chars / LLM_CHARS_PER_TOKEN). Tool specs count: the server
    renders them into the prompt."""
    chars = len(json.dumps(messages, ensure_ascii=False))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    return int(chars / config.LLM_CHARS_PER_TOKEN) + 1


def check_budget(messages, tools=None, num_ctx=None):
    """Refuse a request that would not fit the context window. Ollama silently truncates instead of
    failing, so without this an over-long prompt loses its middle (the seed facts) and the model
    still answers. Raising LLMUnavailable sends every caller down its deterministic fallback."""
    num_ctx = num_ctx or config.LLM_NUM_CTX
    need = estimate_tokens(messages, tools) + config.LLM_REPLY_RESERVE
    if need > num_ctx:
        raise PromptTooLong(f"prompt too long: ~{need} tokens incl. reply reserve "
                             f"> context {num_ctx}")
    return need


class LLMClient:
    def __init__(self, base_url=None, model=None, timeout=None):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL
        self.timeout = timeout if timeout is not None else config.LLM_TIMEOUT
        self.num_ctx = config.LLM_NUM_CTX

    def _post(self, path: str, payload: dict) -> dict:
        url = self.base_url + path
        body = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer local",  # local servers ignore it; keep clients happy
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMUnavailable(str(e)) from e
        except json.JSONDecodeError as e:
            raise LLMUnavailable(f"bad JSON from LLM: {e}") from e

    def chat(self, messages, tools=None, tool_choice="auto", json_object=False,
             schema=None, temperature=None) -> dict:
        """One chat round. Returns the assistant message dict, which may contain `tool_calls`.
        `schema` (a JSON Schema) forces schema-valid JSON output (constrained decoding) — the most
        reliable structured-output mode; `json_object=True` is the looser JSON-only fallback."""
        check_shed()
        check_budget(messages, tools, self.num_ctx)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if schema:
            payload["response_format"] = {"type": "json_schema",
                                          "json_schema": {"name": "response", "schema": schema}}
        elif json_object:
            payload["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", payload)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMUnavailable(f"unexpected LLM response shape: {str(data)[:300]}") from e

    def chat_stream(self, messages, temperature=None):
        """Stream a chat completion, yielding content deltas as they arrive (SSE). Used for the
        dashboard tile deep-dive so words appear token-by-token instead of after a long blank."""
        check_shed()
        check_budget(messages, None, self.num_ctx)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "stream": True,
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer local",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMUnavailable(str(e)) from e

    def reachable(self) -> bool:
        try:
            self._post_models()
            return True
        except LLMUnavailable:
            return False

    def _post_models(self):
        url = self.base_url + "/models"
        try:
            with urllib.request.urlopen(url, timeout=min(self.timeout, 8)) as r:
                json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            raise LLMUnavailable(str(e)) from e
