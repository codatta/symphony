# Symphony Phase 1 Handoff

## What It Is

Symphony turns Linear issues into isolated agent implementation runs. The current
Python CLI polls Linear, creates a per-issue workspace, renders the issue through
`WORKFLOW.md`, launches Codex, and exposes enough status/logging to inspect the
run. Codex can also use Symphony-managed Linear auth through the `linear_graphql`
tool to comment, update issue state, and attach PR links.

## Where We Are

Phase 1, the CLI Linear + Codex MVP, is closed for planning with one explicit
caveat: live Linear auth and a zero-candidate polling tick were verified, but a
real dispatch of one active Linear issue to Codex still needs proof.

Implemented in Phase 1:

- `WORKFLOW.md` parsing, strict config validation, prompt rendering, and hot
  reload.
- Linear API-key auth, candidate issue polling, state refresh, pagination, and
  normalized issue models.
- Orchestration state with claims, concurrency limits, retry/backoff,
  continuation retries, reconciliation, and cleanup decisions.
- Safe per-issue workspaces with sanitized paths, lifecycle hooks, root
  containment checks, and terminal cleanup behavior.
- Codex app-server runner, `linear_graphql` routing, and minimal status API.

This branch continues into Phase 2A onboarding:

- `symphony init` generates a starter `WORKFLOW.md`.
- `symphony doctor` validates workflow, auth, Codex command, workspace, logs,
  and status API readiness.
- `symphony run` is the productized daemon command; legacy
  `symphony WORKFLOW.md` remains compatible.
- Local Linear API keys can be stored outside the repo in
  `~/.config/symphony/credentials.json`.

## Where We Are Heading

The next goal is to prove the CLI path before expanding scope. Packaging docs
and install channels come next, followed by the live dispatch proof if it is not
completed during verification. OAuth / PKCE, native binaries, Homebrew, desktop
app, webhooks, and additional agent backends should stay deferred until the CLI
install and live Linear-to-Codex loop are proven.

## Verification Needed

Please test from a clean checkout or clean virtual environment. Use a disposable
Linear issue and avoid production-impacting work.

### 1. Local Gate

```bash
uv sync
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run symphony --help
uv run symphony init --help
uv run symphony doctor --help
uv run symphony run --help
git diff --check
```

Expected: tests pass, help commands exit successfully, and there are no
whitespace errors.

### 2. Onboarding Smoke Test

From a temporary test repository directory:

```bash
uv run symphony init \
  --yes \
  --project-slug <linear-project-slug> \
  --linear-api-key <linear-api-key>

uv run symphony doctor WORKFLOW.md
```

Expected:

- `WORKFLOW.md` is generated and parseable.
- The Linear key is stored in local credentials, not in `WORKFLOW.md`.
- `doctor` reports success for workflow, Linear auth, Codex command, workspace
  root, logs root, and status API.

### 3. Live Dispatch Proof

Create one disposable Linear issue in the configured project and move it into an
active state from the workflow, normally `Todo` or `In Progress`.

```bash
uv run symphony run WORKFLOW.md --once --log-level INFO
```

Expected:

- Symphony fetches the disposable issue.
- Symphony creates one isolated workspace.
- Codex starts with the rendered issue prompt.
- The run completes or leaves clear logs explaining the failure.
- The command prints a non-zero fetched or dispatched count for the test issue.

## Evidence To Return

Please send back the branch or commit tested, exact commands run, test summary,
disposable Linear issue identifier, whether the issue was fetched/dispatched,
and any logs or errors needed to reproduce failures. Also call out any mismatch
with `prd.md`, `SPEC.md`, or `ARCHITECTURE.md`.

If live dispatch fails, capture the failure and file or update follow-up work
against Phase 2A rather than expanding the current scope.

---

## Phase 2 Update: Claude Code Runner + PR Automation

### What Was Added

**Claude Code runner** (`feat/claude-code-runner`)

Symphony now supports Claude Code as an agent backend alongside Codex. Switch
with one line in `WORKFLOW.md`:

```yaml
agent:
  runner: claude_code   # or codex
```

Claude runs via `claude --print --output-format stream-json --permission-mode
bypassPermissions`. `LINEAR_API_KEY` and `GITHUB_TOKEN` are injected into the
subprocess environment automatically.

**PR automation and review feedback loop** (`feat/pr-automation`)

- `symphony init` now guides the user through GitHub token setup and validates
  the token on entry. Tokens for Linear and GitHub are stored together in
  `~/.config/symphony/credentials.json` without overwriting each other.
- Before rendering the prompt, Symphony fetches the Linear issue's comments and
  makes them available as `{{ issue.comments }}` in the template. Claude sees
  reviewer feedback on re-dispatch and addresses it directly.
- When `runner: claude_code` is used, `symphony init` generates a PR-aware
  prompt that instructs Claude to clone the target repository, implement the
  changes, open a PR via `gh pr create`, comment the PR URL on the Linear issue,
  and move the issue state to In Review.
- The startup snapshot mechanism was fixed: pre-existing issues at daemon start
  are skipped on the first tick, but if an issue leaves the active states and
  re-enters them it is treated as new and dispatched again. Previously the
  blacklist was permanent.

### Updated Usage Flow

**First-time setup (run once):**

```bash
uv run symphony init --project-slug <linear-project-slug>
```

The guided flow asks for:
1. Linear project slug — hint provided on where to find it
2. GitHub organisation or user name
3. Linear API key — stored in local credentials, validated on entry
4. GitHub personal access token (Contents + Pull requests R/W) — validated against GitHub API on entry

**Validate configuration:**

```bash
uv run symphony doctor WORKFLOW.md
```

**Start the daemon:**

```bash
uv run symphony run WORKFLOW.md
```

**Day-to-day workflow:**

1. Create or move a Linear issue to In Progress.
2. Symphony detects the new issue within 30 seconds and dispatches Claude.
3. Claude clones the repository specified in the issue description, implements
   the changes, opens a PR, comments the PR URL on the Linear issue, and moves
   the issue to In Review.
4. Engineer reviews the PR. If changes are needed, leave a comment on the Linear
   issue and move it back to In Progress.
5. Symphony detects the re-entry, fetches the comments, and dispatches Claude
   again. Claude reads the feedback and revises the implementation.
6. Repeat until the PR is approved. Move the issue to Done.

**Note on verification steps 2 and 3:** the default runner is now `claude_code`.
To reproduce the original Codex-based verification, pass `--runner codex`
explicitly:

```bash
uv run symphony init \
  --yes \
  --runner codex \
  --project-slug <linear-project-slug> \
  --linear-api-key <linear-api-key>
```
