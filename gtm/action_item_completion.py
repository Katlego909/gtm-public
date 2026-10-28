"""The tool kit an agent works with on one assessment's task.

Shared by agent runs (gtm/agent_runs.py), which wrap the write tools here
behind the human-approved plan: assessment data and the scoring-engine
diagnosis, resource search, uploaded-evidence search, read-only dashboard
context, the completing agent's own domain tools, and document tools that
record the id of anything created/edited -- so the caller can tell whether
real work actually happened, rather than trusting the model's own account.
"""

from typing import Any, List

from .agent_documents import (
    create_agent_document,
    edit_agent_document,
    get_agent_document,
    list_agent_documents,
    search_agent_documents,
    _snippet_around,
)
from .ai_chat import (
    _make_get_gtm_assessment_data_tool,
    _make_get_gtm_diagnosis_tool,
    _make_search_internal_resources_tool,
)
from .models import ActionItem

# The agent that works on a task when it hasn't been routed to a specific one.
DEFAULT_COMPLETION_AGENT_TYPE = "gtm_strategist"


def completion_agent_type(action_item: ActionItem) -> str:
    """The agent that works on `action_item` with "Complete with AI": the one
    it was routed to (assign_task_to_agent), else Charlie. Its persona and
    domain tools run the work, and its deliverables, its comment, and the
    "<name> is working on this" status on the card are all attributed to it."""
    from .agent_runtime import AGENT_DIRECTORY, current_agent_type

    routed = current_agent_type(action_item.assigned_agent_type or "")
    return routed if routed in AGENT_DIRECTORY else DEFAULT_COMPLETION_AGENT_TYPE


def build_task_tools(
    session, user, created_doc_ids: List[Any], agent_type: str = DEFAULT_COMPLETION_AGENT_TYPE, workspace=None,
) -> List[Any]:
    """Read tools plus document tools for work on `session`, as `agent_type`.
    `workspace` defaults to the session's; the completing agent's domain
    read tools are added when it's a workspace agent. The dashboard write
    tool (log_gap_measurement_from_chat) is excluded: no person said a
    number in chat here, so there's nothing legitimate for it to relay."""
    from .agent_dashboard_tools import build_dashboard_tools
    from .data_tools import build_data_tools
    from .workspace_agent_chat import _build_domain_tools

    workspace = workspace or session.workspace
    get_gtm_assessment_data = _make_get_gtm_assessment_data_tool(session)
    get_gtm_diagnosis = _make_get_gtm_diagnosis_tool(session)
    search_internal_resources = _make_search_internal_resources_tool(session)
    dashboard_read_tools = [
        tool for tool in build_dashboard_tools(workspace=workspace, session=session, user=user)
        if tool.__name__ != "log_gap_measurement_from_chat"
    ]

    def create_document(title: str, content: str, doc_type: str = "action_item_deliverable") -> str:
        """Create and save the concrete deliverable this task calls for
        (e.g. a policy, checklist, or standard) so it can be reviewed and
        exported as PDF, Word, or Markdown. `doc_type` should usually stay
        action_item_deliverable unless another type clearly fits better
        (client_summary, action_plan, roadmap, resource_brief, other).
        """
        doc = create_agent_document(
            agent_type=agent_type,
            title=title,
            content=content,
            doc_type=doc_type,
            workspace=workspace,
            session=session,
            user=user,
        )
        created_doc_ids.append(doc.pk)
        return f'Saved "{doc.title}" (ID {doc.pk}).'

    def edit_document(document_id: str, new_content: str) -> str:
        """Edit an existing document's content by its ID (use list_documents
        first if you don't already know the ID) -- use this instead of
        create_document when a deliverable for this task already exists.
        """
        doc = edit_agent_document(document_id, new_content, session=session)
        if not doc:
            return "I couldn't find a document with that ID for this assessment."
        if doc.pk not in created_doc_ids:
            created_doc_ids.append(doc.pk)
        return f'Updated "{doc.title}" to version {doc.version}.'

    def list_documents(doc_type: str = "") -> str:
        """List documents already saved for this assessment, so you can
        check whether a deliverable for this task already exists before
        drafting a new one.
        """
        docs = list(list_agent_documents(session=session, doc_type=doc_type or None)[:10])
        if not docs:
            return "No documents have been saved for this assessment yet."
        lines = ["Documents for this assessment:"]
        for d in docs:
            lines.append(f"- [{d.pk}] {d.title} ({d.get_doc_type_display()}, v{d.version})")
        return "\n".join(lines)

    def read_document(document_id: str) -> str:
        """Read an existing document's full content by its ID (use
        list_documents or search_documents first if you don't already know
        the ID). Use this before referencing, quoting, or building on a
        document that already covers this task.
        """
        doc = get_agent_document(document_id, session=session)
        if not doc:
            return "I couldn't find a document with that ID for this assessment."
        return f'"{doc.title}" ({doc.get_doc_type_display()}, v{doc.version}):\n\n{doc.content}'

    def search_documents(query: str) -> str:
        """Search saved documents for this assessment by title or content.
        Use this to check whether a deliverable for this task already
        exists before drafting a new one.
        """
        docs = list(search_agent_documents(query, session=session)[:10])
        if not docs:
            return f"No documents matching '{query}' found for this assessment."
        lines = [f"Documents matching '{query}':"]
        for d in docs:
            lines.append(f"- [{d.pk}] {d.title} ({d.get_doc_type_display()}, v{d.version})")
        return "\n".join(lines)

    def search_evidence(query: str) -> str:
        """Search the text extracted from evidence documents uploaded to
        auto-score this assessment (CSVs, PDFs, spreadsheets, contracts,
        etc.) for a keyword or phrase. Use this to ground the deliverable
        in real uploaded evidence, e.g. 'what did the uploaded contract
        say about SLAs'.
        """
        delivery_matches = list(
            session.delivery_docs.exclude(extracted_text="").filter(extracted_text__icontains=query)[:5]
        )
        category_matches = list(
            session.category_docs.exclude(extracted_text="").filter(extracted_text__icontains=query)[:5]
        )
        if not delivery_matches and not category_matches:
            return f"No uploaded evidence matching '{query}' found for this assessment."
        lines = [f"Evidence matching '{query}':"]
        for d in delivery_matches:
            snippet = _snippet_around(d.extracted_text, query)
            lines.append(f'- {d.original_filename} (Delivery): "{snippet}"')
        for d in category_matches:
            snippet = _snippet_around(d.extracted_text, query)
            lines.append(f'- {d.original_filename} ({d.get_category_display()}): "{snippet}"')
        return "\n".join(lines)

    tools = [
        get_gtm_assessment_data, get_gtm_diagnosis, search_internal_resources, create_document, edit_document,
        list_documents, read_document, search_documents, search_evidence, *dashboard_read_tools,
        *build_data_tools(session=session, workspace=workspace),
    ]
    # A workspace agent's domain tools can overlap the ones above (e.g. data
    # tools); Gemini rejects duplicate tool names, so the first one wins.
    names = {t.__name__ for t in tools}
    domain_tools = _build_domain_tools(agent_type, workspace, user=user) if workspace else []
    return tools + [t for t in domain_tools if t.__name__ not in names]


def _build_completion_tools(
    action_item: ActionItem, user, created_doc_ids: List[Any], agent_type: str = DEFAULT_COMPLETION_AGENT_TYPE,
) -> List[Any]:
    """build_task_tools scoped to one ActionItem's assessment and workspace."""
    return build_task_tools(
        action_item.session, user, created_doc_ids, agent_type=agent_type,
        workspace=action_item.workspace or action_item.session.workspace,
    )
