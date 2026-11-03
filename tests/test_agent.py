import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent import run
from src.business import KnowledgeBase
from src.models import DeepSeekModel, MockPolicy


ROOT = Path(__file__).resolve().parents[1]


class KnowledgeAcceptanceTests(unittest.TestCase):
    def payload(self, name):
        return json.loads((ROOT / "examples/inputs" / f"{name}.json").read_text())

    def case(self, name, **options):
        return run(self.payload(name), model=MockPolicy(), **options)

    def test_clear_answer_cites_exact_original_and_version(self):
        result = self.case("clear_answer")
        self.assertEqual(result["task_status"], "answered")
        self.assertEqual([x["document_id"] for x in result["citations"]], ["D03"])
        quote = result["citations"][0]["quote"]
        self.assertIn("企业邮箱验证码重置", quote)
        self.assertIn(quote, result["answer"])
        self.assertEqual(result["citations"][0]["version_date"], "2026-03-01")
        self.assertEqual(result["citations"][0]["source_hash"], hashlib.sha256(quote.encode()).hexdigest()[:12])

    def test_conflict_reads_both_and_asks_for_applicability(self):
        result = self.case("conflict")
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertTrue(result["conflict"])
        self.assertEqual([x["document_id"] for x in result["citations"]], ["D01", "D02"])
        self.assertIn("30 天", result["answer"])
        self.assertIn("15 天", result["answer"])
        self.assertIn("适用", result["clarification_question"])
        self.assertNotIn("报销期限为 15 天", result["answer"])

    def test_single_policy_can_answer_thirty_days(self):
        result = self.case("solo_policy", corpus_path=ROOT / "data/solo_reimbursement.json")
        self.assertEqual(result["task_status"], "answered")
        self.assertIn("报销期限为 30 天", result["answer"])
        self.assertEqual([x["document_id"] for x in result["citations"]], ["D01"])

    def test_out_of_corpus_has_no_fabricated_citation(self):
        result = self.case("outside")
        self.assertEqual(result["task_status"], "insufficient")
        self.assertEqual(result["citations"], [])
        self.assertTrue(result["evidence_gaps"])

    def test_paraphrase_retrieves_same_conflict(self):
        result = self.case("paraphrase")
        self.assertEqual(result["target_topics"], ["expense_deadline"])
        self.assertEqual(result["task_status"], "needs_clarification")
        self.assertEqual([x["document_id"] for x in result["citations"]], ["D01", "D02"])

    def test_followup_uses_history_without_answering_unsupported_detail(self):
        result = self.case("followup")
        self.assertEqual(KnowledgeBase(ROOT / "data/documents.json").topics_for(
            self.payload("followup")["question"]), [])
        self.assertEqual(result["target_topics"], ["password_reset"])
        self.assertEqual(result["task_status"], "insufficient")
        self.assertEqual(result["citations"][0]["document_id"], "D03")
        self.assertIn("原文没有回答", result["answer"])

    def test_missing_verification_email_detail_is_not_inferred_from_mailbox_unavailable(self):
        request = {"question": "重置密码收不到验证码邮件怎么办？"}
        result = run(request, model=MockPolicy())
        self.assertEqual(result["task_status"], "insufficient")
        self.assertEqual([item["document_id"] for item in result["citations"]], ["D03"])

    def test_read_timeout_is_failed_and_then_model_retries(self):
        result = self.case("tool_failure", fail_once={"read_snippet"})
        self.assertEqual(result["task_status"], "answered")
        self.assertEqual([x["status"] for x in result["steps"]],
                         ["succeeded", "failed", "succeeded", "succeeded"])
        self.assertEqual(result["steps"][1]["tool_result"]["error"], "reader_timeout")
        self.assertEqual(result["trace"][1]["next_action"], "read_snippet")

    def test_search_timeout_is_observed_and_recovered(self):
        result = self.case("clear_answer", fail_once={"search_documents"})
        self.assertEqual(result["task_status"], "answered")
        self.assertEqual(result["steps"][0]["status"], "failed")
        self.assertEqual(result["trace"][0]["next_action"], "search_documents")

    def test_failed_search_cannot_be_misreported_as_no_document(self):
        kb = KnowledgeBase(ROOT / "data/documents.json")
        state = {"searches": [{"ok": False, "error": "search_service_timeout"}],
                 "reads": {}, "last_tool_error": "search_service_timeout"}
        outcome = kb.finish("insufficient", "公司年假有几天？", [], state)
        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["error"], "successful_search_required")

    def test_unsupported_detail_is_not_filled_in(self):
        result = self.case("unsupported_detail")
        self.assertEqual(result["task_status"], "insufficient")
        self.assertIn("细节", result["evidence_gaps"][0])
        self.assertEqual(result["citations"][0]["document_id"], "D03")

    def test_same_topic_but_unsupported_payout_detail_is_not_answered(self):
        result = self.case("unsupported_payout")
        self.assertEqual(result["task_status"], "insufficient")
        self.assertNotIn("报销期限为", result["answer"])
        self.assertEqual([item["document_id"] for item in result["citations"]], ["D01", "D02"])

    def test_replacing_source_text_changes_answer_and_hash(self):
        old = self.case("solo_policy", corpus_path=ROOT / "data/solo_reimbursement.json")
        with tempfile.TemporaryDirectory() as directory:
            docs = json.loads((ROOT / "data/solo_reimbursement.json").read_text())
            docs[0]["snippets"][0]["text"] = "员工费用报销应在费用发生后 45 天内提交。"
            path = Path(directory) / "changed.json"
            path.write_text(json.dumps(docs, ensure_ascii=False))
            new = self.case("solo_policy", corpus_path=path)
        self.assertIn("报销期限为 45 天", new["answer"])
        self.assertNotEqual(old["citations"][0]["source_hash"], new["citations"][0]["source_hash"])
        self.assertIn("45 天", new["citations"][0]["quote"])

    def test_replacing_conflicting_text_resolves_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = json.loads((ROOT / "data/documents.json").read_text())
            docs[1]["snippets"][0]["text"] = "员工费用报销应在费用发生后 30 天内提交。"
            path = Path(directory) / "aligned.json"
            path.write_text(json.dumps(docs, ensure_ascii=False))
            result = self.case("conflict", corpus_path=path)
        self.assertEqual(result["task_status"], "answered")
        self.assertFalse(result["conflict"])
        self.assertIn("报销期限为 30 天", result["answer"])

    def test_model_cannot_finish_before_search_or_read(self):
        class PrematureModel:
            name = "premature"

            def decide(self, question, history, state):
                return {"name": "finish", "arguments": {"status": "answered"}, "reason": "guess"}

            def observe(self, observation):
                pass

        result = run(self.payload("clear_answer"), model=PrematureModel(), max_steps=2)
        self.assertEqual(result["task_status"], "incomplete")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["steps"][0]["tool_result"]["error"], "search_required")

    def test_model_cannot_read_unsearched_document(self):
        class GuessReadModel:
            name = "guess_read"

            def decide(self, question, history, state):
                return {"name": "read_snippet", "arguments": {
                    "document_id": "D03", "snippet_id": "S01"}, "reason": "guess"}

            def observe(self, observation):
                pass

        result = run(self.payload("clear_answer"), model=GuessReadModel(), max_steps=1)
        self.assertEqual(result["steps"][0]["tool_result"]["error"],
                         "document_not_in_search_results")
        self.assertEqual(result["citations"], [])

    def test_conflict_cannot_be_finished_as_single_answer(self):
        kb = KnowledgeBase(ROOT / "data/documents.json")
        state = {"searches": [], "reads": {}, "last_tool_error": None}
        state["searches"].append(kb.search_documents("报销期限", state, set()))
        for doc_id in ("D01", "D02"):
            state["reads"][f"{doc_id}:S01"] = kb.read_snippet(doc_id, "S01", state, set())
        rejected = kb.finish("answered", "报销期限是多久？", [], state)
        self.assertEqual(rejected["error"], "unresolved_document_conflict")
        self.assertFalse(rejected["accepted"])

    def test_limits_return_explicit_incomplete_state(self):
        for options, expected in (({"max_steps": 1}, "step_limit"),
                                  ({"max_tools": 1}, "tool_limit")):
            with self.subTest(expected=expected):
                result = self.case("clear_answer", **options)
                self.assertEqual(result["task_status"], "incomplete")
                self.assertEqual(result["incomplete_reason"], expected)
                self.assertIsNone(result["answer"])

    def test_deepseek_adapter_feeds_observation_to_next_turn(self):
        responses = [
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "search_documents",
                 "arguments": '{"query":"密码","reason":"search"}'}}]}}]},
            {"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "two", "type": "function", "function": {"name": "read_snippet",
                 "arguments": '{"document_id":"D03","snippet_id":"S01","reason":"read"}'}}]}}]},
        ]
        payloads = []

        def fake_urlopen(request, timeout):
            payloads.append(json.loads(request.data))
            return io.BytesIO(json.dumps(responses[len(payloads) - 1]).encode())

        state = {"searches": [], "reads": {}}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            model = DeepSeekModel()
            first = model.decide("密码", [], state)
            model.observe({"ok": True, "matches": [{"document_id": "D03"}]})
            second = model.decide("密码", [], state)
        self.assertEqual(first["name"], "search_documents")
        self.assertEqual(second["name"], "read_snippet")
        self.assertEqual(payloads[1]["messages"][-1]["role"], "tool")

    def test_parallel_actions_are_rejected_without_business_calls(self):
        response = {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "search_documents",
             "arguments": '{"query":"密码","reason":"search"}'}},
            {"id": "two", "type": "function", "function": {"name": "read_snippet",
             "arguments": '{"document_id":"D03","snippet_id":"S01","reason":"read"}'}}
        ]}}]}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch(
            "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())
        ):
            result = run(self.payload("clear_answer"), model=DeepSeekModel(), max_steps=1)
        self.assertEqual(result["tool_call_count"], 0)
        self.assertEqual(result["trace"][0]["action"], "parallel_calls_rejected")

    def test_cli_without_model_key_is_explicit(self):
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = ""
        completed = subprocess.run(
            [sys.executable, "-m", "src.cli", "examples/inputs/clear_answer.json",
             "--mode", "deepseek"], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stderr)["task_status"], "model_unavailable")


if __name__ == "__main__":
    unittest.main()
