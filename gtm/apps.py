# gtm/apps.py
from django.apps import AppConfig

class GtmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gtm"

    def ready(self):
        from . import signals  # noqa
