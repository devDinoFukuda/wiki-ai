from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

SOURCE = textwrap.dedent(
    '''
    from __future__ import annotations

    import json
    import os
    import subprocess
    import sys
    import time


    def bridge_argv(argv):
        if os.environ["FAKE_STYLE"] == "mcp-config":
            path = argv[argv.index("--mcp-config") + 1]
            server = json.loads(open(path, encoding="utf-8").read())["mcpServers"]["wiki"]
            return [server["command"], *server["args"]]
        text = open(
            os.path.join(os.environ["CODEX_HOME"], "config.toml"), encoding="utf-8"
        ).read()
        command = ""
        args = []
        for line in text.splitlines():
            if line.startswith("command = "):
                command = json.loads(line[len("command = "):])
            elif line.startswith("args = "):
                args = json.loads(line[len("args = "):])
        return [command, *args]


    def exchange(process, identifier, method, params):
        process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            )
            + "\\n"
        )
        process.stdin.flush()
        line = process.stdout.readline()
        return json.loads(line) if line else None


    def main():
        argv = sys.argv[1:]
        if argv == ["--version"]:
            sys.stdout.write("fake 1.0\\n")
            return 0
        report = {"cwd": sorted(os.listdir("."))}
        process = subprocess.Popen(
            bridge_argv(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            exchange(process, 1, "initialize", {})
            listing = exchange(process, 2, "tools/list", {})
            report["tools"] = [t["name"] for t in listing["result"]["tools"]]
            report["replies"] = []
            identifier = 2
            for step in json.loads(os.environ["FAKE_PLAN"]):
                identifier += 1
                report["replies"].append(
                    exchange(
                        process,
                        identifier,
                        "tools/call",
                        {
                            "name": step["name"],
                            "arguments": step.get("arguments", {}),
                        },
                    )
                )
            if os.environ.get("FAKE_HANG"):
                time.sleep(float(os.environ["FAKE_HANG"]))
        finally:
            sys.stdout.write(json.dumps(report) + "\\n")
            sys.stdout.flush()
            process.stdin.close()
            process.terminate()
            process.wait(timeout=10)
        return int(os.environ.get("FAKE_EXIT", "0"))


    if __name__ == "__main__":
        raise SystemExit(main())
    '''
).strip()


def install(directory: Path) -> Path:
    script = directory / "fake_cli.py"
    script.write_text(SOURCE, encoding="utf-8")
    return script


def report_of(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    return {}


FINDING = {
    "claim": "renewal happens monthly",
    "evidence": [{"source_id": "src", "version": "v1", "locator": "billing.py:10-20"}],
}

READ_THEN_FINISH = [
    {"name": "repo.read", "arguments": {"path": "billing.py"}},
    {"name": "wiki.finish", "arguments": {"findings": [FINDING]}},
]
