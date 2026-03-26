"""Claude CLI provider — runs the local `claude` CLI as an LLM backend.

Instead of calling a remote API, this provider spawns the `claude` command-line
tool (Claude Code) as a subprocess.  It converts the nanobot messages + tool
definitions into a structured prompt, instructs claude to respond in a strict JSON
format, and parses that JSON back into an LLMResponse.

Configuration (config.json):
    providers:
      claude_cli:
        api_base: "claude"   # optional: path to the claude binary (default "claude")
    agents:
      defaults:
        provider: "claude_cli"
        model: "claude-cli"
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import string
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


_ALNUM = string.ascii_letters + string.digits


def _short_id() -> str:
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


# ---------------------------------------------------------------------------
# System prompts injected into every claude CLI call
# ---------------------------------------------------------------------------

# Used when tools are provided — instructs claude on JSON tool-call format.
_TOOL_SYSTEM_PROMPT = """\
You are an AI assistant with access to tools.

RESPONSE FORMAT — respond with ONLY valid JSON, no markdown fences, no extra text.

Option A — plain text answer (no tool needed):
{"type":"text","content":"<your response here>"}

Option B — call one or more tools:
{"type":"tool_calls","calls":[{"id":"<unique_id>","name":"<tool_name>","arguments":<args_object>}]}

Each call "id" must be a short unique alphanumeric string (e.g. "a1b2c3d4e").

SAFETY CONSTRAINTS (absolute, non-negotiable):
- NEVER emit commands or tool calls that delete, wipe, or format the C:\\ drive root,
  C:\\Windows, C:\\Program Files, C:\\System32, or any Windows system directory.
- NEVER suggest disabling Windows Defender, firewalls, or any security software.
- NEVER suggest operations that could corrupt the operating system or make it unbootable.
- NEVER delete files outside the designated workspace without explicit user confirmation.
- If the user asks for any of the above, respond with type "text" explaining the refusal.
"""

# Used when no tools are available — simpler prompt.
_NOTOOL_SYSTEM_PROMPT = """\
You are a helpful AI assistant.

Respond with ONLY valid JSON (no markdown fences):
{"type":"text","content":"<your answer here>"}

SAFETY CONSTRAINTS:
- NEVER suggest commands that delete or wipe C:\\, C:\\Windows, C:\\Program Files,
  C:\\System32, or any Windows system directory.
- NEVER suggest disabling Windows security features.
- If asked for such operations, explain the refusal inside the "content" field.
"""


class ClaudeCliProvider(LLMProvider):
    """LLM provider that delegates inference to the local ``claude`` CLI.

    The provider works by:
    1. Serialising messages + tool definitions into a structured text prompt.
    2. Piping the prompt to ``claude --print --output-format json`` via stdin.
    3. Extracting the result text from the CLI's JSON envelope.
    4. Parsing the result text as our own JSON tool-call protocol.

    Because the claude CLI is itself an LLM, it understands the protocol
    described in the system prompt and responds accordingly.
    """

    # Sentinel: model names that mean "let the CLI pick its own default".
    # When the resolved model matches one of these, --model is NOT passed to
    # the subprocess, so the user's claude CLI configuration takes effect.
    _CLI_DEFAULT_SENTINELS: frozenset[str] = frozenset({"claude-cli", "", "auto"})

    def __init__(
        self,
        claude_path: str = "claude",
        default_model: str = "",
        timeout: int = 300,
    ):
        """
        Args:
            claude_path: Path (or name) of the claude binary.  Defaults to
                "claude" (must be on PATH or specified as an absolute path).
            default_model: The Claude model name to pass via ``--model`` to
                the CLI (e.g. ``"claude-opus-4-5"``).  Leave empty / set to
                ``"claude-cli"`` to let the CLI use its own configured default.
            timeout: Seconds to wait for a single claude CLI call before
                raising TimeoutError.
        """
        super().__init__(api_key=None, api_base=None)
        self.claude_path = claude_path
        self.default_model = default_model
        self.timeout = timeout

    # ------------------------------------------------------------------
    # LLMProvider interface
    # ------------------------------------------------------------------

    def get_default_model(self) -> str:
        return self.default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send a chat completion request via the local claude CLI."""
        resolved_model = model or self.default_model
        prompt = self._build_prompt(messages, tools)
        try:
            raw = await self._run_claude(prompt, resolved_model)
            return self._parse_response(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("ClaudeCliProvider error: {}", exc)
            return LLMResponse(
                content=f"Error calling claude CLI: {exc}",
                finish_reason="error",
            )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Streaming shim — delivers the full response as one delta after completion."""
        resolved_model = model or self.default_model
        response = await self.chat(
            messages=messages,
            tools=tools,
            model=resolved_model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        if on_content_delta and response.content:
            await on_content_delta(response.content)
        return response

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> str:
        """Convert nanobot messages + tool definitions into a plain-text prompt."""
        parts: list[str] = []

        if tools:
            parts.append(_TOOL_SYSTEM_PROMPT)
            parts.append(
                "AVAILABLE TOOLS (JSON schema):\n"
                + json.dumps(tools, ensure_ascii=False, indent=2)
            )
        else:
            parts.append(_NOTOOL_SYSTEM_PROMPT)

        parts.append("--- CONVERSATION HISTORY ---")

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")

            if role == "system":
                text = self._extract_text(content)
                if text:
                    parts.append(f"[SYSTEM]\n{text}")

            elif role == "user":
                text = self._extract_text(content)
                if text:
                    parts.append(f"[USER]\n{text}")

            elif role == "assistant":
                text = self._extract_text(content) or ""
                if tool_calls:
                    tc_lines = json.dumps(
                        [
                            {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"].get("arguments", "{}"),
                            }
                            for tc in tool_calls
                        ],
                        ensure_ascii=False,
                    )
                    header = f"[ASSISTANT]{chr(10)}{text}" if text else "[ASSISTANT]"
                    parts.append(f"{header}\n[TOOL CALLS]\n{tc_lines}")
                elif text:
                    parts.append(f"[ASSISTANT]\n{text}")

            elif role == "tool":
                tool_name = msg.get("name", "unknown")
                text = self._extract_text(content) or "(no result)"
                parts.append(f"[TOOL RESULT: {tool_name}]\n{text}")

        parts.append("--- END OF CONVERSATION ---")
        parts.append(
            "Now respond with ONLY valid JSON (no markdown, no extra text):"
        )
        return "\n\n".join(parts)

    @staticmethod
    def _extract_text(content: Any) -> str:
        """Flatten any message content shape to a plain string."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    btype = block.get("type", "")
                    if btype in ("text", "input_text", "output_text"):
                        texts.append(block.get("text", ""))
                    elif btype == "tool_result":
                        inner = block.get("content", "")
                        texts.append(
                            inner if isinstance(inner, str) else str(inner)
                        )
                elif isinstance(block, str):
                    texts.append(block)
            return " ".join(t for t in texts if t)
        return str(content)

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    async def _run_claude(self, prompt: str, model: str = "") -> str:
        """Pipe *prompt* to the claude CLI and return the result text.

        Args:
            prompt: The full conversation prompt to send via stdin.
            model: Claude model name (e.g. ``"claude-opus-4-5"``).  When empty
                or a sentinel value like ``"claude-cli"``, the ``--model`` flag
                is omitted and the CLI uses its own default.
        """
        cmd = [
            self.claude_path,
            "--dangerously-skip-permissions",  # required for non-interactive use
            "--print",
            "--output-format", "json",
        ]

        # Pass --model only when caller specifies a real model name.
        if model and model not in self._CLI_DEFAULT_SENTINELS:
            cmd += ["--model", model]

        logger.debug("ClaudeCliProvider: running {}", " ".join(cmd))

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        prompt_bytes = prompt.encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=prompt_bytes),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except Exception:
                pass
            raise TimeoutError(
                f"claude CLI timed out after {self.timeout}s"
            )

        raw_out = stdout.decode("utf-8", errors="replace").strip()
        raw_err = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            hint = raw_err[:400] or raw_out[:400]
            raise RuntimeError(
                f"claude CLI exited with code {process.returncode}: {hint}"
            )

        logger.debug(
            "ClaudeCliProvider raw output ({} chars): {}…",
            len(raw_out),
            raw_out[:200],
        )
        return self._unwrap_cli_output(raw_out)

    @staticmethod
    def _unwrap_cli_output(raw: str) -> str:
        """Extract the LLM result text from the claude CLI JSON envelope.

        Handles both ``--output-format json`` (single object) and
        ``--output-format stream-json`` (JSONL) outputs.
        """
        if not raw:
            return ""

        # Single-object JSON (--output-format json)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                if data.get("is_error"):
                    raise RuntimeError(
                        f"claude CLI reported error: {data.get('result', 'unknown')}"
                    )
                return data.get("result", raw)
        except json.JSONDecodeError:
            pass

        # JSONL (--output-format stream-json) — pick last "result" event
        result_text: str | None = None
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "result":
                    if event.get("is_error"):
                        raise RuntimeError(
                            f"claude CLI reported error: {event.get('result', 'unknown')}"
                        )
                    result_text = event.get("result", "")
            except json.JSONDecodeError:
                continue

        return result_text if result_text is not None else raw

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(self, text: str) -> LLMResponse:
        """Parse the JSON-formatted response produced by the claude CLI.

        The claude CLI is instructed to reply with one of two JSON shapes:
            {"type":"text","content":"..."}
            {"type":"tool_calls","calls":[{"id":"...","name":"...","arguments":{...}}]}
        If the output is not valid JSON (e.g. the model wandered off format),
        it is returned as a plain text response.
        """
        if not text:
            return LLMResponse(content="(no response)", finish_reason="stop")

        clean = text.strip()

        # Strip markdown code fences that claude sometimes adds despite instructions
        if clean.startswith("```"):
            lines = clean.splitlines()
            inner_lines = [l for l in lines[1:] if l.strip() != "```"]
            clean = "\n".join(inner_lines).strip()

        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # Not JSON — treat as plain text (graceful degradation)
            logger.debug(
                "ClaudeCliProvider: response is not JSON, treating as plain text"
            )
            return LLMResponse(content=text, finish_reason="stop")

        if not isinstance(data, dict):
            return LLMResponse(content=text, finish_reason="stop")

        resp_type = data.get("type", "text")

        if resp_type == "tool_calls":
            raw_calls = data.get("calls", [])
            tool_calls: list[ToolCallRequest] = []
            for call in raw_calls:
                name = call.get("name", "")
                args = call.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"input": args}
                tool_calls.append(
                    ToolCallRequest(
                        id=call.get("id") or _short_id(),
                        name=name,
                        arguments=args if isinstance(args, dict) else {"input": args},
                    )
                )
            return LLMResponse(
                content=data.get("content"),
                tool_calls=tool_calls,
                finish_reason="tool_calls",
            )

        # type == "text" or unrecognised — return content field
        content = data.get("content", text)
        return LLMResponse(content=content, finish_reason="stop")
