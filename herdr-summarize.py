#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

USER_TURN_WINDOW = 5
USER_TURN_CHARS = 600
ASSISTANT_TURN_CHARS = 160
MAX_SUMMARY_CHARS = 30
CLAUDE_INTERNAL_PROMPT_MARKERS = (
    "<task-notification",
    "<command-name>",
    "<local-command-stdout>",
)
SUMMARY_PROMPT = (
    "Below (on stdin) is the recent transcript of a coding session between a User and an AI Assistant.\n"
    "Summarize what the USER is trying to accomplish as a short, concise title: a phrase, NOT a full\n"
    "sentence — no trailing punctuation. Base it on the User's intent, not the Assistant's wording.\n"
    "Match the User's language.\n"
    "Output ONLY the title: no quotes, no labels, no explanation."
)


def read_jsonl(path: Path) -> list[dict]:
    try:
        with path.open(encoding="utf-8") as handle:
            records = []
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
            return records
    except (OSError, UnicodeError):
        return []


def detect_agent_from_records(records: list[dict]) -> str | None:
    for record in records:
        record_type = record.get("type")
        if record_type in {"user", "assistant"}:
            return "claude"
        if (
            record_type == "response_item"
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("type") == "message"
        ):
            return "codex"
        if record_type in {"user.message", "assistant.message"}:
            return "copilot"
    return None


def detect_agent(path: Path) -> str | None:
    return detect_agent_from_records(read_jsonl(path))


def oneline(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_quiet(
    arguments: list[str],
    input_text: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    try:
        completed = subprocess.run(
            arguments,
            input=input_text,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode == 0:
        return completed.stdout
    return ""


def summarize_state_dir() -> Path:
    configured = os.environ.get("HERDR_SUMMARIZE_STATE_DIR")
    if configured:
        return Path(configured)
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def pane_state_path() -> Path:
    pane_id = os.environ.get("HERDR_PANE_ID", "")
    sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", pane_id)
    return summarize_state_dir() / f"herdr-summarize-{sanitized}-last-prompt"


def save_last_prompt(prompt: str) -> None:
    path = pane_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        return


def load_last_prompt() -> str:
    try:
        return pane_state_path().read_text(encoding="utf-8")
    except OSError:
        return ""


def git_root(cwd: Path) -> Path | None:
    output = run_quiet(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if not output:
        return None

    root = output.strip()
    if not root:
        return None
    return Path(root)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def truncate_summary(text: str) -> str:
    return text[:MAX_SUMMARY_CHARS]


def _join_blocks(blocks: object, block_type: str) -> str:
    if not isinstance(blocks, list):
        return ""

    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != block_type:
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)

    return oneline("".join(parts))


def _extract_claude_turn(record: dict) -> dict[str, str] | None:
    if record.get("isSidechain") or record.get("isMeta"):
        return None

    message = record.get("message")
    if not isinstance(message, dict):
        return None

    record_type = record.get("type")
    content = message.get("content")

    if record_type == "user" and isinstance(content, str):
        text = oneline(content)
        if text and not is_internal_prompt(text):
            return {"role": "user", "text": text}
    if record_type == "assistant":
        text = _join_blocks(content, "text")
        if text:
            return {"role": "assistant", "text": text}
    return None


def _extract_codex_turn(record: dict) -> dict[str, str] | None:
    if record.get("type") != "response_item":
        return None

    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None

    role = payload.get("role")
    content = payload.get("content")
    if role == "user":
        text = _join_blocks(content, "input_text")
        if text:
            return {"role": "user", "text": text}
    if role == "assistant":
        text = _join_blocks(content, "output_text")
        if text:
            return {"role": "assistant", "text": text}
    return None


def _extract_copilot_turn(record: dict) -> dict[str, str] | None:
    data = record.get("data")
    if not isinstance(data, dict):
        return None

    content = data.get("content")
    if not isinstance(content, str):
        return None

    text = oneline(content)
    if not text:
        return None

    record_type = record.get("type")
    if record_type == "user.message":
        return {"role": "user", "text": text}
    if record_type == "assistant.message":
        return {"role": "assistant", "text": text}
    return None


def extract_turns(agent: str, records: list[dict]) -> list[dict[str, str]]:
    extractors = {
        "claude": _extract_claude_turn,
        "codex": _extract_codex_turn,
        "copilot": _extract_copilot_turn,
    }
    extractor = extractors.get(agent)
    if extractor is None:
        return []

    turns = []
    for record in records:
        if not isinstance(record, dict):
            continue
        turn = extractor(record)
        if turn is not None:
            turns.append(turn)
    return turns


def extract_latest_prompt(turns: list[dict[str, str]]) -> str:
    prompts = [turn["text"] for turn in turns if turn["role"] == "user"]
    return prompts[-1] if prompts else ""


def extract_recent_turns(turns: list[dict[str, str]]) -> str:
    user_indexes = [index for index, turn in enumerate(turns) if turn["role"] == "user"]
    start = user_indexes[-USER_TURN_WINDOW] if len(user_indexes) >= USER_TURN_WINDOW else 0
    recent_turns = turns[start:]

    rendered_turns = [turn for turn in recent_turns if turn["role"] == "user"]
    assistants = [turn for turn in recent_turns if turn["role"] == "assistant"]
    if assistants:
        rendered_turns.append(assistants[-1])

    lines = []
    for turn in rendered_turns:
        if turn["role"] == "user":
            lines.append(f"User: {_truncate(turn['text'], USER_TURN_CHARS)}")
        else:
            lines.append(f"Assistant: {_truncate(turn['text'], ASSISTANT_TURN_CHARS)}")
    return "\n".join(lines)


def sanitize_summary(summary: str) -> str:
    first_line = ""
    for line in summary.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return ""

    cleaned = first_line.strip("\"'「『」』")
    cleaned = oneline(cleaned)
    if not cleaned:
        return ""
    return truncate_summary(cleaned)


def get_copilot_transcript_path(session_id: str) -> Path | None:
    copilot_home = os.environ.get("COPILOT_HOME")
    base_dir = Path(copilot_home) if copilot_home else Path.home() / ".copilot"
    path = base_dir / "session-state" / session_id / "events.jsonl"
    if path.is_file():
        return path
    return None


def _explicit_transcript_path_value(payload: dict) -> str | None:
    for key in ("transcript_path", "transcriptPath"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_transcript_path(payload: dict) -> Path | None:
    explicit = _explicit_transcript_path_value(payload)
    if explicit is not None:
        return Path(explicit)

    session_id = payload.get("sessionId")
    if isinstance(session_id, str) and session_id:
        return get_copilot_transcript_path(session_id)
    return None


def report_metadata(token: str) -> None:
    pane_id = os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        return
    run_quiet(
        [
            "herdr",
            "pane",
            "report-metadata",
            pane_id,
            "--source",
            "herdr-summarize",
            "--token",
            token,
        ]
    )


def build_stop_input(last_prompt: str, content: str) -> str:
    if last_prompt:
        return (
            f"## User's most recent instruction\n{last_prompt}\n\n"
            f"## Conversation excerpt\n{content}\n"
        )
    return f"{content}\n"


def summary_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["HERDR_SUMMARIZE_ACTIVE"] = "1"
    return environment


def summarize_with_streaming_agent(agent: str, stop_input: str) -> str:
    return run_quiet(
        [agent, "-p", SUMMARY_PROMPT],
        input_text=stop_input,
        environment=summary_environment(),
    )


def summarize_with_codex(stop_input: str) -> str:
    state_dir = summarize_state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""

    output_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="herdr-summarize-codex-",
            suffix=".txt",
            dir=state_dir,
            delete=False,
        ) as handle:
            output_path = Path(handle.name)

        completed = subprocess.run(
            ["codex", "exec", "--ephemeral", "--output-last-message", str(output_path), SUMMARY_PROMPT],
            input=stop_input,
            env=summary_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or output_path is None:
            return ""
        return output_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    finally:
        if output_path is not None:
            try:
                output_path.unlink()
            except OSError:
                pass


def summarize_with_agent(agent: str, stop_input: str) -> str:
    summarizers = {
        "codex": lambda: summarize_with_codex(stop_input),
        "claude": lambda: summarize_with_streaming_agent(agent, stop_input),
        "copilot": lambda: summarize_with_streaming_agent(agent, stop_input),
    }
    summarizer = summarizers.get(agent)
    return summarizer() if summarizer is not None else ""


def is_internal_prompt(prompt: str) -> bool:
    stripped = prompt.lstrip()
    return any(stripped.startswith(marker) for marker in CLAUDE_INTERNAL_PROMPT_MARKERS)


def record_prompt(prompt: str) -> None:
    report_metadata(f"prompt={truncate_summary(prompt)}")
    save_last_prompt(prompt)


def handle_prompt_submit(payload: dict) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt or is_internal_prompt(prompt):
        return

    record_prompt(prompt)


def handle_stop_event(records: list[dict], agent: str | None) -> None:
    if agent is None or not command_exists(agent):
        return

    turns = extract_turns(agent, records)

    prompt = extract_latest_prompt(turns)
    if prompt:
        record_prompt(prompt)

    content = extract_recent_turns(turns)
    if not content:
        return

    report_metadata("summary=...")
    stop_input = build_stop_input(load_last_prompt(), content)
    summary = summarize_with_agent(agent, stop_input)
    sanitized = sanitize_summary(summary)
    if not sanitized:
        return

    report_metadata(f"summary={sanitized}")


def rename_tab(pane_id: str, event: str) -> None:
    if os.environ.get("HERDR_ENV") != "1":
        return

    payload = run_quiet(["herdr", "pane", "get", pane_id])
    if not payload:
        return

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return

    if not isinstance(data, dict):
        return

    result = data.get("result")
    if not isinstance(result, dict):
        return

    pane = result.get("pane")
    if not isinstance(pane, dict):
        return

    tab_id = pane.get("tab_id")
    cwd = pane.get("cwd")
    if not isinstance(tab_id, str) or not tab_id or not isinstance(cwd, str) or not cwd:
        return

    cwd_path = Path(cwd)
    root = git_root(cwd_path)
    label = root.name if root is not None else cwd_path.name
    if not label:
        return

    run_quiet(["herdr", "tab", "rename", tab_id, label])


def handle_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        return
    if os.environ.get("HERDR_SUMMARIZE_ACTIVE"):
        return
    if os.environ.get("LEARNING_SKILLS_OBSERVER"):
        return
    pane_id = os.environ.get("HERDR_PANE_ID")
    if not pane_id:
        return
    if not command_exists("herdr"):
        return

    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        event = ""

    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        return

    rename_tab(pane_id, event)

    transcript_path = resolve_transcript_path(payload)
    has_explicit_transcript_path = _explicit_transcript_path_value(payload) is not None

    if event in {"UserPromptSubmit", "userPromptSubmit"} or (
        event == "" and not has_explicit_transcript_path and isinstance(payload.get("prompt"), str)
    ):
        handle_prompt_submit(payload)
        return
    if event not in {"", "Stop"}:
        return

    if transcript_path is None or not transcript_path.is_file():
        return

    records = read_jsonl(transcript_path)
    agent = detect_agent_from_records(records)
    if event == "" and agent not in {"codex", "copilot"}:
        return

    handle_stop_event(records, agent)


def load_payload_from_stdin() -> dict:
    if sys.stdin.isatty():
        return {}

    raw = sys.stdin.read()
    if not raw:
        return {}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    if isinstance(payload, dict):
        return payload
    return {}


def main() -> None:
    handle_payload(load_payload_from_stdin())


if __name__ == "__main__":
    main()
