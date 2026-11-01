"""DeepSeek tool-choice model and a clearly labelled offline workflow policy."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request


class ModelUnavailableError(RuntimeError):
    pass


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {
                **properties, "reason": {"type": "string", "description": "Short factual basis; no hidden reasoning."}},
                "required": [*required, "reason"], "additionalProperties": False}}}


TOOLS = [
    _tool("search_documents", "Search document metadata by a query. Returns IDs and snippet IDs, not the original text.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("read_snippet", "Read the exact original snippet of a document found by search.",
          {"document_id": {"type": "string"}, "snippet_id": {"type": "string"}},
          ["document_id", "snippet_id"]),
    _tool("finish", "Finish only after enough original snippets have been read; the program validates evidence and builds the final answer.",
          {"status": {"type": "string", "enum": ["answered", "conflict", "ask_clarification", "insufficient", "stopped"]}},
          ["status"]),
]


INSTRUCTIONS = """You are an enterprise knowledge QA agent. Choose exactly ONE function call per response.
First search the synthetic documents for the user's question, using relevant conversation history for follow-ups.
Search returns metadata only. Read the exact original snippet for EACH relevant document before concluding.
If a tool times out, observe the error and choose whether to retry, search differently, or stop.
For reimbursement deadlines, different values in D01 and D02 with no documented scope or priority are a conflict.
Do not treat a newer version date alone as proof that one rule supersedes the other.
When the documents conflict, choose finish status conflict or ask_clarification; ask which policy applies.
If no document supports the question, choose insufficient. If the requested detail is absent, choose insufficient.
If an exact answer is supported, choose answered. If tools persistently fail, choose stopped.
The program builds answer text ONLY from successfully read original snippets and may reject an unsupported finish.
On a rejected finish, use the validation observation to choose the next action.
Use a short factual reason, never reveal hidden chain of thought.
"""


class MockPolicy:
    """Observation-driven offline flow check; this is not a language model."""

    name = "mock_policy"

    def decide(self, question: str, history: list[dict], state: dict) -> dict:
        searches = state["searches"]
        if not searches or not searches[-1].get("ok"):
            query = question
            if not state["target_topics"]:
                query = question
            elif not state["kb"].topics_for(question):
                query = next((item["content"] for item in reversed(history)
                              if item["role"] == "user"), question)
            return {"name": "search_documents", "arguments": {"query": query},
                    "reason": "检索当前问题及必要的对话上下文"}
        matches = [item for item in searches[-1]["matches"]
                   if item["topic"] in state["target_topics"]]
        for item in matches:
            for snippet_id in item["snippet_ids"]:
                key = f"{item['document_id']}:{snippet_id}"
                if key not in state["reads"]:
                    return {"name": "read_snippet", "arguments": {
                        "document_id": item["document_id"], "snippet_id": snippet_id},
                        "reason": "读取检索命中的原文，核对引用及可能冲突"}
        if not matches:
            status, reason = "insufficient", "未检索到支持问题的文档"
        else:
            quotes = [state["reads"][f"{item['document_id']}:{snippet_id}"]["quote"]
                      for item in matches for snippet_id in item["snippet_ids"]]
            if state["kb"]._unsupported_detail(question, matches[0]["topic"], quotes):
                status, reason = "insufficient", "原文未覆盖所问细节"
            else:
                values = [re.search(r"(\d+)\s*天", quote) for quote in quotes]
                if matches[0]["topic"] == "expense_deadline" and any(value is None for value in values):
                    status, reason = "insufficient", "报销期限无法从原文核定"
                elif matches[0]["topic"] == "expense_deadline" and len({value.group(1) for value in values}) > 1:
                    status, reason = "conflict", "两份制度期限不同且无适用范围或优先级"
                else:
                    status, reason = "answered", "已读取原文，相关资料可支持回答"
        return {"name": "finish", "arguments": {"status": status}, "reason": reason}

    def observe(self, observation: dict) -> None:
        pass


class DeepSeekModel:
    name = "deepseek_chat"

    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ModelUnavailableError("DEEPSEEK_API_KEY is required for --mode deepseek")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.messages = None
        self.rejected_call_ids: list[str] = []
        self.turn = 0
        self.call_id = None

    def decide(self, question: str, history: list[dict], state: dict) -> dict:
        if self.messages is None:
            context = {"question": question, "history": history, "prior_state": {
                "searches": state["searches"], "reads": list(state["reads"].values())}}
            self.messages = [{"role": "system", "content": INSTRUCTIONS},
                             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        payload = {"model": self.model, "messages": self.messages, "tools": TOOLS,
                   "tool_choice": "required", "thinking": {"type": "disabled"}, "max_tokens": 1000}
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"model_http_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"model_transport_error:{type(exc).__name__}") from exc
        self.turn += 1
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            raise RuntimeError("model_returned_no_tool_call")
        self.messages.append({"role": "assistant", "content": data["choices"][0]["message"].get("content"),
                              "tool_calls": calls})
        if len(calls) > 1:
            self.rejected_call_ids = [call["id"] for call in calls]
            return {"name": "parallel_calls_rejected", "arguments": {},
                    "reason": "并行动作可能跳过检索与原文读取的依赖", "model_turn": self.turn}
        call = calls[0]
        self.call_id = call["id"]
        try:
            arguments = json.loads(call["function"]["arguments"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_model_tool_arguments") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("invalid_model_tool_arguments")
        return {"name": call["function"]["name"], "arguments": arguments,
                "reason": str(arguments.get("reason", "模型选择此动作"))[:200],
                "model_turn": self.turn}

    def observe(self, observation: dict) -> None:
        if self.rejected_call_ids:
            for call_id in self.rejected_call_ids:
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "content": json.dumps({"error": "parallel_calls_rejected"})})
            self.rejected_call_ids = []
            return
        self.messages.append({"role": "tool", "tool_call_id": self.call_id,
                              "content": json.dumps(observation, ensure_ascii=False)})
