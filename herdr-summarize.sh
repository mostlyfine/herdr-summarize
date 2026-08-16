#!/usr/bin/env bash
set -euo pipefail

# Read stdin when piped (preserve payload for main)
RAW_HOOK_PAYLOAD=""
if ! [ -t 0 ]; then
  RAW_HOOK_PAYLOAD="$(cat)"
fi

rename_tab_if_session_start() {
  [ "${HERDR_ENV:-}" = "1" ] || return 0
  [ -n "${HERDR_PANE_ID:-}" ] || return 0
  command -v herdr >/dev/null 2>&1 || return 0
  command -v jq >/dev/null 2>&1 || return 0
  event="$(printf '%s' "$RAW_HOOK_PAYLOAD" | jq -r '.hook_event_name // empty' 2>/dev/null || true)"
  [ "$event" = "SessionStart" ] || return 0
  pane_info="$(herdr pane get "$HERDR_PANE_ID" 2>/dev/null)" || return 0
  tab_id="$(printf '%s' "$pane_info" | jq -r '.result.pane.tab_id // empty')"
  cwd="$(printf '%s' "$pane_info" | jq -r '.result.pane.cwd // empty')"
  [ -n "$tab_id" ] || return 0
  [ -n "$cwd" ] || return 0
  repo_root="$(git -C "$cwd" rev-parse --show-toplevel 2>/dev/null)" || repo_root=""
  label="${repo_root:-$cwd}"
  label="${label##*/}"
  herdr tab rename "$tab_id" "$label" >/dev/null 2>&1 || true
}

# Attempt rename early (non-blocking)
rename_tab_if_session_start || true

readonly USER_TURN_WINDOW=5
readonly USER_TURN_CHARS=600
readonly ASSISTANT_TURN_CHARS=160
readonly MAX_SUMMARY_CHARS=30

readonly -a INTERNAL_PROMPT_MARKERS=('<task-notification' '<command-name>' '<local-command-stdout>')

readonly JQ_TRUNCATE_DEF='def truncate($max): if (length > $max) then .[0:($max-1)] + "…" else . end;'

readonly SUMMARY_PROMPT='Below (on stdin) is the recent transcript of a coding session between a User and an AI Assistant.
Summarize what the USER is trying to accomplish as a short, concise title: a phrase, NOT a full
sentence — no trailing punctuation. Base it on the User'"'"'s intent, not the Assistant'"'"'s wording.
Match the User'"'"'s language.
Output ONLY the title: no quotes, no labels, no explanation.'

get_copilot_transcript_path() {
  local session_id="$1"
  local session_dir="$HOME/.copilot/session-state/$session_id"
  if [ -d "$session_dir" ] && [ -f "$session_dir/events.jsonl" ]; then
    printf '%s' "$session_dir/events.jsonl"
  fi
}

main() {
  if [ -n "${HERDR_SUMMARIZE_ACTIVE:-}" ]; then
    exit 0
  fi
  if [ -n "${LEARNING_SKILLS_OBSERVER:-}" ]; then
    exit 0
  fi
  if [ -z "${HERDR_PANE_ID:-}" ]; then
    exit 0
  fi
  if ! command -v herdr >/dev/null 2>&1; then
    exit 0
  fi

  local input tsv
  input="$RAW_HOOK_PAYLOAD"

  local event agent_id transcript_path
  tsv="$(printf '%s' "$input" | jq -r '[(.hook_event_name // ""), (.agent_id // "")] | @tsv' || true)"
  IFS=$'\t' read -r event agent_id <<<"$tsv"
  if [ -n "$agent_id" ]; then
    exit 0
  fi

  transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // .transcriptPath // empty')"

  # Check for Copilot CLI hook format
  if [ -z "$transcript_path" ]; then
    local session_id
    session_id="$(printf '%s' "$input" | jq -r '.sessionId // empty')"
    if [ -n "$session_id" ]; then
      transcript_path="$(get_copilot_transcript_path "$session_id")"
    fi
  fi

  case "$event" in
    UserPromptSubmit)
      handle_prompt_submit "$input"
      ;;
    Stop)
      if [ -n "$transcript_path" ]; then
        if is_codex_transcript "$transcript_path"; then
          handle_codex_stop "$input"
        elif is_copilot_transcript "$transcript_path"; then
          handle_copilot_stop "$input" "$transcript_path"
        else
          handle_stop "$input"
        fi
      fi
      ;;
    '')
      if [ -n "$transcript_path" ]; then
        if is_codex_transcript "$transcript_path"; then
          handle_codex_stop "$input"
        elif is_copilot_transcript "$transcript_path"; then
          handle_copilot_stop "$input" "$transcript_path"
        fi
      fi
      ;;
    *)
      exit 0
      ;;
  esac
}

is_codex_transcript() {
  local transcript_path="$1"
  [ -n "$transcript_path" ] && [ -f "$transcript_path" ] && \
    jq -e 'select(.type == "response_item") | .payload.type == "message"' \
      "$transcript_path" >/dev/null 2>&1
}

is_copilot_transcript() {
  local transcript_path="$1"
  [ -n "$transcript_path" ] && [ -f "$transcript_path" ] && \
    jq -e 'select(.type == "user.message" or .type == "assistant.message")' \
      "$transcript_path" >/dev/null 2>&1
}

truncate_summary() {
  local s="$1"
  printf '%s' "${s:0:$MAX_SUMMARY_CHARS}"
}

pane_state_file() {
  local sanitized
  sanitized="$(printf '%s' "$HERDR_PANE_ID" | tr -c 'A-Za-z0-9_.-' '_')"
  printf '%s/herdr-summarize-%s-last-prompt' "${HERDR_SUMMARIZE_STATE_DIR:-${TMPDIR:-/tmp}}" "$sanitized"
}

save_last_prompt() {
  local prompt="$1"
  local file
  file="$(pane_state_file)"
  {
    printf '%s' "$prompt" > "$file" && chmod 600 "$file"
  } 2>/dev/null || true
}

load_last_prompt() {
  local file
  file="$(pane_state_file)"
  cat "$file" 2>/dev/null || true
}

build_stop_input() {
  local last_prompt="$1"
  local content="$2"
  if [ -n "$last_prompt" ]; then
    printf "## User's most recent instruction\n%s\n\n## Conversation excerpt\n%s\n" "$last_prompt" "$content"
  else
    printf '%s\n' "$content"
  fi
}

readonly EXTRACT_TRANSCRIPT_TURNS_JQ='
def oneline:
  gsub("\\s+";" ") | sub("^ ";"") | sub(" $";"");

def to_turn:
  select((.isSidechain // false) | not) |
  select((.isMeta // false) | not) |
  .type as $t |
  .message.content as $c |
  if $t == "user" then
    if ($c|type) == "string" then
      ($c | oneline) as $text |
      if ($text == "") then empty
      elif ($markers | any(. as $m | $text | contains($m))) then empty
      else {role:"user", text:$text}
      end
    else empty end
  elif $t == "assistant" then
    if ($c|type) == "array" then
      ([$c[] | select(.type=="text") | .text] | join("") | oneline) as $text |
      if $text == "" then empty else {role:"assistant", text:$text} end
    else empty end
  elif $t == "response_item" and (.payload.type == "message") then
    .payload.role as $role |
    .payload.content as $content |
    if $role == "user" then
      ([$content[]? | select(.type == "input_text") | .text] | join("") | oneline) as $text |
      if $text == "" then empty else {role:"user", text:$text} end
    elif $role == "assistant" then
      ([$content[]? | select(.type == "output_text") | .text] | join("") | oneline) as $text |
      if $text == "" then empty else {role:"assistant", text:$text} end
    else empty end
  else empty end;

def window_turns($n):
  . as $turns
  | ([range(0;($turns|length)) | select($turns[.].role=="user")]) as $user_idxs
  | ($user_idxs[-$n:]) as $recent_user_idxs
  | (if ($recent_user_idxs|length) > 0 then $recent_user_idxs[0] else 0 end) as $start
  | [$turns[$start:][] | select(.role=="user")] as $users
  | ([$turns[$start:][] | select(.role=="assistant")]
     | if length>0 then .[-1] else null end) as $last_asst
  | $users + (if $last_asst then [$last_asst] else [] end);

[
  inputs
  | (try fromjson catch empty)
  | select(. != null)
  | to_turn
]
| window_turns($window)
| map(
    if .role == "user" then .text |= truncate($user_chars)
    else .text |= truncate($asst_chars) end
  )
| map(if .role=="user" then "User: " + .text else "Assistant: " + .text end)
| join("\n")
'

extract_transcript_turns() {
  local transcript_path="$1"
  local markers_json
  markers_json="$(printf '%s\n' "${INTERNAL_PROMPT_MARKERS[@]}" | jq -R . | jq -s .)"
  jq -R -n -r \
    --argjson markers "$markers_json" \
    --argjson window "$USER_TURN_WINDOW" \
    --argjson user_chars "$USER_TURN_CHARS" \
    --argjson asst_chars "$ASSISTANT_TURN_CHARS" \
    "$JQ_TRUNCATE_DEF $EXTRACT_TRANSCRIPT_TURNS_JQ" "$transcript_path" 2>/dev/null || true
}

readonly EXTRACT_COPILOT_TRANSCRIPT_TURNS_JQ='
def oneline:
  gsub("\\s+";" ") | sub("^ ";"") | sub(" $";"");

def to_turn:
  if .type == "user.message" then
    (.data.content | oneline) as $text |
    if ($text == "") then empty else {role:"user", text:$text} end
  elif .type == "assistant.message" then
    (.data.content | oneline) as $text |
    if ($text == "") then empty else {role:"assistant", text:$text} end
  else empty end;

def window_turns($n):
  . as $turns
  | ([range(0;($turns|length)) | select($turns[.].role=="user")]) as $user_idxs
  | ($user_idxs[-$n:]) as $recent_user_idxs
  | (if ($recent_user_idxs|length) > 0 then $recent_user_idxs[0] else 0 end) as $start
  | [$turns[$start:][] | select(.role=="user")] as $users
  | ([$turns[$start:][] | select(.role=="assistant")]
     | if length>0 then .[-1] else null end) as $last_asst
  | $users + (if $last_asst then [$last_asst] else [] end);

[
  inputs
  | (try fromjson catch empty)
  | select(. != null)
  | to_turn
]
| window_turns($window)
| map(
    if .role == "user" then .text |= truncate($user_chars)
    else .text |= truncate($asst_chars) end
  )
| map(if .role=="user" then "User: " + .text else "Assistant: " + .text end)
| join("\n")
'

extract_copilot_transcript_turns() {
  local transcript_path="$1"
  jq -R -n -r \
    --argjson window "$USER_TURN_WINDOW" \
    --argjson user_chars "$USER_TURN_CHARS" \
    --argjson asst_chars "$ASSISTANT_TURN_CHARS" \
    "$JQ_TRUNCATE_DEF $EXTRACT_COPILOT_TRANSCRIPT_TURNS_JQ" "$transcript_path" 2>/dev/null || true
}

extract_copilot_latest_prompt() {
  local transcript_path="$1"
  jq -R -n -r '
    [ inputs
      | (try fromjson catch empty)
      | select(.type == "user.message")
      | .data.content
      | select(. != "")
    ] | .[-1] // empty
  ' "$transcript_path" 2>/dev/null || true
}

readonly SANITIZE_CLAUDE_SUMMARY_JQ='
(split("\n") | map(gsub("^\\s+|\\s+$";"")) | map(select(. != "")) | (.[0] // "")) as $first
| ($first | gsub("^[\"'"'"'「『]|[\"'"'"'」』]$";""))
| gsub("^\\s+|\\s+$";"")
| truncate($max_chars)
'

sanitize_claude_summary() {
  local raw="$1"
  printf '%s' "$raw" | jq -R -s -r --argjson max_chars "$MAX_SUMMARY_CHARS" "$JQ_TRUNCATE_DEF $SANITIZE_CLAUDE_SUMMARY_JQ" 2>/dev/null || true
}

report_metadata() {
  local token="$1"
  local -a args=(
    pane report-metadata "$HERDR_PANE_ID"
    --source herdr-summarize
    --token "$token"
  )

  herdr "${args[@]}" >/dev/null 2>&1 || true
}

handle_prompt_submit() {
  local input="$1"
  local prompt
  prompt="$(printf '%s' "$input" | jq -r '.prompt // empty')"
  if [ -z "$prompt" ]; then
    return 0
  fi
  local marker
  for marker in "${INTERNAL_PROMPT_MARKERS[@]}"; do
    case "$prompt" in
      "$marker"*) return 0 ;;
    esac
  done
  local truncated
  truncated="$(truncate_summary "$prompt")"
  report_metadata "prompt=$truncated"
  save_last_prompt "$prompt"
}

handle_stop() {
  local input="$1"
  local transcript_path
  transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // empty')"
  if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    return 0
  fi

  local content
  content="$(extract_transcript_turns "$transcript_path")"
  if [ -z "$content" ]; then
    return 0
  fi

  report_metadata "summary=..."

  local last_prompt
  last_prompt="$(load_last_prompt)"

  local stop_input
  stop_input="$(build_stop_input "$last_prompt" "$content")"

  local summary
  summary="$(printf '%s' "$stop_input" | HERDR_SUMMARIZE_ACTIVE=1 claude -p "$SUMMARY_PROMPT" 2>/dev/null || true)"
  if [ -z "$summary" ]; then
    return 0
  fi

  local truncated
  truncated="$(sanitize_claude_summary "$summary")"
  report_metadata "summary=$truncated"
}

extract_codex_latest_prompt() {
  local transcript_path="$1"
  jq -R -n -r '
    [ inputs
      | (try fromjson catch empty)
      | select(.type == "response_item" and .payload.type == "message" and .payload.role == "user")
      | [.payload.content[]? | select(.type == "input_text") | .text] | join("")
      | select(. != "")
    ] | .[-1] // empty
  ' "$transcript_path" 2>/dev/null || true
}

handle_codex_stop() {
  local input="$1"
  local transcript_path
  transcript_path="$(printf '%s' "$input" | jq -r '.transcript_path // empty')"
  if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    return 0
  fi

  local prompt
  prompt="$(extract_codex_latest_prompt "$transcript_path")"
  if [ -n "$prompt" ]; then
    report_metadata "prompt=$(truncate_summary "$prompt")"
    save_last_prompt "$prompt"
  fi

  local content
  content="$(extract_transcript_turns "$transcript_path")"
  if [ -z "$content" ]; then
    return 0
  fi

  report_metadata "summary=..."

  local stop_input output_file summary truncated
  stop_input="$(build_stop_input "$prompt" "$content")"
  output_file="$(mktemp)"
  HERDR_SUMMARIZE_ACTIVE=1 codex exec --ephemeral --output-last-message "$output_file" \
    "$SUMMARY_PROMPT" >/dev/null 2>&1 || true
  summary="$(cat "$output_file" 2>/dev/null || true)"
  rm -f "$output_file"
  if [ -z "$summary" ]; then
    return 0
  fi

  truncated="$(sanitize_claude_summary "$summary")"
  report_metadata "summary=$truncated"
}

handle_copilot_stop() {
  local input="$1"
  local transcript_path="$2"
  
  if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
    return 0
  fi

  # Extract and save the latest prompt
  local prompt
  prompt="$(extract_copilot_latest_prompt "$transcript_path")"
  if [ -n "$prompt" ]; then
    report_metadata "prompt=$(truncate_summary "$prompt")"
    save_last_prompt "$prompt"
  fi

  local content
  content="$(extract_copilot_transcript_turns "$transcript_path")"
  if [ -z "$content" ]; then
    return 0
  fi

  report_metadata "summary=..."

  local last_prompt
  last_prompt="$(load_last_prompt)"

  local stop_input
  stop_input="$(build_stop_input "$last_prompt" "$content")"

  local summary
  summary="$(printf '%s' "$stop_input" | HERDR_SUMMARIZE_ACTIVE=1 copilot -p "$SUMMARY_PROMPT" 2>/dev/null || true)"
  if [ -z "$summary" ]; then
    return 0
  fi

  local truncated
  truncated="$(sanitize_claude_summary "$summary")"
  report_metadata "summary=$truncated"
}

main
