# Symphony — Product Requirements Document

## Status: Draft v0.1 — Open for review before implementation begins

> See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design, component diagram, data flows, and module layout.

---

## 1. Overview

Symphony is an orchestration service that polls a work tracker (Linear), creates isolated workspaces per issue, and runs AI agent sessions against those workspaces. The existing Elixir reference implementation is tightly coupled to the Codex app-server JSON-RPC protocol. This PRD covers the design and build queue for a new implementation that supports multiple agent backends.

---

## 2. Tech Stack Decision

### Candidates considered

| Language | Pros | Cons |
|---|---|---|
| **Elixir (extend current)** | Already spec-compliant; OTP supervision is excellent; hot reload | Smaller contributor pool; harder to integrate Python/TS AI SDKs; Codex coupling runs deep |
| **Python 3.12+** | Best subprocess orchestration; all major AI SDKs (Anthropic, OpenAI, Google); `asyncio` handles concurrent agents cleanly; `pydantic` for config schema | Slower startup; not a single binary out-of-the-box |
| **TypeScript / Bun** | Claude Code SDK is TypeScript-native; strong typing; Bun compiles to single binary | Node.js subprocess management is more complex; weaker daemon patterns |
| **Go** | Single binary; excellent concurrency; fast | Smaller AI SDK ecosystem; verbose for rapid experimentation |

### Recommendation: **Python 3.12+ with asyncio**

**Rationale:**

1. **Subprocess orchestration is the core runtime primitive.** Managing N concurrent agent CLI processes (Codex, Claude Code, Gemini CLI) with streaming stdout/stderr, timeouts, stall detection, and cancellation maps cleanly to `asyncio.create_subprocess_exec`. Python's subprocess model is the most ergonomic for this pattern.
2. **All agent SDKs have first-class Python support.** Anthropic SDK, OpenAI SDK, Google Generative AI SDK — all available and actively maintained.
3. **Pydantic** gives Ecto-equivalent typed config validation with `$VAR` resolution, defaults, and schema errors at startup.
4. **`asyncio` + `anyio`** handles the orchestrator's event loop (tick, retry timers, reconciliation) more directly than OTP GenServer while remaining approachable.
5. **FastAPI + SSE** covers the optional HTTP observability server cleanly.
6. **`watchfiles`** provides WORKFLOW.md hot reload.

### Core dependencies

```
python          3.12+           runtime
pydantic        2.x             config schema, WORKFLOW.md validation
httpx           async           Linear GraphQL, image generation APIs
jinja2          2.x             prompt template rendering (strict mode)
typer           CLI             CLI entrypoint with --port, --logs-root
fastapi         HTTP server     /api/v1/* and optional LiveView-style dashboard
uvicorn         ASGI            HTTP server
watchfiles      fs watch        WORKFLOW.md hot reload
anthropic       0.x             Claude API integration
openai          1.x             Codex / GPT-Image-1 API integration
google-genai    1.x             Gemini API integration
pyyaml          6.x             WORKFLOW.md front matter
anyio           task groups     structured concurrency for agent sessions
```

### Project layout

```
symphony/
  symphony/
    cli.py                  # typer CLI entrypoint
    orchestrator.py         # poll loop, claims, retries, reconciliation
    config.py               # pydantic schema + $VAR resolution
    workflow.py             # WORKFLOW.md loader + jinja2 renderer
    workspace.py            # per-issue directories + hook execution
    tracker/
      base.py               # IssueTrackerAdapter ABC
      linear.py             # Linear GraphQL implementation
    agents/
      base.py               # AgentRunner ABC — the key new abstraction
      codex.py              # Codex app-server JSON-RPC adapter
      claude_code.py        # Claude Code CLI adapter
      gemini_cli.py         # Gemini CLI adapter
      openai_api.py         # OpenAI API adapter (includes image generation)
      hermes.py             # OpenAI-compatible API adapter (Ollama/vLLM)
    http_server.py          # FastAPI observability server
    log_file.py             # structured logging
  tests/
  WORKFLOW.md
  pyproject.toml
```

---

## 3. Core Architecture: Multi-Agent Adapter Pattern

The central change relative to the Elixir implementation is extracting a clean **AgentRunner** abstraction. All four new agent types map onto one of two adapter base classes:

### 3.1 CLIAgentRunner (subprocess-based)

Used by: Codex app-server, Claude Code CLI, Gemini CLI

```python
class CLIAgentRunner(ABC):
    async def start_session(self, workspace: Path) -> AgentSession: ...
    async def run_turn(self, session: AgentSession, prompt: str, issue: Issue) -> TurnResult: ...
    async def stop_session(self, session: AgentSession) -> None: ...
```

The runner launches a subprocess in the workspace directory, manages its lifecycle, enforces timeouts and stall detection, and streams events back to the orchestrator.

### 3.2 APIAgentRunner (API-based)

Used by: GPT-Image-1, Claude API (headless), Gemini API

```python
class APIAgentRunner(ABC):
    async def run_task(self, workspace: Path, prompt: str, issue: Issue) -> TaskResult: ...
```

API-based runners do not have persistent sessions or multi-turn streaming. They make a single API call, save the output to the workspace, and return.

### 3.3 Session contract (unchanged from SPEC.md)

Orchestrator → AgentRunner is a fire-and-forget task. Events are pushed back via a callback:

```python
async def on_event(event: AgentEvent) -> None
```

Event types mirror SPEC.md §10.4: `session_started`, `turn_completed`, `turn_failed`, `notification`, `malformed`, etc.

---

## 4. Feature Evaluation

### 4.1 Claude Code CLI

**Fit assessment: ✅ Strong fit**

Claude Code CLI (`claude`) supports a `--print` flag for non-interactive, single-turn output. Multi-turn agentic operation is done by piping new input into the session, or by calling the Anthropic API directly using the Claude Agent SDK for programmatic session management.

**Integration approach:**

Two sub-modes:

| Sub-mode | When to use | How |
|---|---|---|
| `cli-print` | Simple single-turn tasks | `claude --print "<prompt>"` in workspace dir; capture stdout |
| `api-agent` | Multi-turn agentic workflows (equivalent to Codex app-server) | Anthropic Python SDK, `anthropic.beta.messages.stream()`, tool_use for `linear_graphql` |

For full parity with the Codex integration, use `api-agent` mode. The `CLIAgentRunner` for Claude Code wraps the Python SDK's streaming API rather than a subprocess, since the SDK gives programmatic turn management.

**Configuration in WORKFLOW.md:**

```yaml
agent:
  runner: claude_code
  model: claude-sonnet-4-6
  max_tokens: 32768
  tool_use: true
```

**MCP tool integration:** Claude Code supports MCP natively. The `linear_graphql` dynamic tool can be served as an MCP server exposed to the session, consistent with the existing SPEC.md §10.5 extension contract.

**Verdict:** Implement as a first-class runner. Should be the second runner after Codex in the Build Queue.

---

### 4.2 Hermes Agent (OpenAI-Compatible Local Models)

**Fit assessment: ✅ Good fit**

NousResearch Hermes series (Hermes-2, Hermes-3) are fine-tuned LLMs with strong tool-use capability, typically served locally via Ollama or vLLM with an OpenAI-compatible API endpoint. They are _models_, not agents — they require a host process to manage the agentic loop.

Integration approach: An `OpenAICompatRunner` wraps the OpenAI SDK pointed at a local Ollama/vLLM endpoint (`base_url: http://localhost:11434/v1`). The orchestrator drives the tool-use loop, calling tools client-side (including `linear_graphql`). This is a generic "OpenAI-compatible API runner" that can target any model — Hermes, Mistral, DeepSeek, LLaMA, etc.

**Configuration:**

```yaml
agent:
  runner: openai_compatible
  base_url: http://localhost:11434/v1
  model: nous-hermes-3
  api_key: $HERMES_API_KEY
```

**Verdict:** Implement as `openai_compatible` runner — it is effectively free once the Claude API runner exists, since both use the same streaming tool-use loop pattern. Good for teams running local models or private inference endpoints.

---

### 4.3 Gemini CLI

**Fit assessment: ✅ Good fit**

Google's `gemini` CLI tool (released 2025) supports non-interactive invocation and can work on codebases. Its interface is similar to Claude Code CLI.

**Integration approach:**

`GeminiCLIRunner` as a `CLIAgentRunner` subclass:

```bash
# single turn
gemini --yolo "<prompt>"

# or with stdin piping for longer prompts
echo "<prompt>" | gemini --yolo
```

For multi-turn agentic operation with full tool use, the `google-genai` Python SDK (v1.x) is preferable — it provides streaming, function calling, and code execution tool support.

**Configuration:**

```yaml
agent:
  runner: gemini_cli
  model: gemini-2.5-pro
```

OR for API-based multi-turn:

```yaml
agent:
  runner: gemini_api
  model: gemini-2.5-pro
  api_key: $GOOGLE_API_KEY
```

**Key limitation:** Gemini CLI's tool-use protocol (for `linear_graphql` equivalent) is less standardized than Codex app-server or Anthropic SDK tool_use. The first implementation should use the `google-genai` SDK for full tool-use support rather than the raw CLI.

**Verdict:** Implement as a `gemini_api` runner using the Python SDK. The CLI wrapper is a nice-to-have but the SDK gives better control over multi-turn sessions and tool calls.

---

### 4.4 GPT-Image-1 (Image Generation Tasks)

**Fit assessment: ⚠️ Partial fit — requires design extension, represents meaningful scope expansion**

GPT-Image-1 (OpenAI's image generation model, formerly DALL-E 3) is a **generative API**, not a coding agent. There is no multi-turn loop, no tool use, no workspace file manipulation — it takes a text prompt and returns an image.

**Where it fits in the Symphony model:**

Symphony's core is: _issue → workspace → agent turns → output_. Image generation maps to this as:

- **Issue:** A Linear task like "Generate hero image for landing page v2"
- **Workspace:** Directory where the generated images are saved
- **"Turn":** A single API call to GPT-Image-1 with the rendered prompt
- **Output:** PNG files committed to the workspace; PR attached to Linear issue

This works, but requires a new concept: **task type**. Coding agents do multiple turns of file editing. Image generators do a single (or few) API calls and save files.

**Integration approach — `ImageGenerationRunner`:**

```python
class ImageGenerationRunner(APIAgentRunner):
    async def run_task(self, workspace: Path, prompt: str, issue: Issue) -> TaskResult:
        # 1. Call openai.images.generate(model="gpt-image-1", prompt=prompt, ...)
        # 2. Save PNG to workspace/<issue_identifier>/<timestamp>.png
        # 3. Commit to workspace branch
        # 4. Return TaskResult with file paths
```

**Design implications:**

1. **No stall detection needed** — API calls have fixed timeouts.
2. **No multi-turn continuation** — normal worker exit immediately after one call.
3. **Prompt template** still applies — WORKFLOW.md body describes what to generate, using `{{ issue.title }}`, `{{ issue.description }}`, etc.
4. **Workspace hooks still apply** — `after_create` can set up a git repo; `after_run` can auto-commit.
5. **`linear_graphql` tool** is not applicable — image generator doesn't call tools.

**What this enables for teams:**

- Design tasks in Linear → auto-generate assets → PR with images for review
- UI component sketch generation
- Marketing copy + image generation pipelines
- Multimodal workflows where a coding agent (Claude/Codex) consumes the generated images in a follow-on issue

**Verdict:** Valid fit with clear boundaries. Recommend implementing after the CLI agent runners are stable. Requires adding `task_type: generative | agentic` to the runner config (default: `agentic`). The architectural surface area is small — it's essentially an `APIAgentRunner` with file output and no turn loop.

**Caveat:** If the intent is for GPT-Image-1 to be _called by a coding agent_ (e.g., Claude Code calls the image API as a tool), that is already handled through the agent's built-in tool use or MCP — Symphony doesn't need a dedicated runner for that case.

---

### 4.5 Mac Desktop App

**Fit assessment: ✅ Strong fit — distribution layer, not a core change**

Distributing Symphony as a native macOS application removes the requirement for users to install Python, manage a terminal daemon, or understand CLI conventions. The orchestrator logic is unchanged — the desktop app is a shell that manages the process lifecycle and surfaces the existing web dashboard in a native window.

**What "desktop app" means here:**

| Layer | What it does |
|---|---|
| macOS app bundle (`.app` / `.dmg`) | Installlable via drag-and-drop; no Python/pip required |
| Menubar / system tray icon | Start/stop daemon, quick status, open dashboard |
| Native notifications | Desktop alerts when agents finish, get blocked, or need review |
| Embedded web dashboard | The existing FastAPI + React frontend, rendered in a WebView |
| Python sidecar | The orchestrator daemon, bundled and managed by the app shell |

**Recommended approach: Tauri v2 + PyInstaller sidecar**

Tauri v2 is a Rust-based desktop app framework that wraps a web frontend in a lightweight native shell (no Chromium bundle — uses the OS WebView, which is WKWebView on macOS). The Python orchestrator is bundled as a standalone binary via PyInstaller and registered as a Tauri sidecar. The web frontend (React or Svelte) is shared with the browser dashboard.

```
symphony-desktop/
  src-tauri/         # Rust Tauri shell
    sidecar/         # PyInstaller-built symphony binary
    tauri.conf.json  # window config, sidecar, permissions
  src/               # React/Svelte frontend (reused from web dashboard)
```

**Why Tauri over alternatives:**

| Option | Why not |
|---|---|
| Electron | ~150 MB Chromium bundle per install; heavy for a daemon wrapper |
| Swift/SwiftUI | Separate codebase from the web dashboard; more maintenance |
| `rumps` + `pywebview` | No `.dmg` distribution without significant packaging work; less native |
| Web only (no desktop app) | Requires users to manage a terminal process manually |

**Key Tauri plugins used:**

- `tauri-plugin-shell` — manage Python sidecar lifecycle (start on launch, kill on quit)
- `tauri-plugin-notification` — native macOS notifications for agent events
- `tauri-plugin-updater` — auto-update from GitHub Releases
- `tauri-plugin-single-instance` — prevent multiple Symphony processes

**Distribution path:**

1. `tauri build` produces a signed `.dmg` for direct download
2. Auto-update checks a GitHub Releases feed
3. Mac App Store distribution is possible but requires sandboxing review — treat as a later milestone

**Architecture impact on the Python backend:**

Minimal. The daemon gains one new startup flag `--headless` (suppresses terminal output when launched by the desktop app) and exposes a health endpoint at `/api/v1/health` for the Tauri shell to poll. The sidecar communicates with the frontend over the existing FastAPI HTTP server on localhost.

**Verdict:** Implement after the core Python implementation is stable. The desktop app is a packaging and distribution milestone — it does not require changes to the orchestration logic.

---

### 4.6 Remote Phone Coordination

**Fit assessment: ✅ Strong fit — extends the operator loop to mobile**

Symphony's core value is autonomous agent execution. But agents regularly reach points where human judgment is needed: an issue moves to `Human Review`, an agent is blocked by a missing credential, or an approval gate fires. Today, operators must be at a desk watching the dashboard. Remote phone coordination closes this gap — operators get notified on their phone and can respond without opening a laptop.

**Two sub-features:**

#### A. Mobile monitoring (read-only)

Operators can view active sessions, queue depth, token consumption, and per-issue status from any device. This is largely free if the web dashboard is built as a **Progressive Web App (PWA)**: responsive layout + Web App Manifest + service worker. Operators install it on their home screen; it behaves like a native app.

PWA Web Push now works on iOS 16.4+ (Safari finally shipped it in 2023), which covers the majority of phone-based operators.

#### B. Push notifications + action gates (the valuable part)

Symphony sends a push notification to the operator's phone when an agent needs attention. The notification includes the issue identifier, state, and reason, plus one-tap action buttons.

**Notification triggers:**

| Event | Notification content | Actions |
|---|---|---|
| Issue → `Human Review` | "MT-42: PR ready for review" | Open PR, Open issue |
| Agent blocked (missing auth) | "MT-55 blocked: missing GITHUB_TOKEN" | Open dashboard |
| Agent stalled (stall timeout) | "MT-60 stalled after 5m of inactivity" | Retry, Cancel |
| Worker failure (after N retries) | "MT-71 failed: 3 retries exhausted" | View logs |
| Agent requests approval (non-auto-approve policy) | "MT-80 awaiting command approval" | Approve, Reject |

**Notification backend options:**

| Backend | Pros | Cons |
|---|---|---|
| **ntfy** (recommended) | Self-hosted or ntfy.sh cloud; zero mobile app needed (app exists on iOS/Android); HTTP `POST` to push; free | Requires ntfy app installed |
| **Pushover** | Reliable; one-time $5 per platform; great iOS/Android apps | Paid; third-party dependency |
| **Web Push (APNs/FCM)** | Native to PWA; no extra app | Requires VAPID key setup; complex on iOS |
| **Webhook** | Generic; operators wire to Slack, Teams, Discord, etc. | No action buttons; read-only |
| **Telegram Bot** | Free; global; action buttons via inline keyboard | Requires Telegram account |

**Recommendation: ntfy as primary, webhook as generic fallback.** ntfy's HTTP API is trivially simple (`POST https://ntfy.sh/my-topic`), its apps are on iOS and Android, and it can be self-hosted for air-gapped deployments. The webhook backend means operators who prefer Slack or Teams can wire it up themselves.

**Approval workflow architecture:**

When an agent hits an approval gate that is not auto-resolved:

1. Orchestrator emits `approval_requested` event
2. `NotificationService` sends push notification with `approve_url` and `reject_url` deep links
3. Operator taps Approve → browser (or PWA) opens, calls `POST /api/v1/sessions/<session_id>/approve`
4. Orchestrator receives approval, unblocks the agent turn
5. If no response within `approval_timeout_ms`, treat as rejection per configured policy

**WORKFLOW.md config additions:**

```yaml
notifications:
  backend: ntfy                        # ntfy | pushover | webhook | telegram
  ntfy_topic: $SYMPHONY_NTFY_TOPIC    # ntfy topic URL or topic name
  webhook_url: $SYMPHONY_WEBHOOK_URL  # generic webhook fallback
  approval_timeout_ms: 300000          # 5 minutes; treat as reject after this
  events:                              # which events trigger notifications
    - human_review
    - agent_blocked
    - agent_stalled
    - worker_failed
    - approval_requested
```

**New API endpoints:**

```
POST /api/v1/sessions/<session_id>/approve    # operator approves a gate
POST /api/v1/sessions/<session_id>/reject     # operator rejects
GET  /api/v1/health                           # liveness check for desktop sidecar
```

**Architecture impact:** A new `NotificationService` module that subscribes to orchestrator events and dispatches push messages. It is observability-only for monitoring events; for approval gates it becomes load-bearing (the orchestrator waits on the approval channel with a timeout). The approval gate integrates with the existing `codex.approval_policy` model — it activates only when policy is `on-request` rather than `never` or `untrusted`.

**Verdict:** Strong fit. Start with ntfy + webhook (low effort, high operator value). The approval gate is the premium feature — build it after the notification plumbing is proven. The PWA mobile dashboard is effectively free once the web dashboard is responsive.

---

### 4.7 Linear Integration & Authentication

**Fit assessment: ✅ Core requirement — Linear is the primary UX surface**

Symphony's orchestration loop is entirely driven by Linear state. Every goal, objective, and work item enters the system as a Linear issue. This is not just an integration — it is the interface. The system needs:

1. **Reliable authentication** that works for CLI, desktop app, and CI without different code paths
2. **Real-time coordination via webhooks**, not just polling — when an operator moves an issue to a different state in Linear, agents must react within seconds
3. **A setup wizard** so non-CLI users can connect Linear and generate a WORKFLOW.md without touching config files
4. **Token security** appropriate to each deployment context (env var for CI, Keychain for desktop)

**Authentication design:**

Two modes are supported and coexist:

| Mode | How | Storage |
|---|---|---|
| **Personal API key** | `LINEAR_API_KEY` env var or `tracker.api_key` in WORKFLOW.md | Env / WORKFLOW.md |
| **OAuth 2.0** | Full consent flow via Linear Application; bearer token stored securely | macOS Keychain (desktop) or `~/.symphony/credentials.json` (CLI) |

Token resolution order (first non-empty wins): env var → WORKFLOW.md → Keychain → credentials file. The `LinearClient` never reads credentials directly — it receives a resolved token from `TokenStore`.

**OAuth scopes required:** `read`, `write` (issue state + comments), optionally `app:assignIssues`.

**Webhook vs polling:**

| | Polling only | Polling + Webhooks |
|---|---|---|
| Reaction time | 5–30 s | < 1 s |
| Linear API calls | O(N × ticks) | O(1) per change + periodic reconcile |
| Requires public URL | No | Yes (or a tunnel) |

When webhooks are active, `polling.interval_ms` can be raised to 120 000 ms (2 min) as a safety net. Webhooks handle the fast path; polling catches missed events.

**Setup wizard** (desktop app first-run, also accessible via `/setup` in the web UI):

Step 1 → Connect Linear (OAuth) → Step 2 → Select team + project → Step 3 → Configure active/terminal states → Step 4 → Choose AI agent + enter API key → Step 5 → Preview + save generated WORKFLOW.md → Step 6 → Launch

**New config fields** (`tracker` block in WORKFLOW.md):
```yaml
tracker:
  team_id: "..."
  oauth_client_id: $LINEAR_CLIENT_ID
  oauth_client_secret: $LINEAR_CLIENT_SECRET
  webhook_secret: $LINEAR_WEBHOOK_SECRET
server:
  public_url: $SYMPHONY_PUBLIC_URL
  tunnel: none                          # none | cloudflared | ngrok
```

**Verdict:** Implement Linear auth + webhook support before shipping any agent runner to production. Without webhooks, Symphony is too slow for real team use. Without OAuth, the desktop app has no clean first-run flow.

---

## 5. Summary: Feature Fit Matrix

| Feature | Fit | Layer | Effort | Key Dependency |
|---|---|---|---|---|
| **Linear Auth + OAuth** | ✅ Core requirement | Auth / tracker | Medium | `keyring`, `httpx` |
| **Linear Webhooks** | ✅ Core requirement | Tracker / HTTP | Medium | `cryptography` (HMAC) |
| **Setup Wizard** | ✅ Strong | Web UI + HTTP | Medium | Linear OAuth |
| Claude Code (API/SDK) | ✅ Strong | Agent runner | Medium | `anthropic` Python SDK |
| Gemini CLI / API | ✅ Good | Agent runner | Medium | `google-genai` SDK |
| Hermes / OpenAI-compatible | ✅ Good | Agent runner | Low | `openai` SDK (reuse) |
| GPT-Image-1 | ⚠️ Scope expansion | Agent runner (generative) | Medium | `openai` SDK |
| Mac Desktop App | ✅ Strong | Distribution shell | Medium-High | Tauri v2, PyInstaller |
| Remote Phone / IM | ✅ Strong | Notifications + mobile UI | Medium | `aiogram` / `slack_bolt` |

---

## 6. Build Queue

> Items are ordered. Complete one fully before starting the next.

### 🔜 Next Up

- [ ] **[Linear: Authentication]** — Implement `TokenStore`, OAuth 2.0 flow, and `symphony auth linear` CLI command
  - **User:** All Symphony users — authentication is a gate for every other feature
  - **Acceptance Criteria:**
    - `TokenStore.resolve()` checks env → WORKFLOW.md → Keychain → credentials file, in order
    - `symphony auth linear` CLI command: opens OAuth URL, captures code via ephemeral server, exchanges for token, stores in `~/.symphony/credentials.json` (mode `0o600`)
    - `symphony auth linear --status` prints workspace name, actor, token age
    - `symphony auth linear --revoke` clears token from all stores
    - `GET /api/v1/linear/auth/url` returns OAuth authorize URL with CSRF nonce
    - `POST /api/v1/linear/auth/callback` exchanges code, stores token, returns workspace/actor
    - `GET /api/v1/linear/auth/status` returns auth state for setup wizard
    - `DELETE /api/v1/linear/auth/revoke` clears token
    - Token is never printed in logs, error messages, or API responses
    - Desktop app: token stored in macOS Keychain via `keyring`
    - OAuth client_id/secret read from env (`$LINEAR_CLIENT_ID`, `$LINEAR_CLIENT_SECRET`)
  - **Technical Notes:** `keyring` library wraps Keychain on macOS and Secret Service on Linux; CSRF nonce stored in memory for the duration of the OAuth round-trip; credentials file path: `~/.symphony/credentials.json`
  - **Tests Required:**
    - TokenStore priority order (env beats file beats keychain)
    - Token never appears in any log output
    - Credential file created with correct permissions
    - CSRF nonce mismatch rejected
    - Revoke clears all stores

- [ ] **[Linear: Webhook Integration]** — Real-time issue state coordination via Linear webhooks
  - **User:** Teams where issue state changes must trigger agent reactions within seconds
  - **Acceptance Criteria:**
    - `linear_webhook.ensure_registered()` auto-registers a webhook for the configured team on startup when `server.public_url` is set
    - `POST /linear/webhook` endpoint: verifies `X-Linear-Signature` HMAC-SHA256 and returns 401 on failure
    - Issue create event → triggers immediate dispatch check (skip waiting for next tick)
    - Issue update event (state change) → `orchestrator.reconcile_issue(id)` — stops agent if state terminal
    - Issue remove event → release claim + clean workspace
    - Webhook processing is async and never blocks the 200 response
    - `tunnel: cloudflared` in WORKFLOW.md → Symphony spawns `cloudflared tunnel --url http://localhost:<port>`, captures the printed HTTPS URL, registers webhook using it
    - `tunnel: ngrok` → same via `pyngrok`
    - When no public URL and no tunnel: webhooks disabled, warning surfaced in dashboard; polling continues normally
    - Dashboard shows: webhook status (active / inactive), last event timestamp
    - `polling.interval_ms` recommendation: raise to `120000` when webhooks active (shown in setup wizard)
  - **Technical Notes:** HMAC verified with `hmac.compare_digest` to prevent timing attacks; webhook_id persisted to `~/.symphony/state.json`; cloudflared subprocess stderr parsed for the `trycloudflare.com` URL
  - **Tests Required:**
    - Valid HMAC accepted; invalid rejected with 401
    - Issue update with terminal state → `reconcile_issue` called
    - Issue create → dispatch check triggered
    - No public URL → graceful degradation to polling

- [ ] **[Linear: Setup Wizard]** — Guided first-run flow to connect Linear and generate WORKFLOW.md
  - **User:** Non-CLI users setting up Symphony for the first time via desktop app or web UI
  - **Acceptance Criteria:**
    - `GET /api/v1/linear/teams` returns team list (requires valid auth)
    - `GET /api/v1/linear/projects?teamId=` returns projects in team
    - `GET /api/v1/linear/workflow-states?teamId=` returns all states with type metadata
    - `POST /api/v1/linear/generate-workflow` renders a WORKFLOW.md from inputs
    - Setup wizard accessible at `/setup` in the web UI and opened automatically on desktop when no WORKFLOW.md configured
    - Six-step flow: Connect → Team/Project → States → Agent → Preview → Launch
    - State step pre-selects sensible defaults (Todo + In Progress = active; Done + Cancelled + Closed = terminal)
    - Generated WORKFLOW.md is syntax-highlighted in the preview step; [Download] and [Save to path] buttons
    - Wizard skipped if valid WORKFLOW.md and Linear token already present
    - On completion, daemon starts (or restarts) with the new WORKFLOW.md
  - **Technical Notes:** Wizard is a React multi-step form in the web frontend; state is held client-side until final submit; `generate-workflow` uses jinja2 to render from a bundled template
  - **Tests Required:**
    - `generate-workflow` produces valid YAML front matter + non-empty prompt body
    - Missing auth → `teams` endpoint returns 401
    - All six steps reachable and submittable

- [ ] **[Core: Python Implementation]** — Implement SPEC.md §3–§14 conformance in Python, replacing the Elixir reference impl
  - **User:** Teams running multi-agent coding workflows
  - **Acceptance Criteria:**
    - `CLIAgentRunner` base class with Codex app-server adapter (functional parity with Elixir)
    - `APIAgentRunner` base class
    - Orchestrator: poll loop, dispatch, claims, retry/backoff, reconciliation
    - Workspace manager: hooks, sanitized paths, safety invariants (SPEC §9.5)
    - Config: pydantic schema, `$VAR` resolution, `~` expansion, dynamic WORKFLOW.md reload
    - Linear tracker adapter: candidate fetch, state refresh, pagination
    - HTTP observability server: `/api/v1/state`, `/api/v1/<identifier>`, `/api/v1/refresh`
    - Structured logging with `issue_id`, `issue_identifier`, `session_id`
    - CLI: positional workflow path, `--port`, `--logs-root`
    - All SPEC §17 Core Conformance tests pass
  - **Technical Notes:** Start from SPEC.md not the Elixir source; use `asyncio.TaskGroup` for concurrent sessions; pydantic `model_validator` for config cross-field checks
  - **Tests Required:**
    - Config parsing (defaults, `$VAR`, invalid YAML, hot reload)
    - Workspace safety invariants (path traversal rejection)
    - Dispatch priority sort
    - Retry backoff formula
    - Reconciliation state transitions
    - Linear adapter (mock HTTP)

- [ ] **[Agent: Claude Code]** — Implement `ClaudeCodeRunner` using Anthropic Python SDK
  - **User:** Teams using Claude as their primary coding agent
  - **Acceptance Criteria:**
    - Multi-turn streaming session via `anthropic.messages.stream()`
    - `linear_graphql` tool exposed via `tools=[...]` in API call
    - Session events normalized to Symphony event schema
    - Token usage extracted from API response and reported to orchestrator
    - Rate limit headers tracked and surfaced in `/api/v1/state`
    - WORKFLOW.md runner config: `runner: claude_code`, `model`, `max_tokens`
    - Continuation turns reuse existing thread context (conversation history)
    - All SPEC §17.5 App-Server Client tests pass (adapted for SDK-based runner)
  - **Technical Notes:** Use `anthropic` SDK v0.40+; tool_use follows `tool_use` + `tool_result` blocks; stall detection uses last streaming event timestamp
  - **Tests Required:**
    - Tool call dispatch (linear_graphql success, failure, unsupported tool)
    - Turn completion / failure event normalization
    - Token accounting across multiple turns

- [ ] **[Agent: OpenAI-Compatible / Hermes]** — Implement `OpenAICompatRunner` for any OpenAI-protocol endpoint
  - **User:** Teams running local LLMs (Ollama, vLLM, LM Studio) or Hermes models
  - **Acceptance Criteria:**
    - Configurable `base_url` and `model` in WORKFLOW.md
    - Tool use via OpenAI function-calling protocol
    - `linear_graphql` tool advertised and handled
    - Continuation turns via conversation history accumulation
    - Config: `runner: openai_compatible`, `base_url: $OLLAMA_URL`, `model: nous-hermes-3`
  - **Technical Notes:** Reuses `openai` SDK with `base_url` override; covers Codex API mode as well
  - **Tests Required:**
    - Tool call dispatch via function-calling protocol
    - Base URL resolution from `$VAR`

- [ ] **[Agent: Gemini API]** — Implement `GeminiAPIRunner` using `google-genai` SDK
  - **User:** Teams using Google Gemini as their coding agent
  - **Acceptance Criteria:**
    - Multi-turn streaming session via `google.generativeai.GenerativeModel.generate_content_async()`
    - `linear_graphql` tool exposed as a `FunctionDeclaration`
    - Token usage extracted and reported
    - Config: `runner: gemini_api`, `model: gemini-2.5-pro`, `api_key: $GOOGLE_API_KEY`
  - **Technical Notes:** `google-genai` 1.x uses `genai.Client()`; function calling returns `FunctionCall` parts; handle `RECITATION`/safety blocks as turn failures
  - **Tests Required:**
    - Function call dispatch
    - Safety block mapped to `turn_failed` event

- [ ] **[Agent: GPT-Image-1]** — Implement `ImageGenerationRunner` for image-generative tasks
  - **User:** Design teams tracking visual asset creation in Linear
  - **Acceptance Criteria:**
    - Single API call to `openai.images.generate(model="gpt-image-1", ...)`
    - Generated images saved to workspace with timestamp-based filenames
    - Images committed to workspace branch via `after_run` hook pattern (or built-in git commit)
    - Prompt rendered from WORKFLOW.md template (`{{ issue.title }}`, `{{ issue.description }}`)
    - WORKFLOW.md config: `runner: gpt_image`, `model: gpt-image-1`, `size: 1024x1024`, `quality: high`
    - WORKFLOW.md config: `task_type: generative` (disables turn continuation loop)
    - Linear issue linked to generated assets via `linear_graphql` comment
    - No stall detection, no multi-turn loop
  - **Technical Notes:** Introduce `task_type: agentic | generative` in config schema; `generative` runners skip the turn loop and re-dispatch after single API call; image URLs from API are downloaded and saved locally
  - **Tests Required:**
    - Image saved to correct workspace path
    - Prompt rendered correctly from template
    - API failure mapped to worker failure + retry

- [ ] **[Mobile: Push Notifications]** — Emit operator alerts for key agent events via ntfy / webhook
  - **User:** Operators who need to stay informed without watching a dashboard
  - **Acceptance Criteria:**
    - `NotificationService` subscribes to orchestrator events and dispatches push messages
    - `ntfy` backend: HTTP `POST` to configured ntfy topic with title, body, and priority
    - `webhook` backend: HTTP `POST` with JSON payload to configured URL
    - WORKFLOW.md config: `notifications.backend`, `notifications.ntfy_topic`, `notifications.webhook_url`, `notifications.events` list
    - Events dispatched: `human_review`, `agent_blocked`, `agent_stalled`, `worker_failed`
    - Notification failures are logged and ignored — never crash or stall the orchestrator
    - `notifications.events: []` disables all notifications
  - **Technical Notes:** `NotificationService` is a fire-and-forget `asyncio.Task`; use `httpx.AsyncClient` for both backends; support `$VAR` for topic/URL values
  - **Tests Required:**
    - ntfy POST payload shape and headers
    - Webhook POST payload shape
    - Notification failure does not propagate to orchestrator
    - `events` filter correctly suppresses notifications

- [ ] **[Mobile: PWA Dashboard]** — Make the web dashboard installable as a Progressive Web App
  - **User:** Operators who want a phone home-screen shortcut to the dashboard
  - **Acceptance Criteria:**
    - `manifest.json` served at `/manifest.json` with app name, icons, `display: standalone`
    - Service worker that caches the app shell for offline load
    - Responsive layout — all `/api/v1/state` data readable on a 390px screen
    - iOS Safari and Android Chrome "Add to Home Screen" flow works
    - Web Push VAPID key configured via `notifications.vapid_key` in WORKFLOW.md
    - Opt-in Web Push permission request on first open
  - **Technical Notes:** Service worker registered from the React/Svelte frontend; VAPID key generation documented in README; `pywebpush` on the FastAPI side for Web Push delivery
  - **Tests Required:**
    - `manifest.json` fields present and valid
    - Dashboard renders correctly at 390px and 768px viewports

- [ ] **[Mobile: Approval Gate]** — Let operators approve or reject agent action gates from their phone
  - **User:** Operators running non-auto-approve agent policies who need to unblock agents remotely
  - **Acceptance Criteria:**
    - New orchestrator event: `approval_requested` with `session_id`, `issue_identifier`, `prompt`
    - Push notification sent on `approval_requested` with Approve and Reject action deep-links
    - `POST /api/v1/sessions/<session_id>/approve` and `/reject` endpoints
    - Orchestrator holds the agent turn waiting on the approval channel (async `asyncio.Event`)
    - `notifications.approval_timeout_ms` (default `300000`): treat as reject after timeout
    - Only activates when `codex.approval_policy: on-request`; auto-approve and never-approve policies bypass this
    - Approval response is logged with timestamp and operator source (IP)
  - **Technical Notes:** Approval channel is an `asyncio.Event` stored in the session state; `/approve` endpoint sets it; timeout via `asyncio.wait_for`; the gate must not block the orchestrator tick loop
  - **Tests Required:**
    - Approve path resumes agent turn
    - Reject path fails the current turn and triggers retry
    - Timeout elapses → treated as rejection
    - Gate bypassed when policy is `never` or `untrusted`

- [ ] **[Desktop: Mac App]** — Distribute Symphony as a native macOS application
  - **User:** Non-CLI users who want to install Symphony like a normal Mac app
  - **Acceptance Criteria:**
    - Tauri v2 app shell wraps the Python sidecar (PyInstaller bundle) and web frontend
    - `.dmg` built via `tauri build`; drag-to-`/Applications` install
    - Menubar icon shows live agent count (e.g. `♩ 3 running`)
    - Menubar dropdown: Open Dashboard, Start/Stop Symphony, Preferences, Quit
    - Native macOS notification for each event in the configured `notifications.events` list
    - Dashboard opens in an embedded WKWebView window (not the user's browser)
    - Sidecar starts automatically on app launch; stops on quit
    - `tauri-plugin-single-instance` prevents duplicate daemons
    - Auto-update via GitHub Releases feed (`tauri-plugin-updater`)
    - WORKFLOW.md path configurable in Preferences (persisted via `tauri-plugin-store`)
  - **Technical Notes:** Python sidecar registered in `tauri.conf.json` `externalBin`; PyInstaller build step added to CI; `--headless` flag suppresses terminal output when launched by Tauri; health check at `GET /api/v1/health` polled by Tauri every 5s; Tauri Rust code is minimal — just sidecar lifecycle + notifications
  - **Tests Required:**
    - Sidecar starts and responds to `/api/v1/health` within 5s of app launch
    - Sidecar terminates cleanly when app quits
    - Menubar agent count updates when `/api/v1/state` changes
    - Preferences WORKFLOW.md path is persisted across restarts

---

### 🔵 Backlog

- [ ] **[Tracker: GitHub Issues adapter]** — support GitHub Issues as an alternative to Linear
- [ ] **[Tracker: Jira adapter]** — support Jira projects
- [ ] **[SSH Worker Extension]** — port Appendix A SSH worker extension from Elixir impl
- [ ] **[Observability: LiveDashboard]** — rich real-time dashboard (SSE/WebSocket push)
- [ ] **[Security: Workspace sandboxing]** — Docker/cgroup-based execution isolation per workspace
- [ ] **[Config: Multi-runner per workflow]** — dispatch different issue labels to different agent runners
- [ ] **[Retry: Persistent queue]** — survive process restarts without losing retry state (SQLite)
- [ ] **[Multimodal: Vision input]** — pass screenshot/image from workspace into agent prompt (Claude/Gemini)

---

## 7. Open Questions

1. **Linear OAuth app registration:** Should Symphony ship with a shared OAuth client_id (users install the Symphony Linear app from Linear's marketplace), or does each team register their own Linear application with their own client_id/secret?
2. **Hermes deployment:** Is the target Ollama on localhost, a remote vLLM cluster, or a hosted inference endpoint?
2. **GPT-Image-1 workflow:** Should Symphony auto-commit generated images and open a PR, or just save to workspace and leave the commit to a coding agent in a subsequent issue?
3. **Runner selection:** Should WORKFLOW.md support a single `runner` per workflow, or a per-label/per-state dispatch map (e.g., `In Progress → claude_code`, `Merging → codex`)?
4. **Tracker scope:** Linear-only for the initial Python implementation, or should GitHub Issues be co-designed from the start to avoid Linear-specific leakage in the adapter interface?
5. **Desktop app distribution:** Direct `.dmg` download from GitHub Releases, or target the Mac App Store (requires sandboxing and notarization review)?
6. **IM backend priority:** Telegram (simpler setup, free, long polling) or Slack (enterprise-friendly, socket mode)? Both are designed; see ARCHITECTURE.md §7.5 for the trade-off table.
7. **Approval gate scope:** Is remote approval of agent action gates (approve/reject from phone) a launch requirement, or is read-only monitoring + `Human Review` notifications sufficient for v1?
