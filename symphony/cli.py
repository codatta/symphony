from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import shlex
import shutil
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from symphony.agents.claude_code import ClaudeCodeRunner
from symphony.agents.codex import CodexRunner
from symphony.auth import (
    MissingLinearTokenError,
    TokenStore,
    default_credentials_path,
    load_local_github_token,
    save_local_github_token,
    save_local_linear_token,
)
from symphony.config import ConfigError, WorkflowConfig
from symphony.http_server import StatusAPI
from symphony.onboarding import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_PRESET,
    DEFAULT_RUNNER,
    DEFAULT_TERMINAL_STATES,
    DEFAULT_WORKFLOW_PATH,
    PRESETS,
    InitConfig,
    OnboardingError,
    default_workspace_root,
    generate_workflow,
    parse_state_list,
    write_workflow,
)
from symphony.runtime import RuntimeTickResult, SymphonyRuntime
from symphony.tracker.linear import LinearClient
from symphony.workflow import WorkflowError, load_workflow
from symphony.workflow import EffectiveWorkflow, WorkflowReloader
from symphony.workspace import WorkspaceManager


DEFAULT_PORT = 7337
LOGGER = logging.getLogger(__name__)
TickHook = Callable[[], Any]
StatusServer = Callable[[StatusAPI, int], Awaitable[None]]


@dataclass(frozen=True)
class StartupContext:
    workflow_path: Path
    logs_root: Path
    port: int
    workflow: object
    config: WorkflowConfig


class StartupError(RuntimeError):
    """Raised when Symphony cannot start with the requested configuration."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony",
        description="Run the Symphony CLI MVP orchestrator for a repository WORKFLOW.md.",
        epilog="Common commands: symphony init, symphony doctor WORKFLOW.md, symphony run WORKFLOW.md",
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to repository WORKFLOW.md. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--port",
        type=_port_value,
        default=DEFAULT_PORT,
        help=f"Loopback status API port. Defaults to {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--logs-root",
        default="./log",
        help="Directory for Symphony runtime logs. Defaults to ./log.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate workflow/configuration and exit without starting the daemon.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one poll/dispatch tick and exit. Useful for smoke tests.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Console log level. Defaults to INFO.",
    )
    return parser


def build_run_parser() -> argparse.ArgumentParser:
    parser = build_parser()
    parser.prog = "symphony run"
    parser.description = "Run the Symphony orchestrator for a repository WORKFLOW.md."
    return parser


def build_init_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony init",
        description="Generate a starter WORKFLOW.md and optionally store Linear CLI credentials.",
    )
    parser.add_argument(
        "--workflow-path",
        default=DEFAULT_WORKFLOW_PATH,
        help="Where to write the generated workflow. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--preset",
        default=DEFAULT_PRESET,
        choices=tuple(PRESETS),
        help=f"Workflow preset. Defaults to {DEFAULT_PRESET}.",
    )
    parser.add_argument("--project-slug", help="Linear project slugId to poll.")
    parser.add_argument(
        "--active-states",
        help="Comma-separated Linear state names that should dispatch work.",
    )
    parser.add_argument(
        "--terminal-states",
        help="Comma-separated Linear terminal state names.",
    )
    parser.add_argument("--workspace-root", help="Root directory for issue workspaces.")
    parser.add_argument(
        "--codex-command",
        default="codex app-server",
        help="Command used to launch Codex. Defaults to 'codex app-server'.",
    )
    parser.add_argument(
        "--linear-api-key",
        help="Store this Linear API key in the local Symphony credential file.",
    )
    parser.add_argument(
        "--credentials-path",
        help="Override the local credentials file path. Mostly useful for tests.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing WORKFLOW.md.",
    )
    parser.add_argument(
        "--runner",
        default=DEFAULT_RUNNER,
        choices=("claude_code", "codex"),
        help=f"Agent runner to use. Defaults to {DEFAULT_RUNNER}.",
    )
    parser.add_argument(
        "--github-token",
        help="GitHub personal access token for PR automation (Contents + Pull requests R/W).",
    )
    parser.add_argument(
        "--github-org",
        help="GitHub organisation or user name that owns the target repositories.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Run non-interactively using defaults. Requires --project-slug.",
    )
    return parser


def build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="symphony doctor",
        description="Check workflow, Linear auth, Codex command, and workspace readiness.",
    )
    parser.add_argument(
        "workflow_path",
        nargs="?",
        default=DEFAULT_WORKFLOW_PATH,
        help="Path to repository WORKFLOW.md. Defaults to ./WORKFLOW.md.",
    )
    parser.add_argument(
        "--port",
        type=_port_value,
        default=DEFAULT_PORT,
        help=f"Loopback status API port. Defaults to {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--logs-root",
        default="./log",
        help="Directory for Symphony runtime logs. Defaults to ./log.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Console log level. Defaults to WARNING.",
    )
    return parser


def load_startup_context(
    workflow_path: str | Path,
    *,
    logs_root: str | Path,
    port: int,
    environ: Mapping[str, str] | None = None,
) -> StartupContext:
    workflow_file = Path(workflow_path).expanduser().resolve()
    logs_path = _resolve_logs_root(logs_root, workflow_file)

    try:
        definition = load_workflow(workflow_file)
        config = definition.typed_config(workflow_path=workflow_file, environ=environ)
        validate_dispatch_config(config, environ=environ)
    except (WorkflowError, ConfigError, MissingLinearTokenError) as exc:
        raise StartupError(str(exc)) from exc

    return StartupContext(
        workflow_path=workflow_file,
        logs_root=logs_path,
        port=port,
        workflow=definition,
        config=config,
    )


def validate_dispatch_config(config: WorkflowConfig, *, environ: Mapping[str, str] | None = None) -> None:
    if config.tracker.kind != "linear":
        raise ConfigError("unsupported_tracker_kind")
    if not config.tracker.project_slug:
        raise ConfigError("missing_tracker_project_slug")
    TokenStore(config.tracker, environ=environ).resolve_linear_token()
    if config.agent.runner == "claude_code":
        if not config.claude_code.command.strip():
            raise ConfigError("claude_code_command_required")
    else:
        if not config.codex.command.strip():
            raise ConfigError("codex_command_required")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_runtime(context: StartupContext) -> SymphonyRuntime:
    linear_client = create_tracker(context.config)
    workspace_manager = create_workspace_manager(context.config)
    runner = create_runner(context.config, linear_client)
    return SymphonyRuntime(
        config=context.config,
        workflow=context.workflow,
        tracker=linear_client,
        workspace_manager=workspace_manager,
        runner=runner,
    )


def create_status_api(runtime: SymphonyRuntime) -> StatusAPI:
    return StatusAPI(runtime.snapshot, refresh_callback=runtime.run_tick)


def create_tracker(config: WorkflowConfig) -> LinearClient:
    return LinearClient(config.tracker)


def create_workspace_manager(config: WorkflowConfig) -> WorkspaceManager:
    return WorkspaceManager(config.workspace, config.hooks)


def create_runner(config: WorkflowConfig, linear_client: LinearClient) -> CodexRunner | ClaudeCodeRunner:
    if config.agent.runner == "claude_code":
        linear_api_key = _resolve_linear_token(config)
        github_token = _resolve_github_token()
        return ClaudeCodeRunner(
            config.claude_code.command,
            model=config.claude_code.model,
            permission_mode=config.claude_code.permission_mode,
            turn_timeout_ms=config.claude_code.turn_timeout_ms,
            linear_api_key=linear_api_key,
            github_token=github_token,
        )
    return CodexRunner(
        config.codex.command,
        approval_policy=config.codex.approval_policy or "on-request",
        thread_sandbox=config.codex.thread_sandbox or "workspace-write",
        turn_sandbox_policy=_codex_turn_sandbox_policy(config),
        read_timeout_ms=config.codex.read_timeout_ms,
        turn_timeout_ms=config.codex.turn_timeout_ms,
        linear_client=linear_client,
    )


async def run_once(runtime: SymphonyRuntime) -> RuntimeTickResult:
    return await runtime.run_tick()


async def run_poll_loop(runtime: SymphonyRuntime, *, before_tick: TickHook | None = None) -> None:
    await runtime.record_startup_issues()
    while True:
        if before_tick is not None:
            result = before_tick()
            if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
                await result
        result = await runtime.run_tick()
        LOGGER.info(
            "Tick completed: fetched=%s dispatched=%s completed=%s failed=%s released=%s",
            result.fetched,
            ",".join(result.dispatched) or "-",
            ",".join(result.completed) or "-",
            ",".join(result.failed) or "-",
            ",".join(result.released) or "-",
        )
        await asyncio.sleep(runtime.state.poll_interval_ms / 1000)


async def serve_status_api(status_api: StatusAPI, port: int) -> None:
    loop = asyncio.get_running_loop()
    server = create_status_http_server(status_api, port, loop=loop)
    LOGGER.info("Status API listening on http://127.0.0.1:%s", port)
    serve_task = asyncio.create_task(asyncio.to_thread(server.serve_forever, 0.25))
    try:
        await serve_task
    finally:
        server.shutdown()
        server.server_close()
        await asyncio.gather(serve_task, return_exceptions=True)


def create_status_http_server(
    status_api: StatusAPI,
    port: int,
    *,
    loop: asyncio.AbstractEventLoop,
    host: str = "127.0.0.1",
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("POST")

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("PUT")

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API.
            self._send_status_response("DELETE")

        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.debug("Status API request: " + fmt, *args)

        def _send_status_response(self, method: str) -> None:
            body = self.rfile.read(_content_length(self.headers.get("content-length")))
            if method == "POST" and self.path.split("?", 1)[0] == "/api/v1/refresh":
                future = asyncio.run_coroutine_threadsafe(
                    status_api.async_handle_request(method, self.path, body),
                    loop,
                )
                response = future.result()
            else:
                response = status_api.handle_request(method, self.path, body)

            payload = response.json_bytes()
            self.send_response(response.status_code)
            for header, value in response.headers.items():
                self.send_header(header, value)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer((host, port), Handler)


async def run_daemon(
    runtime: SymphonyRuntime,
    context: StartupContext,
    *,
    workflow_reloader: "RuntimeWorkflowReloader | None" = None,
    status_server: StatusServer = serve_status_api,
) -> None:
    status_api = create_status_api(runtime)
    status_task = asyncio.create_task(status_server(status_api, context.port))
    poll_task = asyncio.create_task(
        run_poll_loop(
            runtime,
            before_tick=workflow_reloader.reload_if_changed if workflow_reloader is not None else None,
        )
    )
    tasks = {status_task, poll_task}

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
        for task in pending:
            task.cancel()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class RuntimeWorkflowReloader:
    runtime: SymphonyRuntime
    workflow_path: Path
    environ: Mapping[str, str] | None = None
    reloader: WorkflowReloader | None = None
    last_observed_mtime_ns: int | None = None

    @classmethod
    def from_context(
        cls,
        runtime: SymphonyRuntime,
        context: StartupContext,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeWorkflowReloader":
        reloader = WorkflowReloader.for_path(context.workflow_path)
        effective = EffectiveWorkflow(definition=context.workflow, config=context.config)
        reloader.last_good = context.workflow
        reloader.last_good_effective = effective
        return cls(
            runtime=runtime,
            workflow_path=context.workflow_path,
            environ=environ,
            reloader=reloader,
            last_observed_mtime_ns=_workflow_mtime_ns(context.workflow_path),
        )

    def reload_if_changed(self) -> bool:
        mtime_ns = _workflow_mtime_ns(self.workflow_path)
        if mtime_ns == self.last_observed_mtime_ns:
            return False
        self.last_observed_mtime_ns = mtime_ns
        return self.reload_now()

    def reload_now(self) -> bool:
        active_reloader = self.reloader or WorkflowReloader.for_path(self.workflow_path)
        try:
            definition = load_workflow(self.workflow_path)
            config = definition.typed_config(workflow_path=self.workflow_path, environ=self.environ)
            validate_dispatch_config(config, environ=self.environ)
        except (WorkflowError, ConfigError, MissingLinearTokenError) as exc:
            active_reloader.last_error = exc
            LOGGER.error("Rejected WORKFLOW.md reload for %s: %s", self.workflow_path, exc)
            return False

        effective = EffectiveWorkflow(definition=definition, config=config)
        active_reloader.last_good = definition
        active_reloader.last_good_effective = effective
        active_reloader.last_error = None
        self.reloader = active_reloader
        apply_runtime_workflow(self.runtime, effective)
        LOGGER.info("Reloaded WORKFLOW.md from %s", self.workflow_path)
        return True


def apply_runtime_workflow(runtime: SymphonyRuntime, effective: EffectiveWorkflow) -> None:
    linear_client = create_tracker(effective.config)
    runtime.config = effective.config
    runtime.workflow = effective.definition
    runtime.prompt_template = effective.definition.prompt_template
    runtime.tracker = linear_client
    runtime.workspace_manager = create_workspace_manager(effective.config)
    runtime.runner = create_runner(effective.config, linear_client)
    runtime.state.apply_config(effective.config)
    runtime._notify_state_change()


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    if raw_args:
        command = raw_args[0]
        if command == "init":
            return init_main(raw_args[1:])
        if command == "doctor":
            return doctor_main(raw_args[1:])
        if command == "run":
            return run_main(raw_args[1:])

    parser = build_parser()
    args = parser.parse_args(raw_args)
    return run_with_args(args, parser)


def run_main(argv: Sequence[str] | None = None) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    return run_with_args(args, parser)


def run_with_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    configure_logging(args.log_level)

    try:
        context = load_startup_context(
            args.workflow_path,
            logs_root=args.logs_root,
            port=args.port,
        )
    except StartupError as exc:
        parser.exit(2, f"symphony: {exc}\n")

    if args.check:
        print(f"Workflow OK: {context.workflow_path}")
        print(f"Logs root: {context.logs_root}")
        print(f"Status API port: {context.port}")
        return 0

    runtime = create_runtime(context)
    if args.once:
        result = asyncio.run(run_once(runtime))
        print(
            "Tick OK: "
            f"fetched={result.fetched} "
            f"dispatched={len(result.dispatched)} "
            f"completed={len(result.completed)} "
            f"failed={len(result.failed)} "
            f"released={len(result.released)}"
        )
        return 0

    workflow_reloader = RuntimeWorkflowReloader.from_context(runtime, context)
    asyncio.run(run_daemon(runtime, context, workflow_reloader=workflow_reloader))
    return 0


def init_main(argv: Sequence[str] | None = None) -> int:
    parser = build_init_parser()
    args = parser.parse_args(argv)

    try:
        if args.yes and not args.project_slug:
            raise OnboardingError("missing_project_slug")

        # --- Step 1: Linear project slug ---
        if not args.project_slug and not args.yes:
            print("Find your project slugId at: Linear → Settings → API → Project slug")
            print("  (also visible in the project URL: linear.app/TEAM/project/NAME-SLUG)")
        project_slug = args.project_slug or _prompt("Linear project slugId")
        if not project_slug:
            raise OnboardingError("missing_project_slug")

        active_states = parse_state_list(
            args.active_states
            or (None if args.yes else _prompt_default("Active Linear states", ", ".join(DEFAULT_ACTIVE_STATES))),
            DEFAULT_ACTIVE_STATES,
        )
        terminal_states = parse_state_list(
            args.terminal_states
            or (None if args.yes else _prompt_default("Terminal Linear states", ", ".join(DEFAULT_TERMINAL_STATES))),
            DEFAULT_TERMINAL_STATES,
        )
        workspace_root = args.workspace_root or (
            default_workspace_root(project_slug)
            if args.yes
            else _prompt_default("Workspace root", default_workspace_root(project_slug))
        )

        # --- Step 2: GitHub org (claude_code runner only) ---
        runner = args.runner
        github_org = args.github_org or ""
        if runner == "claude_code" and not github_org and not args.yes:
            print("\nGitHub organisation or user name that owns your repositories")
            print("  (e.g. 'acme-corp' from github.com/acme-corp/...)")
            github_org = _prompt("GitHub org/user (blank to fill in later)").strip()

        workflow = generate_workflow(
            InitConfig(
                project_slug=project_slug,
                preset=args.preset,
                active_states=active_states,
                terminal_states=terminal_states,
                workspace_root=workspace_root,
                codex_command=args.codex_command,
                runner=runner,
                github_org=github_org,
            )
        )
        workflow_path = write_workflow(args.workflow_path, workflow, overwrite=args.overwrite)
    except OnboardingError as exc:
        parser.exit(2, f"symphony init: {exc}\n")

    # --- Step 3: Linear API key ---
    linear_token = args.linear_api_key
    if linear_token is None and not args.yes:
        print("\nLinear API key (starts with lin_api_...)")
        print("  Create at: linear.app/settings/api → Personal API keys")
        linear_token = getpass.getpass("Linear API key (blank to skip): ").strip()
    if linear_token:
        credentials_path = save_local_linear_token(linear_token, path=args.credentials_path)
        print(f"Stored Linear credentials: {credentials_path}")
    else:
        print("Linear credentials not stored. Set LINEAR_API_KEY or re-run with --linear-api-key.")
        print(f"Default credentials path: {default_credentials_path()}")

    # --- Step 4: GitHub token (optional, for PR automation) ---
    github_token = args.github_token
    if github_token is None and runner == "claude_code" and not args.yes:
        print("\nGitHub personal access token for PR automation (optional)")
        print("  Required permissions: Contents (Read/Write), Pull requests (Read/Write)")
        print("  Create at: github.com/settings/tokens → Fine-grained tokens")
        github_token = getpass.getpass("GitHub token (blank to skip): ").strip()
    if github_token:
        credentials_path = save_local_github_token(github_token, path=args.credentials_path)
        print(f"Stored GitHub credentials: {credentials_path}")
        result = _validate_github_token(github_token)
        if result:
            print(f"  Connected as: {result}")
        else:
            print("  Warning: could not verify token — check permissions and try again.")
    elif runner == "claude_code":
        print("GitHub token not stored. Set GITHUB_TOKEN or re-run with --github-token.")

    print(f"\nWrote workflow: {workflow_path}")
    print(f"Next: symphony doctor {workflow_path}")
    return 0


def doctor_main(argv: Sequence[str] | None = None) -> int:
    parser = build_doctor_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    checks = doctor_checks(args.workflow_path, logs_root=args.logs_root, port=args.port)
    for ok, label, detail in checks:
        marker = "ok" if ok else "fail"
        print(f"[{marker}] {label}: {detail}")
    return 0 if all(ok for ok, _, _ in checks) else 2


def doctor_checks(
    workflow_path: str | Path,
    *,
    logs_root: str | Path,
    port: int,
    environ: Mapping[str, str] | None = None,
) -> list[tuple[bool, str, str]]:
    checks: list[tuple[bool, str, str]] = []
    try:
        context = load_startup_context(workflow_path, logs_root=logs_root, port=port, environ=environ)
    except StartupError as exc:
        checks.append((False, "workflow", str(exc)))
        return checks

    checks.append((True, "workflow", str(context.workflow_path)))
    checks.append((True, "linear auth", "token resolved"))

    if context.config.agent.runner == "claude_code":
        command_ok, command_check = _check_command(context.config.claude_code.command)
        checks.append((command_ok, "claude command", command_check))
    else:
        command_ok, command_check = _check_command(context.config.codex.command)
        checks.append((command_ok, "codex command", command_check))

    workspace_ok, workspace_check = _check_workspace_root(context.config.workspace.root)
    checks.append((workspace_ok, "workspace root", workspace_check))

    logs_root = context.logs_root
    checks.append((True, "logs root", str(logs_root)))
    checks.append((True, "status api", f"http://127.0.0.1:{context.port}"))
    return checks


def _resolve_logs_root(logs_root: str | Path, workflow_file: Path) -> Path:
    path = Path(logs_root).expanduser()
    if not path.is_absolute():
        path = workflow_file.parent / path
    return path.resolve()


def _port_value(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port_must_be_integer") from exc
    if port <= 0 or port > 65_535:
        raise argparse.ArgumentTypeError("port_must_be_1_to_65535")
    return port


def _resolve_linear_token(config: WorkflowConfig) -> str | None:
    try:
        return TokenStore(config.tracker).resolve_linear_token()
    except Exception:
        return None


def _resolve_github_token(credentials_path: Path | None = None) -> str | None:
    import os
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    return load_local_github_token(path=credentials_path)


def _validate_github_token(token: str) -> str | None:
    """Return the authenticated GitHub username, or None if the token is invalid."""
    import json as _json
    import urllib.request as _req
    try:
        request = _req.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with _req.urlopen(request, timeout=10) as resp:
            data = _json.loads(resp.read())
            return data.get("login") or "authenticated"
    except Exception:
        return None


def _codex_turn_sandbox_policy(config: WorkflowConfig) -> dict[str, object] | None:
    if config.codex.turn_sandbox_policy is None:
        return None
    return {"type": config.codex.turn_sandbox_policy}


def _workflow_mtime_ns(workflow_path: Path) -> int | None:
    try:
        return workflow_path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _prompt(label: str) -> str:
    return input(f"{label}: ").strip()


def _prompt_default(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _check_command(command: str) -> tuple[bool, str]:
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return False, f"invalid command: {exc}"
    if not parts:
        return False, "missing command"
    executable = shutil.which(parts[0])
    if executable is None:
        return False, f"missing executable: {parts[0]}"
    return True, executable


def _check_workspace_root(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".symphony-write-check"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"not writable: {exc}"
    return True, str(path)


def _content_length(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        return max(int(raw), 0)
    except ValueError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
