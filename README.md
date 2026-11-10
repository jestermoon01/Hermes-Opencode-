# Hermes + OpenCode for Windows

Chat in Hermes, choose an OpenCode model, and watch the coding agent's progress in the same terminal. OpenCode runs its own native file-editing and command tools. LM Studio is not required.

```text
You -> Hermes -> localhost gateway -> installed OpenCode agent -> selected model
You <- live status, tool progress, and replies <- OpenCode
```

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

Progress appears inside Hermes as native events arrive:

```text
[OpenCode] Model: opencode/glm-5.3-flash-free. Waiting for coding agent.
[OpenCode] Working - step 1.
[OpenCode] Completed: write.
[OpenCode] Working - step 2.
[OpenCode] Completed: shell.
Created the file and ran it. Output: 43.
[OpenCode] Finished.
```

Waiting updates appear every ten seconds while the native CLI is quiet. Native tool states are shown honestly; private reasoning, tool arguments, and raw terminal output are not rendered as progress. Text may arrive in blocks because this follows OpenCode's native CLI events, rather than promising token-by-token output.

OpenCode owns tool execution and its normal permissions. The adapter does not pass auto-approval flags or inherit Hermes/provider keys into the OpenCode process. OpenCode uses its own normal login and configuration. The project-scope instruction is not an operating-system sandbox. Work only with projects and model providers you are authorized to use.

If OpenCode reports a free-usage limit, Hermes displays the limit and reset delay instead of silently retrying. The adapter records the provider's reset time in ignored `.runtime/opencode-quota.json`; an account-wide free limit blocks other free-model choices too, including after restart. Ordinary model-specific rate limits apply to that model. No account rotation, paid fallback, or quota bypass is performed. The startup picker can still display catalog entries while access is temporarily exhausted.

Each turn starts a private OpenCode CLI run with the user/assistant conversation as context. Progress lines are removed from replayed assistant history. Native tool-event history is not replayed between turns. The adapter supports text-only messages; outer `max_tokens` and reasoning-effort settings do not configure the native CLI. An agent turn is never automatically retried by this gateway because partial edits may already exist. Other clients can implement their own retries.

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
| `hermes_gateway/opencode_models.py` | Free-model discovery and local model aliases. |
| `.runtime/hermes-home/config.yaml` | Generated Hermes settings, model catalog, and saved selection. |
| `.runtime/tools.json` | Machine-specific executable locations; never published. |
| `.runtime/preflight.json` | Installed CLI versions and catalog detected on this laptop. |
| `.runtime/opencode-usage.jsonl` | Request metadata, actual native model, usage, and tool names. |
| `.runtime/opencode-gateway.log` | Gateway server errors and startup output. |
| `publish-manifest.json` | Explicit allowlist of files included in source exports. |

The default coding directory is the repository folder. Edit `workdir` in `opencode-gateway.yaml` to select another authorized project; relative paths are resolved against this repository. The gateway binds only to `127.0.0.1`. Its health endpoint checks local executable readiness, not provider availability; real requests check access. The default turn timeout is 600 seconds.

Generated data and dependencies stay inside `.runtime`, `.tools`, `.venv`, and `.uv-cache`. Setup detects Hermes' configuration schema and seeds the project-local config with it. Hermes uses a project-local home with Windows extended-length paths to support deep runtime directories without changing registry settings. Its first launch can prepare an isolated dependency environment and take several minutes. Before opening chat, the launcher explicitly waits for the Hermes background runtime using its supported `gateway ensure` command. `hermes_startup_timeout_seconds` in `opencode-gateway.yaml` controls that readiness budget (300 seconds by default), separately from the coding-turn timeout. Do not copy runtime directories from another laptop.

## Verification and reproducibility

The original integration was live-tested on Windows with Hermes 0.23.1 and OpenCode 2.2.6. DeepSeek and GLM free models replied through Hermes. An interactive Hermes model switch selected GLM, and its coding task created a Python file, ran it, and printed `43` while tool progress appeared in Hermes. Startup/model selection, SSE progress order, error handling, cancellation, and source-export boundaries have regression coverage.

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
