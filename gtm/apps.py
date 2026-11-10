# gtm/apps.py
from django.apps import AppConfig

class GtmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gtm"

    def ready(self):
        from . import signals  # noqa

        # The document-analysis ML models (sentence-transformer + spaCy) are
        # NOT pre-warmed here. Doing it in every gunicorn worker at boot
        # slowed cold starts, competed with the first requests for CPU, and
        # held a copy of the models per worker whether or not anyone analysed
        # a document. They load lazily on first use and stay cached per
        # process (see _get_sentence_transformer / _get_spacy_nlp in
        # gtm/delivery_analyzer.py).
