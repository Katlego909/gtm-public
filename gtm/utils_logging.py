import logging, traceback
from django.conf import settings

logger = logging.getLogger("gtm")

def log_error(context: str, err: Exception, extra=None):
    """
    Logs detailed error info with optional context and traceback.
    Example:
        log_error("AI Playbook Generation", e, {"session": session.uuid})
    """
    tb = traceback.format_exc()
    extra_info = f" | extra={extra}" if extra else ""
    message = f"[GTM ERROR] {context}: {type(err).__name__} – {err}{extra_info}\n{tb}"
    logger.error(message)

    # Optional: print to console in dev mode
    if getattr(settings, "DEBUG", True):
        print(message)
