# Hermes + OpenCode for Windows

Chat in Hermes, choose a free OpenCode model, and delegate independent work to parallel OpenCode workers. Hermes owns memory, skills, and worker-control tools; OpenCode runs native coding tools. LM Studio is not required.

```text
You -> Hermes -> localhost gateway -> installed OpenCode agent -> selected model
                  Hermes worker tools -> gateway worker pool -> parallel OpenCode sessions
You <- Hermes replies and worker results
```

## How a request works

1. You choose a model at launch and send your task in Hermes.
2. Hermes sends its conversation, memory context, and enabled memory/skill/worker tool schemas to the local gateway.
3. The gateway starts OpenCode with the selected model. OpenCode edits files and runs commands with its own tools; progress returns to Hermes.
4. The gateway translates memory, skill, or worker requests into Hermes tool calls. Hermes executes memory/skills itself and invokes its project-local MCP server for worker control. Results return through the conversation.
5. The gateway saves task context. An explicitly model-specific limit can hand unfinished work to another free model; a shared free-usage limit saves progress and stops. The final answer appears in Hermes.

Hermes remains your interface and owns its memory/skills. OpenCode supplies the model connection and coding tools. The memory/skill translation is experimental; the verification section distinguishes local checks from live model tests.

## Set up another laptop

Install Hermes Agent and the native OpenCode CLI first. OpenCode must already be able to use your selected provider/model under its own account. This project never copies accounts, cookies, API keys, or another machine's settings.

Clone or download this repository, then run:

```powershell
git clone https://github.com/jestermoon01/Hermes-Opencode-.git
cd Hermes-Opencode-
.\setup-hermes-opencode.bat
.\hermes-opencode.bat
```

Setup detects the executables and prepares the locked Python environment. On first launch, the launcher also runs setup automatically if its local tool-path record is missing. No administrator access, system-wide installation, PATH edits, or global Hermes/OpenCode configuration edits are performed.

The first setup needs internet access. Python 3.14.9 and uv 0.13.5 are pinned; if matching tools are not already available, setup downloads them into this project's `.tools` directory. The uv archive is checked against its pinned SHA-256. Python downloads use uv's managed distribution verification and are installed without registry registration or global executable links. Python package versions and hashes are recorded in `uv.lock`.

If detection cannot find an executable, provide its native `.exe` path:

```powershell
.\setup-hermes-opencode.ps1 -HermesPath 'C:\path\hermes.exe' -OpenCodePath 'C:\path\opencode-cli.exe'
```

Use `-CheckOnly` to inspect detection without installing anything. `-UvPath` and `-PythonPath` accept existing executables matching the pinned versions. Typical Desktop OpenCode installs are detected automatically; `.cmd` wrappers should be replaced with the underlying native OpenCode executable. Paths are saved only in ignored `.runtime/tools.json` and must be regenerated on each laptop.

## Choose a model before chatting

Run `hermes-opencode.bat`. It lists the free models advertised by the installed OpenCode CLI. Enter a number to choose one, press Enter for the saved choice, or type `q` to quit. Hermes opens after you choose.

During a chat, type `/model`, choose **opencode-agent**, then select another model. These `oc-` names are local aliases that keep requests routed through the agent gateway. If a reasoning-effort menu appears, choose **Keep current effort**: model generation settings remain owned by OpenCode.

To save a mid-chat choice for future launches:

```text
/model oc-glm-5.3-flash-free --provider custom:opencode-agent --global
```

A saved choice is preserved while it remains in the discovered catalog. Restart the stack to refresh the catalog. There is no automatic paid-model fallback. Free-model names, pricing, availability, quotas, and privacy terms are controlled by OpenCode and the model provider.

## Progress and tool execution

Replies read as Hermes by default. A greeting produces only the conversational answer:

```text
Hey! What can I help with?
```

Backend banners, step counters, checkpoint IDs, and completion markers stay out of ordinary chat. SSE comments keep the connection alive without adding assistant text. Set `display_agent_progress: true` in `opencode-gateway.yaml` to restore diagnostic progress. Worker status is available through Hermes's worker tools. Private reasoning is never rendered. Replies may arrive in blocks rather than token by token.

OpenCode owns coding tool execution and its normal permissions. The adapter does not pass auto-approval flags or inherit Hermes/provider keys into the OpenCode process. OpenCode uses its own normal login and configuration. The project-scope instruction is not an operating-system sandbox. Work only with projects and model providers you are authorized to use.

If OpenCode reports an account-wide free-usage limit, Hermes displays it and saves a task checkpoint. The adapter records cooldowns in ignored `.runtime/opencode-quota.json`; shared limits block other free-model choices too, including after restart. An explicitly model-specific rate limit can trigger bounded continuation on another available free model. No account rotation, paid fallback, or quota bypass is performed. The startup picker can still display catalog entries while access is temporarily exhausted.

Each turn uses a private OpenCode CLI run with conversation history as context. With task continuity enabled, the bridge reuses a recorded native session when it can identify the parent turn. It matches Hermes tool-call IDs, locally indexed assistant answers, or legacy checkpoint markers and verifies the preceding conversation; it never uses a global "last task" shared between chats. Quiet chat therefore retains continuity. The adapter supports text-only messages; outer `max_tokens` and reasoning-effort settings do not configure the native CLI. Ordinary failures do not trigger automatic coding retries; model-specific limits use the continuation rules below.

## Parallel OpenCode workers

The default configuration enables three concurrent workers with a queue of up to 24 active/queued tasks:

```yaml
parallel_workers: true
max_parallel_workers: 3
max_queued_workers: 24
display_agent_progress: false
```

The launcher registers a project-local stdio MCP server with Hermes. Its seven tools are `spawn_worker`, `list_workers`, `get_worker_status`, `send_worker_message`, `wait_for_workers`, `cancel_worker`, and `resume_worker`. Hermes supplies their schemas to the model through the existing validated relay. Only configured worker tools are accepted. The worker manager does not acquire the chat-turn lock: workers genuinely run concurrently while Hermes can continue answering or coordinating.

For example, ask Hermes:

```text
Use two OpenCode workers in parallel: one investigates the bug, the other reviews test coverage.
Wait for both, inspect their results, then explain the proposed fix.
```

Every worker runs the installed OpenCode CLI with its own native session. Hermes chooses an advertised free model through the optional `model` argument; omitted models use the gateway default. Workers have the same bounded model-specific fallback and shared provider cooldowns as the main conversation. Already running provider requests cannot be recalled when another worker discovers a shared limit; queued/new attempts check the shared cooldown before execution.

Workers in a clean Git repository receive distinct branches and worktrees. Commit or stash pending changes first. Projects without a Git checkout receive separate source snapshots, bounded to 128 MiB/10000 files, excluding runtime directories, dependencies, symlinks, junctions, and `.env` files. Workspaces prevent accidental edit collisions; they are not operating-system sandboxes. Dependencies may need installing inside a worker workspace. Changes remain there for the orchestrator to inspect, integrate, and test; there is no automatic merge or publication.

Follow-up messages are persisted and delivered at the next native turn boundary, never injected into an in-flight command. Results, progress, workspace paths, session IDs, and errors are available through status/wait tools. Waiting has a maximum of 30 seconds per tool call and does not cancel work. Workers cannot recursively spawn gateway workers. A configured worker control loop has a 100-turn budget; native turns retain `timeout_seconds`.

Worker records/checkpoints persist under `.runtime/workers`. Closing Hermes leaves its gateway running when workers are active; launching `hermes-opencode.bat` again reconnects only after authenticating the same project and settings. If the gateway itself stops, unfinished workers become `interrupted` on restart and require explicit `resume_worker` after inspecting partial edits. After a forced termination, resume refuses to overlap a surviving native process with the same process identity. Completed workers accept new follow-ups. No uncertain action is automatically replayed after a restart.

After tasks finish or are cancelled, stop a retained background gateway with:

```powershell
.\hermes-stack.ps1 -StopGateway
```

The worker API binds to loopback and requires a project-local capability token. The MCP subprocess reads that token from an ignored local file; credentials and runtime data are not exported. This does not change OpenCode's normal permissions or login. Windows must permit the installed Hermes executable before the complete Hermes interface can start.

## Model fallback and task continuation

Default settings in `opencode-gateway.yaml`:

```yaml
task_continuity: true
automatic_model_fallback: true
max_model_switches: 2
fallback_models: []
```

An empty fallback list uses the discovered free-model catalog order after the selected model. To control the order, supply explicit native IDs, for example `fallback_models: [opencode/glm-5.3-flash-free, opencode/big-pickle]`. Only discovered free models are eligible. Each model is considered at most once per turn, cooldowns are persisted, and the number of switches is capped.

The provider's documented error schema does not reliably identify quota scope. The bridge permits automatic fallback only when its error message explicitly starts with `model-specific rate limit`, `per-model rate limit`, or the corresponding `usage limit` wording. This is a conservative interpretation of provider text, not a standardized OpenCode scope flag. Generic `429` errors, missing scope, and `provider.free-limit` stop all automatic model switching. Many OpenCode free-model limits are shared, so this feature cannot guarantee uninterrupted free usage. See the [OpenCode API error schema](https://opencode.ai/docs/api) and [upstream discussion of shared free limits](https://github.com/anomalyco/opencode/issues/56211).

Each turn saves a private `.runtime/tasks/<id>.json` checkpoint containing the supplied conversation and tool schemas/results, public OpenCode progress, tool names/statuses, attempted models, and a native session ID when emitted. Raw native tool arguments/output and private reasoning are not copied into checkpoints. Native session storage remains owned by OpenCode. Usage logs contain per-model attempt counts; request-summary entries have empty usage to avoid double counting.

Before switching, the old process is closed. The next model receives the conversation, saved progress, tool states, and continuation instructions; `opencode run --session <id>` reuses native history when an ID was recorded. It is instructed to inspect existing files and continue unfinished work rather than repeat completed actions. This is not an exactly-once execution guarantee: interrupted commands may already have made changes. Uncertain external/destructive operations must not be replayed blindly. Checkpoint progress is bounded to 128 KiB of text and 1,000 tool states; truncation is recorded.

In the same Hermes chat, retain the checkpoint status lines and say what remains to do. The bridge can follow the checkpoint marker and preserve the native session, including when you change models using `/model`. A new conversation does not inherit another chat's task. If history was compacted or markers removed, automatic matching may be unavailable.

To resume an interrupted task after closing the stack:

```powershell
# List checkpoint IDs and states without showing conversation contents
.\.venv\Scripts\python.exe -m hermes_gateway.task_context

# Choose a model as usual, then give Hermes your continuation instruction
.\hermes-stack.ps1 -ResumeTask YOUR_32_CHARACTER_TASK_ID

# Or continue in one prompt
.\hermes-stack.ps1 -ResumeTask YOUR_32_CHARACTER_TASK_ID -Prompt 'Continue the unfinished checks; inspect existing changes first.'
```

Resume restores the saved conversation on the first request and preserves native session/progress context. The work directory must match. Completed turns and pending Hermes tool handoffs should be continued in their original Hermes conversation, where actual tool results are available. Shared cooldowns are still enforced when resuming. Checkpoints are local private runtime data, excluded from Git and release exports; they are not synced between laptops.

## Hermes memory and skills relay (experimental)

`hermes_tools: true` in `opencode-gateway.yaml` enables Hermes's `memory` and `skills` toolsets on the next launch. No separate model API key is required: the selected model stays behind OpenCode's native CLI. Coding still uses OpenCode tools.

The bridge sends Hermes's system context (including injected memories and skill instructions), conversation, available memory/skill function schemas, prior function requests, and function results to OpenCode. This information therefore reaches the selected OpenCode model/provider; keep its privacy terms in mind when choosing what to store.

When OpenCode needs a Hermes tool, it must return a specific JSON envelope. The bridge checks the request nonce, allowed function name, JSON argument schema, and tool-choice constraints, then translates it into standard OpenAI `tool_calls`, with streaming `finish_reason: tool_calls`. Hermes executes the function under its own normal storage and approval rules. Its result goes back to OpenCode on the next model turn. The bridge does not execute Hermes tools or write memory/skill files itself.

Allowed functions are `memory`, `skills_list`, `skill_view`, and `skill_manage`. Unsupported function schemas are rejected rather than silently routed. Coding tools are not exposed to Hermes. Native progress continues streaming, but model answer text is buffered until the turn ends so control JSON never appears as chat text. A handoff status appears before Hermes executes a requested tool.

Try asking Hermes:

```text
Remember that my preferred language for explanations is English. Use your memory tool.
Create a reusable skill for this project's test workflow using skill_manage.
List your skills, then load the relevant one before working.
```

Check Hermes's actual tool result: a request alone does not mean a save succeeded. Hermes may stage a skill write for approval. Memories and skills belong to this project's `.runtime/hermes-home`, persist across stack restarts, and are excluded from Git/exports. They are not synchronized with another laptop or an independently opened OpenCode session.

This is a custom prompted protocol, not OpenCode's native function-calling endpoint. Local tests cover tool handoff, results, streaming, argument validation, and rejected calls. The real Hermes CLI completed a `skills_list` round trip against a simulated OpenCode response, and the installed Hermes handlers saved a test memory and created a test skill in a separate project-local test home. A live OpenCode attempt returned a free-usage limit, so live model compliance and the complete interactive approval flow remain unverified. A model can ignore the protocol or produce malformed output. Invalid tool envelopes fail the turn without execution or gateway retry; the Hermes client may apply its own error retry policy. Setting `hermes_tools: false` disables Hermes tool relay; coding and task continuity retain their separate settings.

Interfaces checked against the [Hermes tools documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools), [OpenCode API documentation](https://opencode.ai/docs/api), and the installed CLIs/source. The JSON relay itself is implemented in this project, not an official integration.

## Commands

```powershell
# Interactive chat, with a model menu first
.\hermes-opencode.bat

# One prompt; uses the saved model and skips the startup menu
.\hermes-stack.ps1 -Prompt 'Reply exactly SETUP_OK. Do not use tools.'

# Adapter only; stop with Ctrl+C
.\hermes-stack.ps1 -GatewayOnly

# Setup plus a real Hermes/OpenCode inference smoke test
.\setup-hermes-opencode.ps1 -SmokeTest
```

Default traffic is `127.0.0.1:8787/v1`; no LM Studio server is started. Exit Hermes normally or press Ctrl+C to clean up owned processes. A force-closed terminal may prevent cleanup. The launcher refuses an occupied port and will not terminate an existing stack. The original LM Studio implementation remains available only with the explicit `-LMStudio` switch.

## Files and configuration

| File | Purpose |
| --- | --- |
| `setup-hermes-opencode.ps1` | Detects prerequisites and prepares project-local pinned tooling/dependencies. |
| `hermes-opencode.bat`, `hermes-stack.ps1` | Start the gateway, choose a model, and open Hermes. |
| `opencode-gateway.yaml` | Gateway port, default native model, working directory, and timeout. |
| `hermes_gateway/opencode_agent.py` | Native OpenCode execution and incremental progress/reply adapter. |
| `hermes_gateway/tool_relay.py` | Experimental translation between OpenCode text and Hermes memory/skill tool calls. |
| `hermes_gateway/model_router.py` | Bounded fallback for explicitly model-scoped limits. |
| `hermes_gateway/task_context.py` | Private task checkpoints, conversation linking, and resume listing. |
| `hermes_gateway/opencode_models.py` | Free-model discovery and local model aliases. |
| `.runtime/hermes-home/config.yaml` | Generated Hermes settings, model catalog, and saved selection. |
| `.runtime/tools.json` | Machine-specific executable locations; never published. |
| `.runtime/preflight.json` | Installed CLI versions and catalog detected on this laptop. |
| `.runtime/opencode-usage.jsonl` | Request metadata, actual native model, usage, and tool names. |
| `.runtime/tasks/` | Private conversation/progress checkpoints and tool-call links; never published. |
| `.runtime/opencode-gateway.log` | Gateway server errors and startup output. |
| `publish-manifest.json` | Explicit allowlist of files included in source exports. |

The default coding directory is the repository folder. Edit `workdir` in `opencode-gateway.yaml` to select another authorized project; relative paths are resolved against this repository. The gateway binds only to `127.0.0.1`. Its health endpoint checks local executable readiness, not provider availability; real requests check access. The default turn timeout is 600 seconds.

Generated data and dependencies stay inside `.runtime`, `.tools`, `.venv`, and `.uv-cache`. Setup detects Hermes' configuration schema and seeds the project-local config with it. Hermes uses a project-local home with Windows extended-length paths to support deep runtime directories without changing registry settings. Its first launch can prepare an isolated dependency environment and take several minutes. Before opening chat, the launcher explicitly waits for the Hermes background runtime using its supported `gateway ensure` command. `hermes_startup_timeout_seconds` in `opencode-gateway.yaml` controls that readiness budget (300 seconds by default), separately from the coding-turn timeout. Do not copy runtime directories from another laptop.

## Verification and reproducibility

The original integration was live-tested on Windows with Hermes 0.23.1 and OpenCode 2.2.6. DeepSeek and GLM free models replied through Hermes. An interactive Hermes model switch selected GLM, and its coding task created a Python file, ran it, and printed `43` while tool progress appeared in Hermes. Startup/model selection, SSE progress order, error handling, cancellation, and source-export boundaries have regression coverage.

The memory/skill relay and continuity update passes 50 local tests and Ruff. A real Hermes CLI run against a simulated OpenCode agent completed model fallback, a native `skills_list` handoff, and the final reply; the native session ID also carried across the tool result. This verifies local plumbing, not successful live free-model inference. The latest live attempt returned a provider free-usage limit and stopped without switching around it.

The parallel-worker update passes 65 local tests and Ruff. Coverage includes overlapping execution and concurrency limits, Git worktree and snapshot isolation, queued follow-ups on the same native session, cancellation, restart/resume, orphan-process detection, timeouts, shared quota handling, authenticated reconnection/shutdown, MCP transport, and quiet chat continuity. A live check using the installed Hermes MCP client started two real OpenCode workers concurrently: DeepSeek returned `WORKER_A_OK` and GLM returned `WORKER_B_OK`, each with a separate session. An earlier same-model check completed one worker while the other reached its configured timeout; provider latency and concurrency are not guaranteed.

A fresh-home Hermes CLI smoke check timed out before sending any requests to the gateway, so the complete current CLI approval/orchestration flow remains unverified. A separate live greeting also timed out at the provider/native-agent boundary; quiet response formatting is covered locally. These failures are not counted as successful live checks.

Setup checks required CLI flags and writes the installed version report to `.runtime/preflight.json`. `-SmokeTest` adds a real model request through Hermes and succeeds only if an exact `SETUP_OK` response line is received. A quota notice, missing login, or unavailable model fails that optional check clearly. During packaging, the clean-copy dependency/preflight checks passed, but the final live inference check encountered OpenCode's native `provider.free-limit` (429); no additional live calls were made after identifying it. Tooling and package dependencies are pinned, but remote models, their outputs, free availability, installed Hermes/OpenCode versions, and hardware are external dependencies: identical behavior can be checked, but identical generated responses on every laptop cannot be guaranteed.

To install development tools and run local checks:

```powershell
.\setup-hermes-opencode.ps1 -Dev
.\.venv\Scripts\ruff.exe check hermes_gateway tests
.\.venv\Scripts\python.exe -m pytest -q
```

Tests use mocked backends by default. The upstream Starlette test client may emit a deprecation warning about its HTTPX integration.

## Export a clean source package

```powershell
.\export-hermes-opencode.ps1
```

This creates `dist/Hermes-Opencode` and `dist/Hermes-Opencode.zip` using the explicit source manifest. It excludes runtime/session data, credentials, environments, downloaded binaries, caches, and unrelated workspace folders. Export requires an empty destination; choose a new `-OutputDirectory` for another export.

## Official references

- [Hermes model configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models)
- [OpenCode CLI automation and private servers](https://opencode.ai/docs/cli/)
- [OpenCode provider/model policy](https://opencode.ai/docs/console/models/)
- [uv locked dependency environments](https://docs.astral.sh/uv/concepts/projects/sync/)
- [uv managed Python versions](https://docs.astral.sh/uv/concepts/python-versions/)

The adapter runs the actual installed OpenCode application for personal coding tasks. It does not call OpenCode's hosted free inference endpoint directly, imitate client headers, or bypass quotas or terms.
