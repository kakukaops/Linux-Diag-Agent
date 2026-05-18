"""M1 claude_code provider — subprocess adapter (阶段 A, ADR-004).

Wraps `claude -p --output-format stream-json --verbose --bare` as a subprocess.
Translates between OpenAI Chat schema and Anthropic Messages format.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from threading import Semaphore
from typing import Any

from llm.provider.base import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    LLMProviderError,
    Usage,
)
from llm.provider.translation.openai_to_anthropic import (
    to_anthropic_messages,
    to_anthropic_tools,
)
from llm.provider.translation.stream_parser import (
    build_response,
    parse_stream_json,
    parse_tool_calls_from_message,
)


class ClaudeCodeProvider:
    """Calls `claude -p` subprocess; implements LLMProvider protocol."""

    provider_name = "claude_code"

    def __init__(self, role: str = "chat") -> None:
        from configs.config import get_config
        cfg = get_config()
        role_cfg = cfg.llm.chat if role == "chat" else cfg.llm.navigator
        self._model = role_cfg.model
        self._timeout = role_cfg.timeout_seconds
        self._semaphore = Semaphore(role_cfg.concurrency)
        self._adapter = role_cfg.adapter  # "subprocess" | "agent_sdk"

    # ── Public interface ─────────────────────────────────────────────────

    def chat(self, req: ChatRequest) -> ChatResponse:
        chunks = list(self.chat_stream(req))
        resp = build_response(chunks, provider=self.provider_name)
        # Patch tool_calls from the raw stream if any
        return resp

    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]:
        if self._adapter == "agent_sdk":
            yield from self._stream_agent_sdk(req)
        else:
            yield from self._stream_subprocess(req)

    def health_check(self) -> bool:
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    # ── Subprocess adapter ───────────────────────────────────────────────

    def _stream_subprocess(self, req: ChatRequest) -> Iterator[ChatChunk]:
        """Run claude CLI subprocess and yield streaming chunks."""
        cmd = self._build_command(req)
        prompt = self._build_prompt_text(req)

        with self._semaphore:
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = proc.communicate(
                    input=prompt,
                    timeout=self._timeout,
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                raise LLMProviderError(
                    "claude subprocess timed out",
                    code="transient",
                    provider=self.provider_name,
                )
            except FileNotFoundError:
                raise LLMProviderError(
                    "claude CLI not found — install Claude Code and log in",
                    code="auth",
                    provider=self.provider_name,
                )

        if proc.returncode != 0:
            self._raise_from_stderr(stderr)

        yield from parse_stream_json(iter(stdout.splitlines()))

    def _build_command(self, req: ChatRequest) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--bare",
        ]
        if req.model:
            cmd += ["--model", req.model]
        elif self._model:
            cmd += ["--model", self._model]
        if req.tools:
            # Tool names must be pre-registered via MCP; here we just allow them
            tool_names = ",".join(t.function.name for t in req.tools)
            cmd += ["--allowedTools", tool_names]
        return cmd

    def _build_prompt_text(self, req: ChatRequest) -> str:
        """Serialize messages to a prompt string for -p stdin injection.

        For the subprocess adapter we pass a structured JSON envelope via stdin
        so that the claude CLI can reconstruct the conversation.
        """
        system, messages = to_anthropic_messages(req.messages)
        envelope: dict[str, Any] = {"messages": messages}
        if system:
            envelope["system"] = system
        if req.tools:
            envelope["tools"] = to_anthropic_tools(req.tools)
        if req.response_format:
            envelope["response_format"] = req.response_format
        return json.dumps(envelope)

    def _raise_from_stderr(self, stderr: str) -> None:
        stderr_lower = stderr.lower()
        if "rate limit" in stderr_lower or "too many requests" in stderr_lower:
            raise LLMProviderError(
                f"Rate limited by Claude: {stderr[:200]}",
                code="rate_limited",
                retry_after_seconds=300.0,
                provider=self.provider_name,
            )
        if "unauthorized" in stderr_lower or "not logged in" in stderr_lower:
            raise LLMProviderError(
                "Claude Code not authenticated. Run `claude login`.",
                code="auth",
                provider=self.provider_name,
            )
        if "context" in stderr_lower and "length" in stderr_lower:
            raise LLMProviderError(
                "Context window exceeded",
                code="context_exceeded",
                provider=self.provider_name,
            )
        raise LLMProviderError(
            f"claude subprocess error: {stderr[:400]}",
            code="unknown",
            raw_error={"stderr": stderr},
            provider=self.provider_name,
        )

    # ── Agent SDK adapter (stub — enabled 2026-06-15+) ───────────────────

    def _stream_agent_sdk(self, req: ChatRequest) -> Iterator[ChatChunk]:
        """Placeholder for claude-agent-sdk adapter (阶段 B).

        When the SDK is available, replace this body with SDK calls.
        The public interface (chat / chat_stream) remains identical.
        """
        raise LLMProviderError(
            "agent_sdk adapter not yet implemented. Set adapter: subprocess in config.",
            code="unknown",
            provider=self.provider_name,
        )
