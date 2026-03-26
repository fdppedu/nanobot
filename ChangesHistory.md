# Changes History

---

## Change #1 — 2026-03-26

**Topic:** Add local Claude CLI provider and Windows system-path safety guards

### New Files

- **`nanobot/providers/claude_cli_provider.py`**
  - Implements `ClaudeCliProvider(LLMProvider)` — routes all LLM inference through
    the locally installed `claude` CLI (`claude --print --output-format json`) instead
    of a remote API.
  - Converts nanobot message history + tool definitions into a structured plain-text
    prompt that instructs the model to respond in a strict JSON protocol:
    - `{"type":"text","content":"..."}` for plain replies
    - `{"type":"tool_calls","calls":[...]}` for tool invocations
  - Parses the CLI's JSON envelope (`result` field) and further parses the inner
    JSON into `LLMResponse` / `ToolCallRequest` objects consumed by the agent loop.
  - Embeds non-negotiable safety instructions in every system prompt: refuses to
    suggest or execute commands that delete/wipe Windows system directories or
    disable security software.
  - Supports `chat()` and `chat_stream()` (stream shim delivers one delta on finish).
  - `timeout` defaults to 300 s; `claude_path` defaults to `"claude"` (on PATH).

### Modified Files

- **`nanobot/providers/__init__.py`**
  - Added `ClaudeCliProvider` to `__all__` and `_LAZY_IMPORTS`.

- **`nanobot/providers/registry.py`**
  - Registered a new `ProviderSpec` for `"claude_cli"`:
    `is_local=True`, `is_direct=True`, `default_api_base="claude"`.

- **`nanobot/config/schema.py`**
  - Added `claude_cli: ProviderConfig` field to `ProvidersConfig`.
  - `api_base` in this config entry is reused to specify the binary path
    (defaults to `"claude"`).

- **`nanobot/cli/commands.py`**
  - `_make_provider()` now handles `provider_name == "claude_cli"` first (before
    API-key validation) and instantiates `ClaudeCliProvider`.

- **`nanobot/agent/tools/shell.py`**
  - Added 4 new deny-patterns to `ExecTool.__init__` for:
    - `format C:` / `format <drive>:`
    - `rmdir /s C:\` (drive root wipe)
    - Commands targeting `C:\Windows`, `C:\Program Files`, `C:\System32`
    - PowerShell `Remove-Item -Recurse C:\`
  - Added `_WIN_SYSTEM_DIRS` class attribute listing protected directories.
  - Added `_guard_system_paths(command)` classmethod that extracts every absolute
    path from the command and rejects it if it falls inside a protected directory
    combined with a destructive verb or write redirect.
  - `_guard_command()` now calls `_guard_system_paths()` after the URL check.

- **`nanobot/agent/tools/filesystem.py`**
  - Added `_WIN_PROTECTED_DIRS` — tuple of `Path` objects for Windows system dirs.
  - Added `_is_protected_system_path(path)` helper that resolves the path and
    checks membership against the protected list.
  - `WriteFileTool.execute()` and `EditFileTool.execute()` both call
    `_is_protected_system_path()` and return an error string before touching the
    file if the path is protected.

### How to Enable

Add to `~/.nanobot/config.json`:

```json
{
  "agents": {
    "defaults": {
      "provider": "claude_cli",
      "model": "claude-cli"
    }
  },
  "providers": {
    "claude_cli": {
      "api_base": "claude"
    }
  }
}
```

Set `api_base` to the full path of the `claude` binary if it is not on `PATH`.
