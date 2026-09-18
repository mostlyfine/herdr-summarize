# herdr-summarize

A hook script that displays Claude Code's most recent user prompt and a work summary in herdr's sidebar.

## Prerequisites

- [herdr](https://herdr.dev/) must be installed
- `jq` must be installed
- The CLI selected for summarization (`claude`, `codex`, or `copilot`) must be installed and authenticated

## Setup

### 1. Register the hooks

Add the following to `~/.claude/settings.json` (replace with the absolute path to `herdr-summarize.sh`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "/path/to/herdr-summarize.sh" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "/path/to/herdr-summarize.sh" }
        ]
      }
    ]
  }
}
```

### Choose the summary agent

Add `--agent` to the `Stop` hook command to select the CLI that generates the summary:

```bash
/path/to/herdr-summarize.sh --agent claude
/path/to/herdr-summarize.sh --agent codex
/path/to/herdr-summarize.sh --agent copilot
```

Omitting `--agent` selects `copilot`. This option changes only summary generation; the hook input and transcript remain in Claude Code format.

| Agent | Model |
| --- | --- |
| `claude` | `claude-haiku-4-5` |
| `codex` | `gpt-5.6-luna` |
| `copilot` (default) | `gpt-5.6-luna` |

Models are selected for their lowest standard input/output token rates among current CLI models, checked on September 18, 2026. See [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Codex models](https://learn.chatgpt.com/docs/models), [Codex pricing](https://learn.chatgpt.com/docs/pricing), and [Copilot pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing). Copilot uses current AI-credit pricing as the baseline; legacy request-based subscriptions can have different cheapest options. Actual usage costs also depend on caching and token counts. Model access depends on your plan and organization settings.

### Python hook for Claude, Codex, and Copilot transcripts

`herdr-summarize.py` detects the source agent automatically from the transcript records. Use `--agent` to independently select the CLI that generates the summary; it defaults to `copilot` and uses the same models as the shell hook above.

```bash
python3 /path/to/herdr-summarize.py --agent claude
python3 /path/to/herdr-summarize.py --agent codex
python3 /path/to/herdr-summarize.py --agent copilot
```

The Python hook reads a JSON payload from stdin. Supply the transcript path in `transcript_path` or `transcriptPath`. For Copilot, a payload containing `sessionId` can instead locate `$COPILOT_HOME/session-state/<sessionId>/events.jsonl` (`~/.copilot` by default). Codex requires an explicit transcript path. Source detection does not search for unrelated running sessions.

For example, to summarize a Codex transcript using Claude:

```bash
printf '%s\n' '{"transcript_path":"/path/to/codex-transcript.jsonl"}' | python3 /path/to/herdr-summarize.py --agent claude
```

For Claude Code hooks, replace the shell command in the setup above with the Python command. Python 3.10 or later is required. Only the selected summary CLI needs to be installed; the source agent is identified from its transcript.

### 2. Configure the herdr sidebar display

Add `rows` under `[ui.sidebar.agents]` in `~/.config/herdr/config.toml`.

```toml
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace", "agent"],
  [{ token = "$summary", fg = "#a6e3a1" }],
  [{ token = "$prompt" }],
]
```

To change the colors, edit the hex color code in `fg` directly.

### 3. Apply the configuration

```bash
herdr server reload-config
```

## Tests

Run the shell and Python hook tests with Python 3.10 or later and `jq` installed:

```bash
python3 -m unittest discover -s tests -v
```

The tests substitute local mocks for the agent CLIs and `herdr`; they do not make model requests.

## License

- MIT
