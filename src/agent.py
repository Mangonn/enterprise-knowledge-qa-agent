"""Bounded model-action / tool-observation loop with evidence-checked finish."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .business import KnowledgeBase, ROOT, validate_input


def _record(state: dict, action: str, arguments: dict, observation: dict,
            reason: str, actor: str, model_turn: int | None = None) -> None:
    entry = {"step": len(state["trace"]) + 1, "actor": actor, "action": action,
             "arguments": arguments, "observation": observation,
             "decision_basis": reason}
    if model_turn is not None:
        entry["model_turn"] = model_turn
    state["trace"].append(entry)
    state["steps"].append({"step": entry["step"], "action": action,
                           "status": "succeeded" if observation.get("ok") else "failed",
                           "tool_result": observation})


def _result(question: str, history: list[dict], state: dict, mode: str,
            outcome: dict | None = None, forced_reason: str | None = None) -> dict:
    trace = deepcopy(state["trace"])
    for index, entry in enumerate(trace):
        entry["next_action"] = trace[index + 1]["action"] if index + 1 < len(trace) else None
    result = {"question": question, "history": history,
              "task_status": "incomplete", "answer": None, "citations": [],
              "evidence_gaps": ["未能完成有依据的回答"],
              "incomplete_reason": forced_reason or "not_finished", "conflict": False,
              "clarification_question": None, "mode": mode,
              "corpus_revision": state["kb"].revision,
              "target_topics": state["target_topics"],
              "tool_results": {"search_documents": state["searches"],
                               "read_snippet": list(state["reads"].values())},
              "tool_call_count": state["tool_count"], "steps": state["steps"], "trace": trace}
    if outcome:
        result.update({key: outcome[key] for key in ("task_status", "answer", "citations",
                       "evidence_gaps", "incomplete_reason", "conflict", "clarification_question")})
    return result


def run(payload: dict, *, model, corpus_path: Path | None = None,
        fail_once: set[str] | None = None, max_steps: int = 12,
        max_tools: int = 10) -> dict:
    question, history = validate_input(payload)
    if max_steps < 1 or max_tools < 1:
        raise ValueError("max_steps and max_tools must be positive")
    kb = KnowledgeBase(corpus_path or ROOT / "data/documents.json")
    failures = set(fail_once or ())
    state = {"kb": kb, "target_topics": kb.target_topics(question, history),
             "searches": [], "reads": {}, "steps": [], "trace": [],
             "tool_count": 0, "last_tool_error": None}
    for _ in range(max_steps):
        try:
            decision = model.decide(question, history, state)
        except Exception as exc:
            _record(state, "model_error", {}, {"ok": False, "error": str(exc)},
                    "模型不可用或响应格式异常", model.name)
            return _result(question, history, state, model.name, forced_reason="model_error")
        action = decision.get("name")
        arguments = decision.get("arguments", {})
        reason = str(decision.get("reason", ""))[:200]
        if not isinstance(arguments, dict):
            _record(state, "invalid_model_arguments", {},
                    {"ok": False, "error": "invalid_model_arguments"},
                    reason, model.name, decision.get("model_turn"))
            return _result(question, history, state, model.name,
                           forced_reason="invalid_model_arguments")
        clean_arguments = {key: value for key, value in arguments.items() if key != "reason"}
        if action == "parallel_calls_rejected":
            observation = {"ok": False, "error": "parallel_calls_rejected"}
        elif action == "finish":
            observation = kb.finish(arguments.get("status"), question, history, state)
        elif state["tool_count"] >= max_tools:
            _record(state, "tool_limit", {}, {"ok": False, "error": "tool_limit"},
                    "业务工具调用次数已到上限", "system")
            return _result(question, history, state, model.name, forced_reason="tool_limit")
        elif action == "search_documents" and isinstance(arguments.get("query"), str):
            state["tool_count"] += 1
            observation = kb.search_documents(arguments["query"], state, failures)
            state["searches"].append(observation)
        elif (action == "read_snippet" and isinstance(arguments.get("document_id"), str)
              and isinstance(arguments.get("snippet_id"), str)):
            state["tool_count"] += 1
            observation = kb.read_snippet(arguments["document_id"], arguments["snippet_id"],
                                          state, failures)
            if observation.get("ok"):
                state["reads"][f"{arguments['document_id']}:{arguments['snippet_id']}"] = observation
        else:
            observation = {"ok": False, "error": "unknown_tool_or_arguments"}
        if action in ("search_documents", "read_snippet"):
            state["last_tool_error"] = None if observation.get("ok") else observation.get("error")
        _record(state, action, clean_arguments, observation, reason,
                model.name, decision.get("model_turn"))
        if action == "finish" and observation.get("accepted"):
            return _result(question, history, state, model.name, outcome=observation)
        model.observe(observation)
    return _result(question, history, state, model.name, forced_reason="step_limit")
