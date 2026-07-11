"""Public surface of the dashboard views package.

The former monolithic views.py is split into domain modules; this module
re-exports every view so ``from . import views`` / ``views.<name>`` call sites
(urls.py) keep working unchanged.
"""

# Private helpers historically imported from dashboard.views by other modules
# (dashboard.analytics imports these at function level).
from .helpers import _load_pending_gap_suggestions

from .gap_analysis import (
    accept_gap_suggestion,
    add_edit_gap_metric,
    delete_gap_metric,
    gap_analysis_table,
    gap_report,
    generate_gap_suggestions,
    get_gap_metric_row,
    refresh_gap_analysis_table,
    refresh_gap_suggestions,
    reject_gap_suggestion,
)
from .resources import (
    add_edit_resource,
    asset_audit_result,
    asset_library,
    delete_resource,
    refresh_resources,
    trigger_asset_audit,
)
from .analytics import (
    analytics,
    insight_export,
    insight_feedback,
    kpi_completed_items,
    kpi_pending_items,
    kpi_total_sessions,
)
from .hub import (
    agent_hub,
    dashboard,
    tasks_board,
    workspace_hub,
)
from .agent_api import (
    dashboard_agent_api,
    dashboard_agent_clear_api,
    dashboard_agent_context_api,
)
from .actions import (
    add_action_item_comment,
    add_edit_action_item,
    assign_action_item,
    delete_action_item,
    delete_action_item_comment,
    move_action_item,
    refresh_action_items,
    unassign_action_item,
)
from .pages import (
    create_workspace_dashboard,
    invite_to_workspace,
    notifications_panel,
    profile,
    settings_view,
)
