"""Capture verified real DeepSeek model feedback paths."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "examples/model_outputs"
TRACES = ROOT / "examples/model_traces"
CASES = ("clear_answer", "conflict", "outside", "paraphrase", "followup", "tool_failure",
         "changed_policy_45", "aligned_policies", "unsupported_payout")


def verify(case: str, result: dict) -> None:
    trace = result["trace"]
    if result["mode"] != "deepseek_chat":
        raise RuntimeError(f"{case}: this was not a real model run")
    if case in ("clear_answer", "tool_failure"):
        if result["task_status"] != "answered" or [item["document_id"] for item in result["citations"]] != ["D03"]:
            raise RuntimeError(f"{case}: expected a sourced D03 answer")
    if case == "followup" and (result["task_status"] != "insufficient"
                               or [item["document_id"] for item in result["citations"]] != ["D03"]):
        raise RuntimeError("followup: unsupported email-delivery detail was answered")
    if case in ("conflict", "paraphrase"):
        if (result["task_status"] != "needs_clarification" or not result["conflict"]
                or [item["document_id"] for item in result["citations"]] != ["D01", "D02"]):
            raise RuntimeError(f"{case}: conflict was not verified and explained")
        if not all(value in result["answer"] for value in ("30 天", "15 天")):
            raise RuntimeError(f"{case}: conflicting values are missing")
    if case == "outside" and (result["task_status"] != "insufficient" or result["citations"]):
        raise RuntimeError("outside: unsupported claim or fabricated citation")
    if case == "changed_policy_45" and (result["task_status"] != "answered"
                                        or [item["document_id"] for item in result["citations"]] != ["D01"]
                                        or "报销期限为 45 天" not in result["answer"]):
        raise RuntimeError("changed_policy_45: answer did not follow replacement source")
    if case == "aligned_policies" and (result["task_status"] != "answered" or result["conflict"]
                                       or [item["document_id"] for item in result["citations"]] != ["D01", "D02"]
                                       or "报销期限为 30 天" not in result["answer"]):
        raise RuntimeError("aligned_policies: identical source values should resolve conflict")
    if case == "unsupported_payout" and (result["task_status"] != "insufficient"
                                         or "报销期限为" in result["answer"]):
        raise RuntimeError("unsupported_payout: deadline source cannot answer payout timing")
    if case == "tool_failure":
        failures = [index for index, entry in enumerate(trace)
                    if entry["action"] == "read_snippet" and entry["observation"].get("error") == "reader_timeout"]
        if not failures or not any(entry["action"] == "read_snippet" and entry["observation"].get("ok")
                                   for entry in trace[failures[0] + 1:]):
            raise RuntimeError("tool_failure: no observed recovery after reader timeout")
    if result["task_status"] in ("answered", "needs_clarification"):
        for citation in result["citations"]:
            if citation["quote"] not in result["answer"]:
                raise RuntimeError(f"{case}: quote missing from answer")


parser = argparse.ArgumentParser()
parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
selected = parser.parse_args().cases
OUTPUTS.mkdir(exist_ok=True)
TRACES.mkdir(exist_ok=True)
for case in selected:
    input_name = ("solo_policy" if case == "changed_policy_45"
                  else "conflict" if case == "aligned_policies" else case)
    command = [sys.executable, "-m", "src.cli", f"examples/inputs/{input_name}.json", "--mode", "deepseek"]
    if case == "changed_policy_45":
        command += ["--corpus", "data/changed_policy_45.json"]
    if case == "aligned_policies":
        command += ["--corpus", "data/aligned_policies.json"]
    if case == "tool_failure":
        command += ["--fail-once", "read_snippet"]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if not completed.stdout:
        raise RuntimeError(f"{case}: {completed.stderr}")
    result = json.loads(completed.stdout)
    verify(case, result)
    (OUTPUTS / f"deepseek_{case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (TRACES / f"deepseek_{case}.json").write_text(json.dumps(result["trace"], ensure_ascii=False, indent=2) + "\n")
    print(f"{case}: {result['task_status']} ({len(result['trace'])} steps)", flush=True)
