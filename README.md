# Nexus Recovery Peer

A Hermes plugin that requests **one bounded second-opinion call** to a separately configured provider when a tool loop makes no progress or a delegated child explicitly fails. It never substitutes a model for the current leader, reruns a tool, or treats advice as a verified fix. Provider independence depends on the operator's mapping and Hermes routing; it is not guaranteed.

## Behavior

- After three consecutive identical failed tool results, or three identical successful *read-only* results, ask the configured peer model for at most two different next steps and a verification check.
- A failed `delegate_task` result, or a completed child summary that explicitly begins with an inability statement, triggers the same review immediately. The child's original status and result remain intact.
- At most one `ctx.llm` invocation per user turn within a session (or once per session when turn IDs are absent), requesting an 18-second timeout and 220 output tokens. Host retries/fallbacks may create multiple provider attempts; this is not a wall-clock deadline. If review fails, the original result passes through unchanged. Hermes' own loop guardrails remain independent.
- This detects only observable repetition and explicit failures, **not arbitrary incorrect answers**.

## Installation

Install from this source repository at a reviewed full commit SHA (replace `FULL_COMMIT_SHA`), initially disabled:

```bash
hermes plugins install rezaaibox-ui/nexus-recovery-peer --ref FULL_COMMIT_SHA --no-enable
```

This source installation is not a claim of catalog admission. Review the disclosures and configure/consent below before enabling.

## Configure routes (opt-in)

The `routes` setting defaults to `[]`, so a new installation sends nothing to an external model until the operator configures a route. Set `plugins.entries.nexus-recovery-peer.settings.routes` to an **ordered list** of records with:

| Key | Meaning |
| --- | --- |
| `model_contains` | Case-insensitive substring of the current model ID (first match wins). |
| `origin_provider` | Current provider ID associated with that model pattern; supplied by the operator because the hook reports the model but not the provider. |
| `peer_provider` | Independent provider ID for the review. Must differ from `origin_provider`. |
| `peer_model` | Model ID at the peer provider. |

Example with placeholders (replace every placeholder with an installed and authenticated provider/model):

```json
[{"model_contains":"<leader-model-fragment>","origin_provider":"<leader-provider>","peer_provider":"<independent-provider>","peer_model":"<independent-model>"}]
```

Enable the plugin with `hermes plugins enable nexus-recovery-peer --no-allow-tool-override` and review the declared `llm.provider_override` and `llm.model_override` consent requests. With either capability denied, no peer request is made. Set routes with `hermes config set plugins.entries.nexus-recovery-peer.settings.routes '<JSON list>'`; Desktop exposes the same setting. Hermes also supports per-plugin `llm.allowed_providers` and `llm.allowed_models` allowlists: restrict these to the intended peers rather than using wildcards. These gate requested overrides, not a guarantee about downstream host fallbacks. The plugin never reads credentials.

The hook supplies a model, not its actual provider; `origin_provider` is an operator-supplied assertion. Substring matching can include unintended models: use distinctive model IDs and review every route. Hermes may retry or fall back after dispatch. Checking returned attribution only controls whether advice is appended; it cannot undo data already sent or prove which provider handled every attempt. Do not enable a route if those constraints violate your data-sharing policy.

## Data and security disclosure

For a recovery call the plugin passes a redacted, truncated recent user task (up to 1,000 characters), the tool name, and a redacted/truncated failure or no-progress summary (up to 350 characters) **through Hermes' `ctx.llm` API**. Error text, or an error result when no separate error message exists, may contain file paths and file content. Explicitly blocked delegated-child summaries are excerpted even when the child reported `completed`; ordinary successful read-only output bodies are not sent. The plugin sees tool arguments/results in memory for comparison, hashes them, and does not persist them. JSON results may be reserialized with advice added; semantic original fields remain, but byte formatting/order is not preserved.

Redaction covers common token/password forms but is **best-effort, not a security boundary**. Do not configure a peer provider for conversations whose task/error text must not leave the primary provider. The selected provider's own retention and privacy terms apply. There are no direct network calls, shell commands, stored credentials, telemetry, self-updaters, or background processes in this plugin.

## Test and validate

From the plugin directory:

```bash
python -m unittest discover -s tests -p test_nexus_recovery_peer.py -v
hermes plugins validate . --install-deps --json
hermes plugins doctor nexus-recovery-peer
```

Tests use unittest (not pytest), synthetic credential values and mocked provider calls. Run with Hermes' Python environment, which supplies PyYAML for manifest checks; the runtime plugin itself uses only the standard library and Hermes APIs. Catalog admission is a separate maintainer-reviewed PR, not implied by local validation.
