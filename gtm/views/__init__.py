"""Public surface of the gtm views package.

The former monolithic views.py is split into domain modules; this module
re-exports every view so ``from . import views`` / ``views.<name>`` call sites
(urls.py) and external imports keep working unchanged.
"""

from .helpers import (
    LEGEND,
    _get_template,
    _is_htmx,
    _remember_session,
    require_action_ownership,
    require_session_ownership,
)

# Score helpers historically re-exported from gtm.views (used by dashboard.views,
# gtm.ai_chat, gtm.agent_services).
from ..services import _band_for_score, _compute_scores

from .assessment import (
    assessment_step,
    edit_assessment_intro,
    landing,
    resume_assessment,
    resume_latest,
    rewrite_context_note,
    start_assessment,
)
from .results import results
from .playbook import (
    download_report_pdf,
    enrichment_status,
    insight_status,
    next_moves_content,
    playbook,
    playbook_content_status,
    playbook_footer_status,
    playbook_status,
)
from .documents import (
    analyze_category_documents,
    analyze_delivery_documents,
    delete_category_document,
    delete_delivery_document,
    get_category_documents,
    get_delivery_documents,
    upload_category_document,
    upload_delivery_document,
    upload_strategic_evidence,
)
from .actions import (
    action_add,
    action_delete,
    action_toggle,
    action_update,
    cancel_assessment,
)
from .profile import history, logout_view, profile
