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

def log_ai_error(
    context: str,
    err: Exception,
    service: str = None,
    model: str = None,
    prompt: str = None,
    extra: dict = None,
):
    """
    Logs detailed AI error info with optional context and traceback.
    Example:
        log_ai_error("AI Playbook Generation", e, service="openai", model="gpt-4", prompt=prompt, extra={"session": session.uuid})
    """
    ai_extra = {
        "service": service,
        "model": model,
        "prompt": prompt,
    }
    if extra:
        ai_extra.update(extra)

    log_error(f"AI Error – {context}", err, extra=ai_extra)
