from __future__ import annotations

import unittest

from codescan.noise import (
    redact_tool_noise,
    summarize_command_result,
    validate_agent_output,
    validate_chat_update,
)


class NoiseContractTest(unittest.TestCase):
    def test_chat_update_rejects_command_echo(self) -> None:
        errors = validate_chat_update("Ran command: python wk.pyz code audit")
        self.assertIn("command_echo", errors)

    def test_chat_update_rejects_edit_echo(self) -> None:
        for token in ("Edited README.md", "Write file", "Wrote test.py"):
            with self.subTest(token=token):
                self.assertIn("edit_echo", validate_chat_update(token))

    def test_chat_update_rejects_diff_echo(self) -> None:
        text = "\n".join(["diff --git a/x b/x", "@@ -1 +1 @@", "+++ b/x", "--- a/x"])
        errors = validate_chat_update(text)
        self.assertIn("diff_echo", errors)

    def test_agent_output_rejects_written_artifact_echo(self) -> None:
        text = "Conteúdo do arquivo recém-escrito:\n# Artefato"
        errors = validate_agent_output(text)
        self.assertIn("written_artifact_echo", errors)

    def test_agent_output_rejects_long_log(self) -> None:
        text = "\n".join(f"2026-01-01 10:00:{i:02d} ERROR failure {i}" for i in range(25))
        errors = validate_agent_output(text)
        self.assertIn("long_log_echo", errors)

    def test_agent_output_rejects_long_stacktrace(self) -> None:
        text = "\n".join(
            ["Traceback (most recent call last):"]
            + [f'  File "app.py", line {i}, in run' for i in range(20)]
            + ["RuntimeError: failed"]
        )
        errors = validate_agent_output(text)
        self.assertIn("long_stacktrace_echo", errors)

    def test_redact_tool_noise_removes_forbidden_lines(self) -> None:
        text = "\n".join(["STATUS: x", "Ran command: y", "Edited z", "RESULTADO: ok"])
        redacted = redact_tool_noise(text)
        self.assertIn("STATUS: x", redacted)
        self.assertIn("RESULTADO: ok", redacted)
        self.assertNotIn("Ran command", redacted)
        self.assertNotIn("Edited", redacted)

    def test_summarize_empty_command_result(self) -> None:
        self.assertEqual("", summarize_command_result("", ""))

    def test_summarize_long_stacktrace(self) -> None:
        stderr = "\n".join(
            ["Traceback (most recent call last):"]
            + [f'  File "app.py", line {i}, in run' for i in range(20)]
            + ["ValueError: invalid state"]
        )
        summary = summarize_command_result("", stderr)
        self.assertEqual("STACKTRACE: ValueError: invalid state", summary)

    def test_summarize_long_output_counts_files(self) -> None:
        stdout = "\n".join(f"src/app/File{i % 3}.java: match {i}" for i in range(30))
        summary = summarize_command_result(stdout, "", max_lines=3)
        self.assertLessEqual(len(summary.splitlines()), 3)
        self.assertIn("File", summary)


if __name__ == "__main__":
    unittest.main()
