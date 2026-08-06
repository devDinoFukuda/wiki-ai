from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from codescan import agentmerge as agentmerge_mod
from codescan import state as st_mod


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _sha256_bytes(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class ManifestProvenanceTest(unittest.TestCase):
    def test_manifest_schema_v2_with_input_and_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(
                inp,
                "=== MODULE: src/payments ===\n# Pagamentos\n\n## Responsabilidade\n- ok\n=== END ===\n",
            )

            result = agentmerge_mod.merge_agent_output(wd, "modules", inp, agent="Module Archaeologist")

            manifest_path = os.path.join(wd, "agent-runs", "modules.json")
            self.assertEqual(manifest_path, result["manifest"])
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)

            self.assertEqual(data["schema"], "wiki-ai.agent-runs.v2")
            self.assertEqual(data["stage"], "modules")
            self.assertEqual(len(data["runs"]), 1)
            run = data["runs"][0]

            self.assertEqual(run["stage"], "modules")
            self.assertEqual(os.path.abspath(run["input"]), os.path.abspath(inp))
            self.assertEqual(run["input_sha256"], _sha256_bytes(inp))
            self.assertEqual(run["input_bytes"], os.path.getsize(inp))
            self.assertEqual(run["agent"], "Module Archaeologist")
            self.assertEqual(run["items_count"], 1)
            self.assertEqual(run["artifacts_count"], 1)

            item_entry = run["items"][0]
            self.assertEqual(item_entry["item"], "src/payments")
            artifact_entry = item_entry["artifacts"][0]
            self.assertEqual(artifact_entry["sha256"], _sha256_bytes(artifact_entry["path"]))
            self.assertEqual(artifact_entry["bytes"], os.path.getsize(artifact_entry["path"]))

    def test_manifest_agent_defaults_to_null_when_not_informed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            inp = os.path.join(tmp, "agent.txt")
            _write(
                inp,
                "=== SPEC: checkout ===\n--- requirements.md ---\n# Req\n--- design.md ---\n# Design\n"
                "--- tasks.md ---\n# Tarefas\n=== END ===\n",
            )

            agentmerge_mod.merge_agent_output(wd, "specs", inp)

            manifest_path = os.path.join(wd, "agent-runs", "specs.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["schema"], "wiki-ai.agent-runs.v2")
            self.assertIsNone(data["runs"][0]["agent"])

    def test_manifest_accumulates_runs_with_independent_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            store = os.path.join(tmp, "store")
            os.makedirs(repo)
            wd = st_mod.workdir(store, repo)
            st_mod.init(wd, repo, "codebases/test")
            first = os.path.join(tmp, "agent-1.txt")
            second = os.path.join(tmp, "agent-2.txt")
            _write(first, "=== RULES: domain ===\n# Domínio\n\n- 🟢 Regra A.\n=== END ===\n")
            _write(
                second,
                "=== RULES: permissions ===\n# Permissões\n\n- 🟢 Regra B.\n=== END ===\n",
            )

            agentmerge_mod.merge_agent_output(wd, "rules", first, agent="Business Rules Detective")
            agentmerge_mod.merge_agent_output(wd, "rules", second)

            manifest_path = os.path.join(wd, "agent-runs", "rules.json")
            with open(manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(len(data["runs"]), 2)
            self.assertEqual(data["runs"][0]["agent"], "Business Rules Detective")
            self.assertEqual(data["runs"][0]["input_sha256"], _sha256_bytes(first))
            self.assertIsNone(data["runs"][1]["agent"])
            self.assertEqual(data["runs"][1]["input_sha256"], _sha256_bytes(second))
            self.assertNotEqual(data["runs"][0]["input_sha256"], data["runs"][1]["input_sha256"])


if __name__ == "__main__":
    unittest.main()
