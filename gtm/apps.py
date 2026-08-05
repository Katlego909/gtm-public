# gtm/apps.py
import sys

from django.apps import AppConfig

class GtmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gtm"

    def ready(self):
        from . import signals  # noqa

        # Pre-warm the document-analysis ML models (sentence-transformer +
        # spaCy) once per worker process, so the first real "Analyze
        # Documents" request doesn't pay their multi-second cold-load cost.
        # Skipped under the test runner to avoid loading ~90MB of models on
        # every test run for a background task the tests never wait on.
        if "test" not in sys.argv:
            self._warm_document_analysis_models()

    @staticmethod
    def _warm_document_analysis_models():
        from .utils_async import run_in_background

        def _warm():
            from .delivery_analyzer import _get_sentence_transformer, _get_spacy_nlp
            _get_sentence_transformer()
            _get_spacy_nlp()

        run_in_background(_warm, name="warm_document_analysis_models")
