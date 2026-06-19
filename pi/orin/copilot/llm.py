"""Minimal OpenAI-compatible /v1 chat client (stdlib urllib).

Talks to whatever serves the contract — Ollama (:11434) today, MLC or anything else later. The
copilot only needs two things: a single chat round that may return tool calls, and a way to
report reachability. Kept dependency-free so it runs on a bare Orin.
"""
import json
import urllib.error
import urllib.request

from . import config


class LLMUnavailable(Exception):
    pass


class LLMClient:
    def __init__(self, base_url=None, model=None, timeout=None):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL
        self.timeout = timeout if timeout is not None else config.LLM_TIMEOUT

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
             temperature=None) -> dict:
        """One chat round. Returns the assistant message dict, which may contain `tool_calls`.
        Set `json_object=True` to ask for a JSON-only answer (the concluding turn)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", payload)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMUnavailable(f"unexpected LLM response shape: {str(data)[:300]}") from e

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
