"""
COMPONENT: llm
DESIGN-REF: D6
PURPOSE: Provider-agnostic LLM client. Wraps litellm so any code in the
  package can call `chat(...)` without knowing the backend. The .env
  selects the provider/model; reviewers can swap to any OpenAI-compatible
  endpoint (Ollama, Groq, Together, Gemini, etc.) without code changes.
PROBLEM-STATEMENT REQ (verbatim): >
  "LLM: Use whatever you have access to — Ollama running locally, free
  tiers of hosted APIs (Groq, Together, Google AI Studio, etc.), or
  anything else. We care about how you use the model, not which one you
  pick. ... No custom LLM models we cannot access for verification."
EXPECTED INPUT: list of {role, content} chat messages plus kwargs
EXPECTED OUTPUT: dict with response.content and usage metadata
UPSTREAM: every component that needs an LLM call (expand_policy, agent,
  citation verifier, consistency reviewer, output filter for free-text)
DOWNSTREAM: litellm (provider-agnostic)
COMPONENT TESTS: tests/whitebox/test_llm.py
SCENARIO COVERAGE: indirectly all 21 (every scenario needs LLM)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_ENV_LOADED = False


def _ensure_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    repo_root = Path(__file__).resolve().parent.parent
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(repo_root / ".env.example")
    _ENV_LOADED = True


@dataclass
class ChatResult:
    content: str
    raw: Any
    model: str

    def parse_json(self) -> Any:
        """Parse JSON from the model output, tolerating common artifacts:

        - leading preamble before the JSON ("Here is the JSON: ...")
        - fenced code blocks ``` or ```json
        - top-level arrays as well as objects

        Strategy: find the first balanced JSON value (``{...}`` or
        ``[...]``) in the text and parse that. Falls back to a plain
        ``json.loads`` on the stripped text.
        """
        text = self.content.strip()
        # Strip code fences if present.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Locate the first { or [ and find its matching close.
        first_obj = text.find("{")
        first_arr = text.find("[")
        candidates = [i for i in (first_obj, first_arr) if i >= 0]
        if not candidates:
            return json.loads(text)
        start = min(candidates)
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            # Unbalanced — fall back to raw parse for clearer error.
            return json.loads(text)
        return json.loads(text[start : end + 1])


# Model context-window ceilings, used by compute_max_tokens() to derive
# a per-call max_new_tokens that respects (input + output <= context).
# When a model isn't in this table we fall back to a conservative 8192.
_MODEL_CONTEXT_CEILING: dict[str, int] = {
    # Llama 3.x family on Together / Groq
    "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo": 131_073,
    "together_ai/meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo": 131_073,
    "groq/llama-3.3-70b-versatile": 131_073,
    "groq/llama-3.1-70b-versatile": 131_073,
    "groq/llama-3.1-8b-instant": 131_073,
    # Gemini
    "gemini/gemini-2.5-flash": 1_048_576,
    "gemini/gemini-1.5-flash": 1_048_576,
    # Ollama local — typical Llama 3.1 8B context
    "ollama_chat/llama3.1:8b": 131_072,
}


def _patch_schema_unique_items(schema: Any) -> None:
    """Recursively walk a JSON Schema dict and add `uniqueItems: true` to
    every array-typed field. Required to forbid the citation/tool_call
    duplication loop documented in the plan (diagnosed from
    /tmp/agent_raw_Blue_attempt0.txt: 60 citation entries of 5 unique
    section IDs, each cycled 12 times).

    With `uniqueItems: true` and Together's json_schema mode, the
    schema-aware decoder rejects any duplicate element and forces the
    closing `]`, stopping the loop at its source.
    """
    if isinstance(schema, dict):
        if schema.get("type") == "array":
            schema["uniqueItems"] = True
        for v in schema.values():
            _patch_schema_unique_items(v)
    elif isinstance(schema, list):
        for item in schema:
            _patch_schema_unique_items(item)


def compute_max_tokens(
    messages: list[dict[str, str]],
    *,
    model: str,
    safety_margin: int = 512,
    ceiling_override: int | None = None,
) -> int:
    """Compute a max_new_tokens that respects the model's context window.

    Together AI enforces `input_tokens + max_new_tokens <= context_size`;
    other providers similarly constrain output by the remaining budget
    after the input. We use `litellm.token_counter` for input counting
    (model-aware tokenizer, falls back to tiktoken).

    Returns `ceiling - input - safety_margin`, never less than 256.
    """
    import litellm as _litellm

    ceiling = ceiling_override if ceiling_override is not None else _MODEL_CONTEXT_CEILING.get(model, 8192)
    try:
        n_input = _litellm.token_counter(model=model, messages=messages)
    except Exception:
        # Fallback: rough estimate at 3.5 chars/token
        n_input = sum(len(str(m.get("content", ""))) for m in messages) // 4
    remaining = ceiling - n_input - safety_margin
    return max(remaining, 256)


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    response_model: Any = None,
    **kwargs: Any,
) -> ChatResult:
    """Call the configured LLM and return a ChatResult.

    `model` defaults to LLM_MODEL from .env (e.g., "ollama_chat/llama3.1:8b").
    For Ollama, LLM_BASE_URL is used as api_base.

    For judge-role calls (citation verifier, consistency reviewer), prefer
    `judge_chat()` which routes to JUDGE_MODEL. Per D5/D13 (research-backed):
    8B-class models are unreliable as factual judges — JudgeBench, Patronus
    Lynx, Zheng et al. all converge on this finding.
    """
    _ensure_env()
    import litellm  # imported lazily so import-time deps stay light

    model = model or os.environ.get("LLM_MODEL", "ollama_chat/llama3.1:8b")
    api_base = kwargs.pop("api_base", None)
    # Only use LLM_BASE_URL for the local model, not arbitrary hosted models.
    if api_base is None and model.startswith(("ollama/", "ollama_chat/")):
        api_base = os.environ.get("LLM_BASE_URL")

    # Ollama on localhost: api_base is required by litellm
    call_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if api_base and model.startswith(("ollama/", "ollama_chat/")):
        call_kwargs["api_base"] = api_base

    # --- Dynamic max_tokens ---
    # Always compute from the model's context window so we never hit
    # `input + output > ceiling`. Caller-supplied max_tokens overrides
    # only when smaller (e.g. forensic diagnosis with a tight budget).
    auto_max = compute_max_tokens(messages, model=model)
    call_kwargs["max_tokens"] = min(max_tokens, auto_max) if max_tokens is not None else auto_max

    # --- Structured output ---
    # If `response_model` is a Pydantic BaseModel subclass, derive the
    # JSON Schema and pass it via `response_format={"type":"json_schema"...}`.
    # We patch the schema to mark list fields with `uniqueItems: true`,
    # which is what stops Llama-3.3-70B's citation-repetition loop:
    # the schema-aware decoder refuses to emit a duplicate, forcing the
    # closing `]` instead.
    if response_format is None and response_model is not None:
        try:
            schema = response_model.model_json_schema()
            _patch_schema_unique_items(schema)
            response_format = {
                "type": "json_schema",
                "schema": schema,
            }
        except Exception as exc:
            print(f"[llm] failed to build json_schema from response_model: {exc}; "
                  "falling back to loose json_object mode")
            response_format = {"type": "json_object"}
    if response_format is not None:
        call_kwargs["response_format"] = response_format

    # Repetition penalty as a belt-and-braces against any residual
    # looping. 1.15 is the well-established default for Together's
    # Llama models per their guides. Configurable via env.
    rep_penalty_env = os.environ.get("LLM_REPETITION_PENALTY")
    if rep_penalty_env:
        try:
            call_kwargs["repetition_penalty"] = float(rep_penalty_env)
        except ValueError:
            pass
    elif model.startswith(("together_ai/", "groq/")):
        # Default on for the providers that accept it as a top-level param.
        call_kwargs["repetition_penalty"] = 1.15

    call_kwargs.update(kwargs)

    # Hosted providers rate-limit aggressively; retry on 429 with backoff
    # parsed from the error when possible. Up to 3 attempts.
    import re as _re
    import time as _time

    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            response = litellm.completion(**call_kwargs)
            break
        except litellm.exceptions.RateLimitError as exc:  # type: ignore[attr-defined]
            last_exc = exc
            msg = str(exc)
            m = _re.search(r"retry in (\d+(?:\.\d+)?)s", msg)
            if not m:
                m = _re.search(r"try again in (\d+(?:\.\d+)?)s", msg)
            wait = float(m.group(1)) + 1.0 if m else (2.0 ** attempt) * 2.0
            # Honor the server's hint up to 90s (Gemini free-tier RPM
            # bursts can wedge for ~35s; we need a higher cap than the
            # original 30s for clean recovery).
            print(f"[llm] rate limited; sleeping {wait:.1f}s (attempt {attempt + 1}/4)")
            _time.sleep(min(wait, 90.0))
        except litellm.exceptions.ServiceUnavailableError as exc:  # type: ignore[attr-defined]
            # Gemini frequently returns 503 under load; retry with backoff.
            last_exc = exc
            wait = (2.0 ** attempt) * 1.5
            print(f"[llm] service unavailable (503); sleeping {wait:.1f}s (attempt {attempt + 1}/4)")
            _time.sleep(min(wait, 30.0))
        except Exception:
            raise
    else:
        # Exhausted retries without break.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("rate-limit retry loop exited without success")

    content = response["choices"][0]["message"]["content"]
    return ChatResult(content=content, raw=response, model=model)


def judge_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    **kwargs: Any,
) -> ChatResult:
    """Call the JUDGE-role model (citation verifier, consistency reviewer).

    Routes to JUDGE_MODEL from .env (default: Groq llama-3.3-70b-versatile).
    Per the research-backed D5/D13 design: judge-role calls need 70B-class
    factual reliability that 8B local models cannot provide.

    Falls back to LLM_MODEL if JUDGE_MODEL is unset (with a printed warning,
    since this means weaker judging).
    """
    _ensure_env()
    judge_model = os.environ.get("JUDGE_MODEL")
    if not judge_model:
        # Fallback: use the primary LLM. Print a warning so this is visible.
        judge_model = os.environ.get("LLM_MODEL", "ollama_chat/llama3.1:8b")
        print(
            f"[warn] JUDGE_MODEL unset; falling back to {judge_model}. "
            "Judge reliability will be lower (see D5/D13).",
        )
    return chat(
        messages,
        model=judge_model,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format=response_format,
        **kwargs,
    )
