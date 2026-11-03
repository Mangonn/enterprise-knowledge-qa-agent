"""Synthetic document search, original-snippet reading, and evidence guards."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in value if character.isalnum())


def validate_input(payload: dict) -> tuple[str, list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a nonempty string")
    history = payload.get("history", [])
    if not isinstance(history, list):
        raise ValueError("history must be a list")
    for entry in history:
        if (not isinstance(entry, dict) or entry.get("role") not in ("user", "assistant")
                or not isinstance(entry.get("content"), str)):
            raise ValueError("each history item needs role=user|assistant and string content")
    return question.strip(), history


class KnowledgeBase:
    def __init__(self, corpus_path: Path):
        raw = json.loads(corpus_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("corpus must be a JSON list")
        self.documents = raw
        seen = set()
        for doc in self.documents:
            document_id = doc["document_id"]
            if document_id in seen:
                raise ValueError(f"duplicate document_id: {document_id}")
            seen.add(document_id)
            date.fromisoformat(doc["version_date"])
            if not doc["snippets"] or not doc["aliases"]:
                raise ValueError(f"document {document_id} needs snippets and aliases")
            for snippet in doc["snippets"]:
                if not snippet["text"].strip():
                    raise ValueError(f"empty snippet in {document_id}")
        self.by_id = {doc["document_id"]: doc for doc in self.documents}
        self.revision = hashlib.sha256(corpus_path.read_bytes()).hexdigest()[:12]

    def topics_for(self, text: str) -> list[str]:
        query = normalize(text)
        found = set()
        for doc in self.documents:
            if any(normalize(alias) in query for alias in doc["aliases"] if normalize(alias)):
                found.add(doc["topic"])
        return sorted(found)

    def target_topics(self, question: str, history: list[dict]) -> list[str]:
        current = self.topics_for(question)
        if current:
            return current
        for entry in reversed(history):
            if entry["role"] == "user":
                topics = self.topics_for(entry["content"])
                if topics:
                    return topics
        return []

    def search_documents(self, query: str, state: dict, fail_once: set[str]) -> dict:
        if "search_documents" in fail_once:
            fail_once.remove("search_documents")
            return {"ok": False, "error": "search_service_timeout", "query": query}
        topics = self.topics_for(query)
        matches = []
        for doc in self.documents:
            if doc["topic"] in topics:
                matches.append({"document_id": doc["document_id"], "title": doc["title"],
                                "version_date": doc["version_date"], "topic": doc["topic"],
                                "snippet_ids": [item["snippet_id"] for item in doc["snippets"]],
                                "scope": doc.get("scope"), "priority": doc.get("priority")})
        matches.sort(key=lambda item: (item["topic"], item["document_id"]))
        return {"ok": True, "query": query, "matched_topics": topics,
                "matches": matches, "corpus_revision": self.revision}

    def read_snippet(self, document_id: str, snippet_id: str,
                     state: dict, fail_once: set[str]) -> dict:
        discovered = {item["document_id"] for result in state["searches"] if result.get("ok")
                      for item in result["matches"]}
        if document_id not in discovered:
            return {"ok": False, "error": "document_not_in_search_results",
                    "document_id": document_id, "snippet_id": snippet_id}
        doc = self.by_id.get(document_id)
        snippet = next((item for item in doc["snippets"] if item["snippet_id"] == snippet_id), None) if doc else None
        if snippet is None:
            return {"ok": False, "error": "snippet_not_found", "document_id": document_id,
                    "snippet_id": snippet_id}
        if "read_snippet" in fail_once:
            fail_once.remove("read_snippet")
            return {"ok": False, "error": "reader_timeout", "document_id": document_id,
                    "snippet_id": snippet_id}
        quote = snippet["text"]
        return {"ok": True, "document_id": document_id, "snippet_id": snippet_id,
                "version_date": doc["version_date"], "title": doc["title"],
                "topic": doc["topic"], "quote": quote,
                "source_hash": hashlib.sha256(quote.encode()).hexdigest()[:12]}

    @staticmethod
    def _unsupported_detail(question: str, topic: str, quotes: list[str]) -> bool:
        text = normalize(question)
        combined = " ".join(quotes)
        if topic == "password_reset" and any(cue in text for cue in
                                              ("收不到", "没收到", "未收到", "收不到验证码", "收不到邮件")):
            return not any(cue in normalize(combined) for cue in ("收不到", "没收到", "未收到"))
        if topic == "expense_deadline":
            other_details = ("到账", "打款", "支付", "额度", "上限", "比例", "发票要求",
                             "审批", "流程", "收费", "价格", "例外", "手续费")
            deadline_cues = ("期限", "最晚", "截止", "时限", "天内", "几天", "提交", "发生后")
            return (any(word in text for word in other_details)
                    or not any(word in text for word in deadline_cues))
        if any(word in text for word in ("有效期", "多久", "几天", "多少分钟", "收费", "价格",
                                         "密码长度", "复杂度", "被盗", "锁定")):
            return re.search(r"\d+\s*(分钟|小时|天|元)", combined) is None
        return False

    def finish(self, proposed_status: str, question: str, history: list[dict], state: dict) -> dict:
        """Accept only a conclusion supported by successfully read original snippets."""
        if proposed_status == "stopped":
            return {"ok": True, "accepted": True, "task_status": "incomplete",
                    "answer": None, "citations": [], "evidence_gaps": ["模型停止或工具持续失败"],
                    "incomplete_reason": "tool_failure" if state.get("last_tool_error") else "model_stopped",
                    "conflict": False, "clarification_question": None}
        if not state["searches"]:
            return {"ok": False, "accepted": False, "error": "search_required"}
        if not any(result.get("ok") for result in state["searches"]):
            return {"ok": False, "accepted": False, "error": "successful_search_required"}
        target = self.target_topics(question, history)
        if len(target) > 1:
            if proposed_status != "ask_clarification":
                return {"ok": False, "accepted": False,
                        "error": "ambiguous_topic_requires_clarification"}
            return {"ok": True, "accepted": True, "task_status": "needs_clarification",
                    "answer": "问题可能涉及多个制度主题，请确认要查询的主题。",
                    "citations": [], "evidence_gaps": ["问题主题不明确"],
                    "incomplete_reason": None, "conflict": False,
                    "clarification_question": "请确认要查询的制度主题。"}
        topic = target[0] if target else None
        relevant = [doc for doc in self.documents if doc["topic"] == topic] if topic else []
        if not relevant:
            if proposed_status != "insufficient":
                return {"ok": False, "accepted": False, "error": "no_relevant_source"}
            return {"ok": True, "accepted": True, "task_status": "insufficient",
                    "answer": "现有资料没有足够依据回答该问题。", "citations": [],
                    "evidence_gaps": ["没有检索到对应制度或 FAQ"], "incomplete_reason": None,
                    "conflict": False, "clarification_question": None}
        discovered = {item["document_id"] for result in state["searches"] if result.get("ok")
                      for item in result["matches"]}
        undiscovered = [doc["document_id"] for doc in relevant
                        if doc["document_id"] not in discovered]
        if undiscovered:
            return {"ok": False, "accepted": False, "error": "supplementary_search_required",
                    "missing_document_ids": undiscovered}
        required = [(doc, snippet) for doc in relevant for snippet in doc["snippets"]]
        unread = [f"{doc['document_id']}:{snippet['snippet_id']}" for doc, snippet in required
                  if f"{doc['document_id']}:{snippet['snippet_id']}" not in state["reads"]]
        if unread:
            return {"ok": False, "accepted": False, "error": "unread_relevant_snippets",
                    "unread": unread}
        citations = [state["reads"][f"{doc['document_id']}:{snippet['snippet_id']}"]
                     for doc, snippet in required]
        quotes = [item["quote"] for item in citations]
        if self._unsupported_detail(question, topic, quotes):
            if proposed_status != "insufficient":
                return {"ok": False, "accepted": False, "error": "requested_detail_not_in_sources"}
            return {"ok": True, "accepted": True, "task_status": "insufficient",
                    "answer": "已找到相关资料，但原文没有回答所问的具体细节。", "citations": citations,
                    "evidence_gaps": ["所问细节未见于现有原文"], "incomplete_reason": None,
                    "conflict": False, "clarification_question": None}
        values = ([re.search(r"(\d+)\s*天", quote) for quote in quotes]
                  if topic == "expense_deadline" else [])
        if topic == "expense_deadline" and any(value is None for value in values):
            if proposed_status != "insufficient":
                return {"ok": False, "accepted": False, "error": "policy_value_not_verifiable"}
            return {"ok": True, "accepted": True, "task_status": "insufficient",
                    "answer": "已找到报销资料，但无法从原文核定期限。", "citations": citations,
                    "evidence_gaps": ["报销期限原文未给出可核定的天数"], "incomplete_reason": None,
                    "conflict": False, "clarification_question": None}
        distinct = {value.group(1) for value in values}
        references = "；".join(f"{item['document_id']}（{item['version_date']}）：“{item['quote']}”"
                              for item in citations)
        if len(distinct) > 1:
            if proposed_status not in ("conflict", "ask_clarification"):
                return {"ok": False, "accepted": False, "error": "unresolved_document_conflict",
                        "document_ids": [item["document_id"] for item in citations]}
            question_to_user = "请确认适用的制度、适用范围或优先级。"
            return {"ok": True, "accepted": True, "task_status": "needs_clarification",
                    "answer": f"现有资料对报销期限存在冲突，不能确定统一答案：{references}。{question_to_user}",
                    "citations": citations, "evidence_gaps": ["制度适用范围与优先级未提供"],
                    "incomplete_reason": None, "conflict": True,
                    "clarification_question": question_to_user}
        if proposed_status != "answered":
            return {"ok": False, "accepted": False, "error": "answer_available"}
        if topic == "expense_deadline":
            answer = f"按当前可用制度原文，报销期限为 {next(iter(distinct))} 天。依据：{references}。"
        else:
            answer = f"依据已读取的原文：{references}。"
        return {"ok": True, "accepted": True, "task_status": "answered", "answer": answer,
                "citations": citations, "evidence_gaps": [], "incomplete_reason": None,
                "conflict": False, "clarification_question": None}
