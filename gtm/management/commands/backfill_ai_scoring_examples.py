"""
Warm-start the AIScoringExample corpus (the AI-unavailable fallback's "scoring
memory") from documents that were already successfully analyzed by Gemini
before this feature existed. Without this, the fallback starts from zero and
falls back further to a fixed, low-confidence score until real usage rebuilds
the corpus organically.

Walks every DeliveryDocument/CategoryDocument with analysis_status='complete'
and a non-empty analysis_result, re-derives the NLP evidence passage each
question was scored against (same _build_nlp_evidence pipeline used live),
and captures an AIScoringExample per question — same mechanism as the live
capture step in delivery_analyzer.py/category_analyzer.py, just run once
retroactively.

Safe to run more than once: it only reads existing analysis_result data and
appends new AIScoringExample rows (which are themselves just anonymous
embedding+score pairs) — running it twice just means running it once again
next time it would run anyway, e.g. on more documents that have since
completed analysis. Use --dry-run to preview counts without writing.

Usage
-----
$ python manage.py backfill_ai_scoring_examples
$ python manage.py backfill_ai_scoring_examples --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from gtm.models import DeliveryDocument, CategoryDocument, AIScoringExample
from gtm.delivery_analyzer import (
    DELIVERY_QUESTIONS, _build_nlp_evidence, _capture_scoring_examples,
)
from gtm.category_analyzer import CATEGORY_QUESTIONS


class Command(BaseCommand):
    help = "Backfill the AI scoring-memory corpus from already-completed document analyses."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Preview how many examples would be captured without writing to the database.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        before = AIScoringExample.objects.count()

        delivery_docs = DeliveryDocument.objects.filter(
            analysis_status="complete"
        ).exclude(analysis_result={})
        for doc in delivery_docs:
            self._backfill_one(doc, DELIVERY_QUESTIONS, dry)

        for category, questions in CATEGORY_QUESTIONS.items():
            category_docs = CategoryDocument.objects.filter(
                category=category, analysis_status="complete"
            ).exclude(analysis_result={})
            for doc in category_docs:
                self._backfill_one(doc, questions, dry)

        if dry:
            self.stdout.write(self.style.WARNING(
                "Dry-run complete. Re-run without --dry-run to apply."
            ))
        else:
            after = AIScoringExample.objects.count()
            self.stdout.write(self.style.SUCCESS(
                f"Captured {after - before} scoring examples (corpus now has {after})."
            ))

    def _backfill_one(self, doc, questions, dry: bool):
        text = (doc.extracted_text or "").strip()
        result = doc.analysis_result or {}
        if not text or not result:
            return
        nlp_evidence = _build_nlp_evidence(text, questions)
        if dry:
            would_capture = sum(
                1 for q in questions
                if result.get(q["id_code"], {}).get("score") is not None
                and (nlp_evidence.get(q["id_code"], {}).get("semantic")
                     or nlp_evidence.get(q["id_code"], {}).get("tfidf"))
            )
            self.stdout.write(f"  {doc.original_filename}: would capture {would_capture} examples")
            return
        with transaction.atomic():
            _capture_scoring_examples(nlp_evidence, result, questions)
