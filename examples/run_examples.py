"""Regenerate offline CLI outputs and traces from synthetic cases."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "examples/outputs"
TRACES = ROOT / "examples/traces"
CASES = {
    "clear_answer": "answered", "conflict": "needs_clarification",
    "outside": "insufficient", "paraphrase": "needs_clarification",
    "followup": "insufficient", "tool_failure": "answered",
    "solo_policy": "answered", "unsupported_detail": "insufficient",
    "unsupported_payout": "insufficient",
    "search_failure": "answered", "step_limit": "incomplete",
    "changed_policy_45": "answered", "aligned_policies": "answered",
}


def invoke(case: str) -> dict:
    input_name = ("clear_answer" if case in ("search_failure", "step_limit")
                  else "solo_policy" if case == "changed_policy_45"
                  else "conflict" if case == "aligned_policies" else case)
    command = [sys.executable, "-m", "src.cli", f"examples/inputs/{input_name}.json",
               "--mode", "mock"]
    if case == "solo_policy":
        command += ["--corpus", "data/solo_reimbursement.json"]
    if case == "changed_policy_45":
        command += ["--corpus", "data/changed_policy_45.json"]
    if case == "aligned_policies":
        command += ["--corpus", "data/aligned_policies.json"]
    if case == "tool_failure":
        command += ["--fail-once", "read_snippet"]
    if case == "search_failure":
        command += ["--fail-once", "search_documents"]
    if case == "step_limit":
        command += ["--max-steps", "1"]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if not completed.stdout:
        raise RuntimeError(f"{case}: {completed.stderr}")
    return json.loads(completed.stdout)


results = {case: invoke(case) for case in CASES}
for case, result in results.items():
    if result["task_status"] != CASES[case] or result["mode"] != "mock_policy":
        raise RuntimeError(f"{case}: unexpected output; files were not replaced")
OUTPUTS.mkdir(exist_ok=True)
TRACES.mkdir(exist_ok=True)
for case, result in results.items():
    (OUTPUTS / f"{case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (TRACES / f"{case}.json").write_text(json.dumps(result["trace"], ensure_ascii=False, indent=2) + "\n")
print(f"Generated {len(results)} offline CLI outputs and traces")
