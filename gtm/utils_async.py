"""
Helper for running fire-and-forget background work off the request thread.

The app kicks off Gemini calls (playbook generation, diagnostics, document
audits) in daemon threads so the HTTP response can return immediately while the
client polls for results via HTMX. Every such thread that touches the ORM opens
its own thread-local database connection, which Django will not clean up
automatically outside the request/response cycle — leaking a connection per
task. This helper centralises that lifecycle so every background task:

    * logs any unhandled exception (instead of dying silently), and
    * closes its database connection when it finishes.
"""

from threading import Thread

from django.db import close_old_connections

from .utils_logging import log_error


def run_in_background(target, *args, name: str = "", **kwargs) -> Thread:
    """Run ``target(*args, **kwargs)`` in a daemon thread.

    Any exception is logged via :func:`log_error`, and the thread's database
    connection is always closed on exit. Returns the started ``Thread`` (callers
    can ignore it).
    """
    label = name or getattr(target, "__name__", "background_task")

    def _wrapped():
        try:
            target(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - background tasks must not crash silently
            log_error(f"Background task: {label}", exc)
        finally:
            close_old_connections()

    thread = Thread(target=_wrapped, daemon=True, name=label)
    thread.start()
    return thread
