import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_shell_hook import MOCK, MODELS


SCRIPT = Path(__file__).resolve().parents[1] / "herdr-summarize.py"
RECORDS = {
    "claude": [
        {"type": "user", "message": {"content": "要約を日本語で表示して"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "実装を確認しました"}]}},
    ],
    "codex": [
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "要約を日本語で表示して"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "実装を確認しました"}]}},
    ],
    "copilot": [
        {"type": "user.message", "data": {"content": "要約を日本語で表示して"}},
        {"type": "assistant.message", "data": {"content": "実装を確認しました"}},
    ],
}


class PythonHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ["herdr", *MODELS]:
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + MOCK)
            executable.chmod(0o755)
        self.log = self.root / "calls.jsonl"
        self.env = dict(os.environ)
        for key in ["HERDR_SUMMARIZE_ACTIVE", "LEARNING_SKILLS_OBSERVER", "HERDR_ENV"]:
            self.env.pop(key, None)
        self.env.update(
            PATH=str(self.bin), HERDR_PANE_ID="test-pane",
            HERDR_SUMMARIZE_STATE_DIR=str(self.root), MOCK_LOG=str(self.log),
            COPILOT_HOME=str(self.root / "copilot"),
        )

    def payload(self, source):
        if source == "copilot":
            path = Path(self.env["COPILOT_HOME"]) / "session-state" / "test-session" / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"sessionId": "test-session"}
        else:
            path = self.root / f"{source}.jsonl"
            payload = {"transcript_path": str(path)}
            if source == "claude":
                payload["hook_event_name"] = "Stop"
        path.write_text("".join(json.dumps(record) + "\n" for record in RECORDS[source]))
        return payload

    def run_hook(self, payload, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], input=json.dumps(payload),
            text=True, capture_output=True, env=self.env, cwd=self.root, timeout=10,
        )

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_all_source_and_summary_combinations_and_default(self):
        for source in RECORDS:
            for selected in [None, *MODELS]:
                with self.subTest(source=source, selected=selected):
                    self.log.write_text("")
                    args = [] if selected is None else ["--agent", selected]
                    result = self.run_hook(self.payload(source), *args)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    calls = self.calls()
                    agents = [call for call in calls if call["name"] != "herdr"]
                    self.assertEqual(len(agents), 1)
                    agent = agents[0]
                    expected = selected or "copilot"
                    self.assertEqual(agent["name"], expected)
                    self.assertEqual(agent["args"][agent["args"].index("--model") + 1], MODELS[expected])
                    self.assertEqual(agent["active"], "1")
                    prompt = agent["stdin"] if expected == "codex" else agent["args"][agent["args"].index("-p") + 1]
                    self.assertIn("Output ONLY the title", prompt)
                    self.assertIn("User: 要約を日本語で表示して", prompt)
                    self.assertIn("Assistant: 実装を確認しました", prompt)
                    tokens = [call["args"][-1] for call in calls if call["name"] == "herdr"]
                    self.assertEqual(tokens, ["prompt=要約を日本語で表示して", "summary=...", "summary=要約エージェントの切り替え"])
                    self.assertEqual(list(self.root.glob("herdr-summarize-codex-*.txt")), [])

    def test_source_cli_is_not_required(self):
        (self.bin / "codex").unlink()
        result = self.run_hook(self.payload("codex"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["args"][-1], "summary=要約エージェントの切り替え")

    def test_missing_summary_cli_is_skipped(self):
        (self.bin / "copilot").unlink()
        result = self.run_hook(self.payload("codex"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_transcript_path_alias(self):
        payload = self.payload("codex")
        payload["transcriptPath"] = payload.pop("transcript_path")
        result = self.run_hook(payload, "--agent", "copilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["args"][-1], "summary=要約エージェントの切り替え")

    def test_invalid_arguments(self):
        for args in [["--agent"], ["--agent", "invalid"], ["--unknown"]]:
            with self.subTest(args=args):
                result = self.run_hook({}, *args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_help(self):
        result = self.run_hook({}, "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("default: copilot", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_failed_or_empty_summary_is_not_reported(self):
        for selected in MODELS:
            for code, response in [(1, "Incomplete response"), (0, " \n\n")]:
                with self.subTest(selected=selected, code=code):
                    self.log.write_text("")
                    self.env.update(MOCK_EXIT=str(code), MOCK_RESPONSE=response)
                    result = self.run_hook(self.payload("copilot"), "--agent", selected)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    tokens = [call["args"][-1] for call in self.calls() if call["name"] == "herdr"]
                    self.assertEqual(tokens, ["prompt=要約を日本語で表示して", "summary=..."])
                    self.assertEqual(list(self.root.glob("herdr-summarize-codex-*.txt")), [])

    def test_recursion_guard(self):
        self.env["HERDR_SUMMARIZE_ACTIVE"] = "1"
        result = self.run_hook(self.payload("codex"), "--agent", "copilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_prompt_event_does_not_generate_summary(self):
        result = self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt": "次の作業"}, "--agent", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call["name"] for call in self.calls()], ["herdr"])
        self.assertEqual(self.calls()[0]["args"][-1], "prompt=次の作業")


if __name__ == "__main__":
    unittest.main()
