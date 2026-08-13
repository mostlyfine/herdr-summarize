# herdr-summarize

A hook script that displays Claude Code's most recent user prompt and a work summary in herdr's sidebar.

## Prerequisites

- [herdr](https://herdr.dev/) must be installed
- `jq` must be installed
- You must be logged in to the Claude Code CLI (`claude -p` is used without an additional API key)

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

## License

- MIT
