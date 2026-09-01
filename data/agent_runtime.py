"""Provider-neutral runner for model agents; Codex CLI is the first provider."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PROXY_KEYS = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def codex_environment() -> dict[str, str]:
    """Build an environment where only the Codex subprocess receives proxy vars."""
    inherited = dict(os.environ)
    fallback = {key: inherited[key] for key in PROXY_KEYS if inherited.get(key)}
    for key in PROXY_KEYS:
        inherited.pop(key, None)
    path = Path(
        os.environ.get("STOCK_PROXY_ENV_FILE") or Path.home() / ".proxy_env"
    ).expanduser()
    loaded: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if key not in PROXY_KEYS:
                continue
            values = shlex.split(raw_value.strip(), posix=True)
            if values:
                loaded[key] = values[0]
    inherited.update(loaded or fallback)
    return inherited


@dataclass(frozen=True)
class AgentRunResult:
    provider: str
    model: str
    command: tuple[str, ...]
    returncode: int
    final_message: str
    events: str
    stderr: str


class CodexCliProvider:
    """Run one ephemeral non-interactive Codex session with one local MCP."""

    name = "codex-cli"

    def __init__(
        self,
        *,
        executable: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 900,
        sandbox: str = "read-only",
        ephemeral: bool = True,
    ) -> None:
        requested = executable or os.environ.get("STOCK_AGENT_CODEX_BIN") or "codex"
        resolved = shutil.which(requested) if not Path(requested).is_absolute() else requested
        if not resolved and requested == "codex":
            candidates = [Path.home() / ".local/bin/codex"]
            candidates.extend(sorted(
                (Path.home() / ".nvm/versions/node").glob("*/bin/codex"), reverse=True,
            ))
            resolved = next((str(path) for path in candidates if path.exists()), None)
        if not resolved or not Path(resolved).exists():
            raise RuntimeError(f"Codex CLI is unavailable: {requested}")
        self.executable = str(Path(resolved).resolve())
        self.model = model or os.environ.get("STOCK_AGENT_MODEL") or "gpt-5.6-sol"
        self.timeout_seconds = timeout_seconds
        if sandbox not in {"read-only", "workspace-write"}:
            raise ValueError(f"unsupported Codex sandbox: {sandbox}")
        self.sandbox = sandbox
        self.ephemeral = bool(ephemeral)

    def build_command(
        self,
        *,
        workspace: Path,
        final_message_path: Path,
        mcp_command: str | None = None,
        mcp_args: Sequence[str] = (),
        output_schema_path: Path | None = None,
        resume_session_id: str = "",
        image_paths: Sequence[Path] = (),
    ) -> list[str]:
        # JSON string/array syntax is valid TOML and avoids shell interpolation.
        command = [
            self.executable,
            "exec",
        ]
        if resume_session_id:
            if mcp_command:
                raise ValueError("MCP overrides are not supported when resuming a Codex session")
            command.append("resume")
        command.extend([
            "--ignore-user-config",
            "--ignore-rules",
            # Scheduled stock decisions need exactly one project-local MCP.
            # Keep account-level Apps/plugins and interactive agent surfaces
            # out of the unattended process: besides reducing the tool attack
            # surface, this prevents an unrelated remote plugin outage from
            # blocking a trading decision.
            "--disable",
            "apps",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--disable",
            "multi_agent",
            "--model",
            self.model,
            "--json",
            "--output-last-message",
            str(final_message_path.resolve()),
            "--config",
            'approval_policy="never"',
        ])
        if not resume_session_id:
            if self.ephemeral:
                command.append("--ephemeral")
            command.extend([
                "--sandbox",
                self.sandbox,
                "--cd",
                str(workspace.resolve()),
            ])
        if output_schema_path is not None:
            command.extend(["--output-schema", str(output_schema_path.resolve())])
        for image_path in image_paths:
            command.extend(["--image", str(image_path.resolve())])
        if mcp_command:
            command.extend([
                "--config",
                f"mcp_servers.stock_agent.command={json.dumps(mcp_command)}",
                "--config",
                f"mcp_servers.stock_agent.args={json.dumps(list(mcp_args), ensure_ascii=False)}",
                "--config",
                "mcp_servers.stock_agent.startup_timeout_sec=30",
                "--config",
                "mcp_servers.stock_agent.tool_timeout_sec=360",
                "--config",
                'mcp_servers.stock_agent.default_tools_approval_mode="approve"',
            ])
            db_path = os.environ.get("STOCK_DB_PATH", "").strip()
            if db_path:
                command.extend([
                    "--config",
                    f"mcp_servers.stock_agent.env.STOCK_DB_PATH={json.dumps(db_path)}",
                ])
        if resume_session_id:
            command.extend([resume_session_id, "-"])
        else:
            command.append("-")
        return command

    def run(
        self,
        *,
        prompt: str,
        workspace: Path,
        run_dir: Path,
        mcp_command: str | None = None,
        mcp_args: Sequence[str] = (),
        output_schema_path: Path | None = None,
        resume_session_id: str = "",
        image_paths: Sequence[Path] = (),
    ) -> AgentRunResult:
        run_dir.mkdir(parents=True, exist_ok=True)
        final_path = run_dir / "final-message.txt"
        command = self.build_command(
            workspace=workspace, mcp_command=mcp_command, mcp_args=mcp_args,
            final_message_path=final_path, output_schema_path=output_schema_path,
            resume_session_id=resume_session_id,
            image_paths=image_paths,
        )
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                cwd=workspace,
                env=codex_environment(),
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            events = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            events = str(exc.stdout or "")
            stderr = f"Codex CLI timed out after {self.timeout_seconds}s\n{exc.stderr or ''}"
            returncode = 124
        (run_dir / "codex-events.jsonl").write_text(events, encoding="utf-8")
        (run_dir / "codex-stderr.log").write_text(stderr, encoding="utf-8")
        final = final_path.read_text(encoding="utf-8").strip() if final_path.exists() else ""
        return AgentRunResult(
            provider=self.name,
            model=self.model,
            command=tuple(command),
            returncode=returncode,
            final_message=final,
            events=events,
            stderr=stderr,
        )
