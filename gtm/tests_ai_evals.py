import json
from types import SimpleNamespace

from django.test import TestCase

from gtm import ai_services as ai
from gtm.ai_evals import CASES, case_scores, check_insight, check_playbook, run_evals
from gtm.models import AssessmentSession, Category, Question, ResultSnapshot
from gtm.scoring_engine import QUESTION_META


def _by_name(checks):
    return {c.name: c for c in checks}


class CheckTests(TestCase):
    def test_good_insight_passes_everything(self):
        text = ("Northwind Analytics has no written stage exit criteria. Deals sit in the same stage for weeks. "
                "Forecasts in HubSpot drift as a result.")
        checks = _by_name(check_insight(text, text, "Northwind Analytics", ai._strip_imperative_sentences))
        self.assertTrue(all(c.passed for c in checks.values()), checks)

    def test_insight_with_advice_fails_soft_check_only(self):
        raw = "Northwind Analytics lacks stage rules. Implement exit criteria now. The team should review weekly."
        filtered = ai._strip_imperative_sentences(raw)
        checks = _by_name(check_insight(raw, filtered, "Northwind Analytics", ai._strip_imperative_sentences))
        self.assertFalse(checks["raw_avoids_advice"].passed)
        self.assertFalse(checks["raw_avoids_advice"].hard)

    def test_descriptive_suggests_is_not_advice(self):
        text = "Atlas Payments has no response targets. This suggests immature sales operations."
        checks = _by_name(check_insight(text, text, "Atlas Payments", ai._strip_imperative_sentences))
        self.assertTrue(checks["raw_avoids_advice"].passed)

    def test_insight_hard_failures(self):
        raw = "The company has gaps \U0001F680 and the pipeline is"
        checks = _by_name(check_insight(raw, raw, "Northwind Analytics", ai._strip_imperative_sentences))
        for name in ("names_company", "no_emoji", "ends_on_full_sentence"):
            self.assertFalse(checks[name].passed, name)
            self.assertTrue(checks[name].hard, name)

    def test_playbook_must_cover_every_top_priority(self):
        body = json.dumps({"markdown_playbook": "# Fjord Health\nCustomer Health is the gap.", "risk_status": "High"})
        _, checks = check_playbook(body, "Fjord Health", ["Customer Health", "Retention"], ai._clean_json_response)
        checks = _by_name(checks)
        self.assertFalse(checks["covers_top_priorities"].passed)
        self.assertIn("Retention", checks["covers_top_priorities"].detail)

    def test_playbook_invalid_json_fails(self):
        _, checks = check_playbook("not json", "Fjord Health", [], ai._clean_json_response)
        self.assertFalse(_by_name(checks)["valid_json"].passed)


class CaseDataTests(TestCase):
    def test_every_case_scores_every_question_on_the_1_to_5_scale(self):
        for case in CASES:
            scores = case_scores(case)
            self.assertEqual(set(scores), set(QUESTION_META), case["key"])
            self.assertTrue(all(1 <= s <= 5 for s in scores.values()), case["key"])
            self.assertTrue(set(case["overrides"]) <= set(QUESTION_META), case["key"])


class FakeModels:
    """Answers like a well-behaved model, naming whatever company the prompt names."""

    def __init__(self):
        self.calls = 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        company = next(c["session"]["company_name"] for c in CASES if c["session"]["company_name"] in contents)
        if config is ai._PLAYBOOK_CONFIG:
            dims = " ".join(m["dimension"] for m in QUESTION_META.values())
            text = json.dumps({"markdown_playbook": f"# {company}\n{dims}.", "risk_status": "Medium"})
        else:
            text = f"{company} has no written process here. The gap shows up in reporting."
        return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(total_token_count=10))


class RunEvalsTests(TestCase):
    def setUp(self):
        categories = {}
        for qid, meta in QUESTION_META.items():
            if meta["pillar"] not in categories:
                categories[meta["pillar"]] = Category.objects.create(name=meta["pillar"])
            category = categories[meta["pillar"]]
            Question.objects.create(id_code=qid, category=category, text=f"Question about {meta['dimension']}")

    def test_full_run_with_fake_client_passes_and_leaves_no_rows(self):
        models = FakeModels()
        results, tokens = run_evals(SimpleNamespace(models=models), "fake-model")
        self.assertEqual(len(results), len(CASES) * 4)  # 3 insights + 1 playbook per case
        self.assertEqual(tokens, models.calls * 10)
        self.assertEqual([c for r in results for c in r.hard_failures], [])
        self.assertFalse(AssessmentSession.objects.exists())
        self.assertFalse(ResultSnapshot.objects.exists())

    def test_missing_seed_questions_is_a_clear_error(self):
        Question.objects.all().delete()
        with self.assertRaisesMessage(RuntimeError, "load_gtm_defaults"):
            run_evals(SimpleNamespace(models=FakeModels()), "fake-model")


class AICostReportTests(TestCase):
    def test_report_totals_recent_spend_by_feature(self):
        from io import StringIO
        from django.core.management import call_command
        from gtm.models_ai_credits import AICreditAccount, AICreditTransaction
        from gtm.models_workspace import Workspace

        account = AICreditAccount.objects.create(workspace=Workspace.objects.create(name="Cost Co"))
        AICreditTransaction.objects.create(account=account, feature="playbook", tokens_spent=1_000_000)
        out = StringIO()
        call_command("ai_cost_report", stdout=out)
        text = out.getvalue()
        self.assertIn("1,000,000 tokens, about $0.63-$1.51", text)
        self.assertIn("playbook", text)
