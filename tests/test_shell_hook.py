import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "herdr-summarize.sh"
MODELS = {"claude": "claude-haiku-4-5", "codex": "gpt-5.6-luna", "copilot": "gpt-5.6-luna"}
MOCK = '''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
stdin = sys.stdin.read() if name == "codex" else ""
with open(os.environ["MOCK_LOG"], "a") as log:
    log.write(json.dumps({"name": name, "args": args, "stdin": stdin,
                          "active": os.environ.get("HERDR_SUMMARIZE_ACTIVE")}) + "\\n")
if name == "herdr":
    sys.exit(0)
response = os.environ.get("MOCK_RESPONSE", '\\n  「要約エージェントの切り替え」  \\n余分な説明\\n')
if name == "codex":
    Path(args[args.index("--output-last-message") + 1]).write_text(response)
    print("Progress output must not become the title")
else:
    print(response)
sys.exit(int(os.environ.get("MOCK_EXIT", "0")))
'''


class ShellHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ["herdr", *MODELS]:
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + MOCK)
            executable.chmod(0o755)
        self.log = self.root / "calls.jsonl"
        self.transcript = self.root / "transcript.jsonl"
        self.transcript.write_text(json.dumps({
            "type": "user", "message": {"content": "要約エージェントを切り替えたい"}
        }) + "\n")
        self.env = dict(os.environ)
        for key in ["HERDR_SUMMARIZE_ACTIVE", "LEARNING_SKILLS_OBSERVER"]:
            self.env.pop(key, None)
        self.env.update(
            PATH=str(self.bin) + os.pathsep + self.env["PATH"],
            HERDR_PANE_ID="test-pane",
            HERDR_SUMMARIZE_STATE_DIR=str(self.root),
            TMPDIR=str(self.root),
            MOCK_LOG=str(self.log),
        )

    def run_hook(self, *args, payload=None):
        if payload is None:
            payload = {"hook_event_name": "Stop", "transcript_path": str(self.transcript)}
        return subprocess.run(
            ["/bin/bash", str(SCRIPT), *args], input=json.dumps(payload),
            text=True, capture_output=True, env=self.env, cwd=self.root, timeout=10,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_agents_receive_model_context_and_report_clean_title(self):
        for selection in [None, *MODELS]:
            with self.subTest(agent=selection):
                self.log.write_text("")
                args = [] if selection is None else ["--agent", selection]
                submitted = self.run_hook(*args, payload={
                    "hook_event_name": "UserPromptSubmit", "prompt": "直前の指示を優先して"
                })
                self.assertEqual(submitted.returncode, 0, submitted.stderr)
                result = self.run_hook(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                calls = self.calls()
                agents = [call for call in calls if call["name"] != "herdr"]
                self.assertEqual(len(agents), 1)
                agent = agents[0]
                selected = selection or "copilot"
                self.assertEqual(agent["name"], selected)
                self.assertEqual(agent["args"][agent["args"].index("--model") + 1], MODELS[selected])
                self.assertEqual(agent["active"], "1")
                prompt = agent["stdin"] if selected == "codex" else agent["args"][agent["args"].index("-p") + 1]
                self.assertIn("Output ONLY the title", prompt)
                self.assertIn("直前の指示を優先して", prompt)
                self.assertIn("User: 要約エージェントを切り替えたい", prompt)
                tokens = [call["args"][-1] for call in calls if call["name"] == "herdr"]
                self.assertEqual(tokens, ["prompt=直前の指示を優先して", "summary=...", "summary=要約エージェントの切り替え"])
                self.assertEqual(list(self.root.glob("herdr-summarize-codex.*")), [])

    def test_invalid_arguments_fail_without_invoking_clis(self):
        for args in [["--agent"], ["--agent", "invalid"], ["--agent", ""], ["--unknown"]]:
            with self.subTest(args=args):
                result = self.run_hook(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Error:", result.stderr)
                self.assertEqual(self.calls(), [])

    def test_help(self):
        result = self.run_hook("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Default agent: copilot", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_failed_agents_do_not_report_partial_output(self):
        self.env.update(MOCK_EXIT="1", MOCK_RESPONSE="Incomplete response")
        for agent in MODELS:
            with self.subTest(agent=agent):
                self.log.write_text("")
                result = self.run_hook("--agent", agent)
                self.assertEqual(result.returncode, 0, result.stderr)
                tokens = [call["args"][-1] for call in self.calls() if call["name"] == "herdr"]
                self.assertEqual(tokens, ["summary=..."])
                self.assertEqual(list(self.root.glob("herdr-summarize-codex.*")), [])

    def test_empty_agent_response_does_not_report_title(self):
        self.env["MOCK_RESPONSE"] = " \n\n"
        for agent in MODELS:
            with self.subTest(agent=agent):
                self.log.write_text("")
                result = self.run_hook("--agent", agent)
                self.assertEqual(result.returncode, 0, result.stderr)
                tokens = [call["args"][-1] for call in self.calls() if call["name"] == "herdr"]
                self.assertEqual(tokens, ["summary=..."])

    def test_long_title_is_truncated(self):
        self.env["MOCK_RESPONSE"] = "あ" * 40
        result = self.run_hook("--agent", "copilot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1]["args"][-1], "summary=" + "あ" * 29 + "…")

    def test_recursion_guard(self):
        self.env["HERDR_SUMMARIZE_ACTIVE"] = "1"
        for agent in MODELS:
            with self.subTest(agent=agent):
                result = self.run_hook("--agent", agent)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
