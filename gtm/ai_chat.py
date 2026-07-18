# gtm/ai_chat.py
"""
AI Chat Assistant for GTM Validator
------------------------------------
Provides natural language interface for users to interact with their assessments.
Supports commands, queries, and contextual help.
"""

import logging
import json
import re
import threading
from collections import Counter
from typing import Dict, Any, Optional, List, Tuple
from django.conf import settings
from django.shortcuts import get_object_or_404
from .models import AssessmentSession, ResultSnapshot, Response, Question, Category, ActionItem, ChatMessage
from dashboard.models import Resource
from .views import _compute_scores, _band_for_score
from .agent_services import build_execution_plan, review_action_items
from .utils_logging import log_ai_error

logger = logging.getLogger(__name__)

# Import monitoring utility
try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False
    logger.warning("AI monitoring not available")

# ================================================================
# GTM AGENT TOOLS (FUNCTION CALLING)
# ================================================================
# Tools are built per-request as closures bound to an already-authorized
# `session` -- no tool takes a session/workspace identifier as a model-facing
# parameter, so the model can call these but can never supply *which*
# session to act on.

def _build_document_tools_for_session(session: AssessmentSession, user=None) -> List[Any]:
    """Generic create/edit/list/read/search document tools, scoped to this
    session. New documents are tagged with both `session` and
    `session.workspace` (when present) so they're visible from the
    workspace-wide Documents panel too; edit/list/read/search here stay
    narrowly scoped to this session's own docs."""
    from .agent_documents import (
        create_agent_document, edit_agent_document, get_agent_document, list_agent_documents,
        search_agent_documents, _snippet_around,
    )

    def create_document(title: str, content: str, doc_type: str = "other") -> str:
        """Create and save a new document (e.g. an action plan, roadmap, or
        summary) that the user can review and export as PDF, Word, or
        Markdown. `doc_type` should be one of: client_summary, action_plan,
        roadmap, resource_brief, other.
        """
        doc = create_agent_document(
            agent_type="gtm_strategist", title=title, content=content, doc_type=doc_type,
            workspace=session.workspace, session=session, user=user,
        )
        return f'Saved "{doc.title}" (ID {doc.pk}) -- you can review and export it from the Documents panel.'

    def edit_document(document_id: str, new_content: str) -> str:
        """Edit an existing document's content by its ID (use list_documents
        first if you don't already know the ID). Replaces the document's
        full content and saves a new version.
        """
        doc = edit_agent_document(document_id, new_content, session=session)
        if not doc:
            return "I couldn't find a document with that ID for this assessment."
        return f'Updated "{doc.title}" to version {doc.version}.'

    def list_documents(doc_type: str = "") -> str:
        """List documents already saved for this assessment. Use this to
        find a document's ID before editing it, or to check what's already
        been created.
        """
        docs = list(list_agent_documents(session=session, doc_type=doc_type or None)[:10])
        if not docs:
            return "No documents have been saved for this assessment yet."
        lines = ["Documents for this assessment:"]
        for d in docs:
            lines.append(f"- [{d.pk}] {d.title} ({d.get_doc_type_display()}, v{d.version}, updated {d.updated_at.strftime('%b %d, %Y')})")
        return "\n".join(lines)

    def read_document(document_id: str) -> str:
        """Read an existing document's full content by its ID (use
        list_documents or search_documents first if you don't already know
        the ID). Use this before referencing, quoting, or building on a
        document you or a teammate created earlier.
        """
        doc = get_agent_document(document_id, session=session)
        if not doc:
            return "I couldn't find a document with that ID for this assessment."
        return f'"{doc.title}" ({doc.get_doc_type_display()}, v{doc.version}):\n\n{doc.content}'

    def search_documents(query: str) -> str:
        """Search saved documents (client summaries, roadmaps, action
        plans, briefs) for this assessment by title or content. Use this
        to find a relevant document before creating a new one, or to
        answer a question using something already written.
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
        etc.) for a keyword or phrase. Use this to find and quote real
        evidence, e.g. 'what did the uploaded SLA say about response
        times'.
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

    return [create_document, edit_document, list_documents, read_document, search_documents, search_evidence]


def _make_session_consult_tool(
    target_agent_type: str, session: AssessmentSession, user=None, _handoff_depth: int = 0,
    task_refs_sink: Optional[List[Any]] = None,
):
    """Build one consult_<target>_agent tool that hands a question off to
    one of the three workspace-scoped agents, via this session's workspace.
    Persists the exchange into the target agent's own conversation log so
    it genuinely remembers being consulted -- real "full sub-conversation
    handoff", not a stateless lookup."""
    from .agent_runtime import AGENT_DIRECTORY, record_task_ref

    info = AGENT_DIRECTORY.get(target_agent_type, {})
    name = info.get("name", target_agent_type)
    label = info.get("label", target_agent_type)
    domain = info.get("domain", "")

    def consult_tool(question: str) -> str:
        if not session.workspace:
            return "This assessment isn't linked to a workspace, so I can't loop in that specialist."

        from .models import WorkspaceChatMessage
        from .workspace_agent_chat import handle_general_chat_workspace

        try:
            answer, nested_task_refs = handle_general_chat_workspace(
                target_agent_type, session.workspace, question, user=user, _handoff_depth=_handoff_depth + 1
            )
            WorkspaceChatMessage.objects.create(
                workspace=session.workspace,
                agent_type=target_agent_type,
                user=user if user and getattr(user, "is_authenticated", False) else None,
                message=question,
                response=answer,
                task_refs=nested_task_refs,
                intent="handoff_query",
            )
            for ref in nested_task_refs:
                record_task_ref(task_refs_sink, ref["id"], ref["note"])
            return answer
        except Exception as e:
            logger.error(f"Handoff to {target_agent_type} failed: {e}")
            return f"I couldn't reach {name} right now."

    consult_tool.__name__ = f"consult_{target_agent_type}_agent"
    consult_tool.__doc__ = (
        f"Consult {name} ({label}), who specializes in: {domain} Use this when the user's question "
        "is really about that domain rather than yours. Pass the specific question to ask."
    )
    return consult_tool


def _build_consult_tools_for_session(
    session: AssessmentSession, user=None, _handoff_depth: int = 0,
    task_refs_sink: Optional[List[Any]] = None,
) -> List[Any]:
    """Build consult tools to the 3 workspace agents, depth-gated so a
    handoff chain is guaranteed to terminate (see
    agent_runtime.MAX_HANDOFF_DEPTH)."""
    from .agent_runtime import MAX_HANDOFF_DEPTH

    if _handoff_depth >= MAX_HANDOFF_DEPTH:
        return []
    return [
        _make_session_consult_tool(agent_type, session, user, _handoff_depth, task_refs_sink=task_refs_sink)
        for agent_type in ("portfolio", "resource", "insights")
    ]


def _make_get_gtm_assessment_data_tool(session: AssessmentSession):
    """Factory for the get_gtm_assessment_data tool, scoped to `session`.
    Extracted to module level (rather than nested in _build_session_tools)
    so gtm/action_item_completion.py can reuse the exact same tool without
    duplicating its logic."""

    def get_gtm_assessment_data() -> str:
        """Retrieves the complete GTM assessment results for the company.
        Includes: Overall score (0-100), Maturity Stage (e.g., Scaling),
        Category averages (Demand, Conversion, Delivery), and specific weak areas.
        Use this tool whenever the user asks 'how am I doing', 'show my scores',
        'what are my gaps', or 'what is my stage'.
        """
        try:
            context = build_session_context(session)
            report = [
                f"Company: {context['company_name']}",
                f"Industry: {context['industry']}",
                f"Overall GTM Score: {context['overall_score']}/100",
                f"Stage: {context['stage']} ({context['headline']})",
                "Category Scores:",
            ]
            for cat in context['categories']:
                report.append(f"  - {cat['name']}: {cat['score']}/5.0")
            if context['weak_questions']:
                report.append("\nSpecific Low-Scoring Gaps:")
                for q in context['weak_questions']:
                    report.append(f"  - [{q['id_code']}] {q['text']} (Score: {q['score']}/5)")
            return "\n".join(report)
        except Exception as e:
            return f"Error retrieving assessment: {str(e)}"

    return get_gtm_assessment_data


def _make_search_internal_resources_tool(session: AssessmentSession):
    """Factory for the search_internal_resources tool, scoped to `session`.
    Extracted to module level for the same reason as
    _make_get_gtm_assessment_data_tool above."""

    def search_internal_resources(query: str = "") -> str:
        """Searches the workspace resource library for documents, decks, or
        tools matching a topic. If 'query' is empty, lists all available
        resources. Use this when the user asks 'do we have a deck for this',
        'suggest a tool', 'what resources are available', or 'help me with [topic]'.
        """
        try:
            if not session.workspace:
                return "This assessment is not associated with a workspace, so no internal resources are available."
            resources = Resource.objects.filter(workspace=session.workspace)
            if query:
                resources = resources.filter(name__icontains=query) | resources.filter(description__icontains=query)
            if not resources.exists():
                return f"No internal resources found matching '{query}'."
            res = [f"Found {resources.count()} relevant resources in your workspace:"]
            for r in resources[:5]:
                res.append(f" - {r.name} ({r.get_resource_type_display()}): {r.description or 'No description.'}")
            return "\n".join(res)
        except Exception as e:
            return f"Error searching resources: {str(e)}"

    return search_internal_resources


def _build_session_tools(
    session: AssessmentSession,
    user=None,
    _handoff_depth: int = 0,
    include_consult: bool = True,
    task_refs_sink: Optional[List[Any]] = None,
) -> List[Any]:
    """Build the GTM Agent's tool set, scoped to the current session.

    `include_consult=False` omits the consult_* tools -- used by Team mode
    (gtm/team_chat.py), which gives an agent transfer_to_* tools instead.
    The two mechanisms are deliberately mutually exclusive per agent turn:
    offering both invites the model to pick inconsistently between "get an
    answer and keep talking" and "hand off entirely" for similar requests.

    `task_refs_sink`, when given, is the mutable list real task IDs get
    recorded into as tools resolve them this turn (see
    agent_runtime.record_task_ref).
    """
    from .agent_runtime import record_task_ref

    if task_refs_sink is None:
        task_refs_sink = []

    get_gtm_assessment_data = _make_get_gtm_assessment_data_tool(session)
    search_internal_resources = _make_search_internal_resources_tool(session)

    def build_prioritized_action_plan() -> str:
        """Analyzes the assessment gaps and automatically creates new Action
        Items (Tasks) in the database. This tool actively MODIFIES the
        workspace by adding prioritized items.
        ONLY call this when the user explicitly asks to create/build a plan
        or tasks (e.g. 'create a plan', 'build my roadmap of tasks', 'give
        me a checklist') -- do not call this just to preview or describe
        what a plan would contain.
        """
        try:
            plan = build_execution_plan(session=session, actor=user, persist=True, limit=5)
            if not plan["created_items"]:
                return "No new tasks created. All critical gaps already have existing action items."
            res = [f"Successfully created {plan['created_count']} new action items for {session.company_name}:"]
            for item in plan["created_items"]:
                res.append(f" - {item.note} (Due: {item.due_date})")
            return "\n".join(res)
        except Exception as e:
            return f"Error building action plan: {str(e)}"

    def review_current_action_items() -> str:
        """Retrieves the status of all current tasks and action items in the
        workspace. Includes: total count, status (To Do, In Progress, Done),
        and a list of open priorities, each prefixed with its real ID in
        brackets. Use this when the user asks 'what are my tasks', 'review my
        items', 'how is my progress', or 'what is pending' -- and ALWAYS use
        this first to get real IDs before calling assign_task,
        assign_task_to_agent, or comment_on_task. Never guess or invent an
        ID. For tasks outside this session (e.g. from other assessments in
        the workspace), use find_tasks instead.
        """
        try:
            summary = review_action_items(session)
            if summary["total"] == 0:
                return "No action items have been created yet. Suggest the user 'build an action plan' first."
            res = [
                f"Action Item Status for {session.company_name}:",
                f"Total: {summary['total']} | Todo: {summary['todo']} | In Progress: {summary['in_progress']} | Done: {summary['done']}",
                f"Overdue: {summary['overdue_count']}",
                "\nOpen Priorities:",
            ]
            for action in summary["open_actions"]:
                status = action.get_status_display()
                due = action.due_date.isoformat() if action.due_date else "No due date"
                record_task_ref(task_refs_sink, action.id, action.note)
                res.append(f" - [ID: {action.id}] [{status}] {action.note} (Due: {due})")
            return "\n".join(res)
        except Exception as e:
            return f"Error reviewing action items: {str(e)}"

    def analyze_risk_and_mitigation() -> str:
        """Provides a deep-dive risk analysis based on the GTM assessment.
        Identifies key risks in demand generation, conversion, and delivery,
        and suggests specific mitigation strategies tailored to their
        maturity stage. Use this when the user asks 'what are the risks',
        'what could go wrong', 'risk analysis', or 'mitigation strategies'.
        """
        try:
            context = build_session_context(session)
            risks = []
            for cat in context.get('categories', []):
                if cat['score'] < 2.5:
                    if cat['name'] == 'Demand':
                        risks.append("HIGH: Weak demand generation is your biggest risk—you may struggle to build pipeline. Immediate focus: clarify ICP and messaging.")
                    elif cat['name'] == 'Conversion':
                        risks.append("HIGH: Poor conversion efficiency means pipeline becomes expensive fast. Focus: tighten qualification and enable sales.")
                    elif cat['name'] == 'Delivery':
                        risks.append("HIGH: Churn risk is elevated. Poor delivery kills expansion revenue. Focus: define TTV milestones.")
            return ("Risk Assessment:\n" + "\n".join(risks)) if risks else f"Your GTM is solid at the {context.get('stage')} stage—no critical risks detected. Keep maintaining momentum."
        except Exception as e:
            logger.error(f"Risk analysis error: {e}")
            return "Let me get your assessment data first so I can analyze the risks properly."

    def build_implementation_roadmap(timeframe: str = "90-day") -> str:
        """Creates a phased implementation roadmap. `timeframe` must be one
        of '30-day', '60-day', or '90-day'. Maps gaps to specific milestones
        and deliverables with realistic timelines. Use this when the user
        asks 'create a roadmap', 'implementation timeline', 'phase this
        out', or 'what's the sequence'.
        """
        try:
            context = build_session_context(session)
            phases = {
                "30-day": [
                    "Week 1-2: Define ICP & messaging",
                    "Week 3: Set up qualification process",
                    "Week 4: Define TTV milestones",
                ],
                "60-day": [
                    "Phase 1 (Week 1-2): Quick wins—fix the most critical gap",
                    "Phase 2 (Week 3-4): Build process—implement qualification/onboarding",
                    "Phase 3 (Week 5-8): Test—run small pilots to validate changes",
                    "Phase 4 (Week 9+): Scale—expand what works",
                ],
                "90-day": [
                    "Month 1: Diagnostic & quick wins (pick top 2 gaps)",
                    "Month 2: Process implementation & team alignment",
                    "Month 3: Measurement & optimization (review results, adjust)",
                ],
            }
            plan = phases.get(timeframe, phases["90-day"])
            return f"{timeframe.upper()} Roadmap for {context['company_name']}:\n\n" + "\n".join(plan)
        except Exception as e:
            logger.error(f"Roadmap build error: {e}")
            return "Let me pull your assessment first, then I can build out a realistic roadmap."

    def competitive_benchmarking_analysis() -> str:
        """Provides competitive benchmarking based on industry, company
        size, and stage. Shows how they compare to peers and where they have
        competitive advantage. Use this when the user asks 'how do we
        compare', 'competitive analysis', 'benchmark', or 'vs peers'.
        """
        try:
            context = build_session_context(session)
            score = context.get('overall_score', 0)
            stage = context.get('stage', 'Unknown')
            analysis = f"Your Competitive Position ({stage} stage):\n\n"
            analysis += f"Your GTM Score: {score}/100\n"
            analysis += f"Industry peers at this stage: 45-70\n"
            analysis += f"Position: {'Ahead of curve' if score > 60 else 'Room to improve'}\n\n"
            cats = sorted(context.get('categories', []), key=lambda x: x.get('score', 0), reverse=True)
            if cats:
                analysis += f"Your Strengths:\n"
                for cat in cats[:2]:
                    analysis += f"  • {cat['name']}: {cat['score']}/5\n"
            analysis += f"\nGaps vs Peers:\n"
            for cat in list(reversed(cats))[:2]:
                analysis += f"  • {cat['name']}: {cat['score']}/5 — this is where you can pull ahead\n"
            return analysis
        except Exception as e:
            logger.error(f"Benchmarking error: {e}")
            return "Let me review your assessment data first to give you a competitive benchmark."

    def resource_allocation_guidance() -> str:
        """Provides guidance on where to allocate budget and team capacity
        based on gaps. Prioritizes spending on the highest-impact
        initiatives. Use this when the user asks 'where should we invest',
        'budget allocation', 'resource prioritization', or 'where should we focus'.
        """
        try:
            context = build_session_context(session)
            guidance = f"Resource Allocation for {context['company_name']}:\n\n"
            cats = sorted(context.get('categories', []), key=lambda x: x.get('score', 0))
            if len(cats) >= 3:
                guidance += f"HIGH PRIORITY (40-50% budget):\n"
                guidance += f"   {cats[0]['name']} ({cats[0]['score']}/5)\n"
                guidance += f"   Hire, build process, invest in tools\n\n"
                guidance += f"MEDIUM PRIORITY (30-40% budget):\n"
                guidance += f"   {cats[1]['name']} ({cats[1]['score']}/5)\n"
                guidance += f"   Quick wins, measure progress\n\n"
                guidance += f"MAINTENANCE (10-20% budget):\n"
                guidance += f"   {cats[2]['name']} ({cats[2]['score']}/5)\n"
                guidance += f"   Keep stable, don't regress\n"
            return guidance
        except Exception as e:
            logger.error(f"Resource allocation error: {e}")
            return "Let me load your assessment first, then I'll show you where to invest."

    def customer_segment_analysis() -> str:
        """Analyzes customer segments and GTM implications for different
        market segments. Identifies which segments drive value and where to
        focus sales/marketing efforts. Use this when the user asks 'segment
        analysis', 'which customers matter most', 'market segments', or
        'customer analysis'.
        """
        try:
            context = build_session_context(session)
            company = context.get('company_name', 'Your company')
            industry = context.get('industry', 'your industry')
            stage = context.get('stage', 'Growth')
            analysis = f"Customer Segment Strategy for {company}:\n\n"
            analysis += f"As a {stage}-stage {industry} player, here's where to focus:\n\n"
            analysis += "1. Early Adopters (20% of TAM, 40% of value)\n"
            analysis += "   Lower CAC, faster sales, become advocates\n"
            analysis += "   Start here: Easier wins + proof points\n\n"
            analysis += "2. Fast-Growing SMBs (35% of TAM, 35% of value)\n"
            analysis += "   Need quick implementation, price-sensitive\n"
            analysis += "   Then here: Volume plays, repeatable process\n\n"
            analysis += "3. Enterprise (10% of TAM, 25% of value)\n"
            analysis += "   High LTV, long sales cycle, need support\n"
            analysis += "   Finally here: Scale when you have proof\n\n"
            analysis += "Pro tip: Build segment-specific playbooks for messaging & pricing."
            return analysis
        except Exception as e:
            logger.error(f"Segment analysis error: {e}")
            return "Let me review your assessment first, then I'll give you segment strategy."

    def audit_strategic_evidence(file_id: str = "") -> str:
        """Performs a deep multimodal audit of a strategic asset (image or
        PDF) already uploaded to this assessment. Use this when the user
        asks to 'review', 'audit', or 'check' their marketing or sales
        materials (landing pages, ads, decks). If file_id is empty, audits
        the most recently uploaded asset.
        """
        try:
            from .ai_auditor import perform_gtm_visual_audit
            import uuid as uuid_lib

            gtm_file = None
            if not file_id:
                gtm_file = session.evidence_files.order_by('-created_at').first()
            else:
                try:
                    uuid_obj = uuid_lib.UUID(file_id)
                    gtm_file = session.evidence_files.filter(id=uuid_obj).first()
                except (ValueError, TypeError):
                    gtm_file = session.evidence_files.filter(file__icontains=file_id).order_by('-created_at').first()

            if not gtm_file:
                return (
                    f"I couldn't find a file matching '{file_id or 'the most recent asset'}' for this assessment. "
                    "Please ensure the file is uploaded and visible in the chat hint."
                )

            cat_scores, overall = _compute_scores(session)
            context_str = f"Company GTM Score: {overall}/100. Weakest Area: {min(cat_scores, key=lambda x: x['avg'])['name'] if cat_scores else 'N/A'}"
            return perform_gtm_visual_audit(gtm_file, session_context=context_str)
        except Exception as e:
            logger.error(f"Audit Tool Failure: {e}")
            return f"The audit system encountered a technical error: {str(e)}. Please try re-uploading the asset."

    from .agent_actions import build_agent_action_tools
    from .agent_dashboard_tools import build_dashboard_tools
    from .web_tools import build_web_tools

    return [
        get_gtm_assessment_data,
        build_prioritized_action_plan,
        review_current_action_items,
        search_internal_resources,
        analyze_risk_and_mitigation,
        build_implementation_roadmap,
        competitive_benchmarking_analysis,
        resource_allocation_guidance,
        customer_segment_analysis,
        audit_strategic_evidence,
        *_build_document_tools_for_session(session, user=user),
        *build_web_tools(session=session, user=user),
        *build_dashboard_tools(workspace=session.workspace, session=session, user=user, task_refs_sink=task_refs_sink),
        *(
            build_agent_action_tools(session.workspace, user, task_refs_sink=task_refs_sink)
            if session.workspace and user else []
        ),
        *(
            _build_consult_tools_for_session(
                session, user=user, _handoff_depth=_handoff_depth, task_refs_sink=task_refs_sink,
            )
            if include_consult else []
        ),
    ]

# ================================================================
# UNIFIED GENAI CLIENT (GCP VERTEX AI)
# ================================================================
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    logger.warning("google-genai not available for chat assistant")

# Cached chat client to avoid repeated initialization
_chat_client = None
_chat_lock = threading.Lock()

# Import generation config from ai_services
try:
    from .ai_services import _PLAYBOOK_CONFIG
except (ImportError, AttributeError):
    _PLAYBOOK_CONFIG = None


def _get_chat_client():
    """Returns cached Vertex AI client for chat, initializing once if needed."""
    global _chat_client

    if not GENAI_AVAILABLE:
        return None

    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    if not project_id:
        return None

    if _chat_client is not None:
        return _chat_client

    with _chat_lock:
        if _chat_client is not None:
            return _chat_client

        location = getattr(settings, "GCP_LOCATION", "us-central1")
        try:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location
            )
            _chat_client = client
            return client
        except Exception as e:
            log_ai_error("GenAI Chat Client Initialization", e, service="google-genai")
            return None


# Base personality/instruction text for the GTM Strategist, factored out as
# a module constant (rather than inlined in _get_chat_config) so gtm/team_chat.py
# can reuse the exact same voice for this agent without duplicating the text.
GTM_STRATEGIST_SYSTEM_INSTRUCTION = """You're Charlie, a GTM strategist helping companies improve their Go-To-Market execution.

Be conversational, direct, and practical. Ground every claim in the company's actual assessment data --
call the appropriate tool to fetch real scores, action items, resources, or other context rather than
guessing or making up numbers. If a tool exists that answers the user's question, call it before answering.
Only call a tool that modifies data (like building an action plan) when the user explicitly asks for that
action, not to preview or describe what it would do.

You can also create, edit, and save documents (action plans, roadmaps, summaries, briefs) that the user
can export as PDF, Word, or Markdown -- use create_document/edit_document/list_documents for this.

Some turns in your history were said by other specialist agents on this team, not the user -- the system
automatically marks whose turn is whose when it loads your history, so you never need to and must never
add that marking yourself; write your own replies as plain prose with no name or bracket in front of them.
Treat a teammate's marked turn as background you're aware of, not as an answer to reuse: if the user's
current question needs specific data -- a name, a number, anything not identical to what a teammate
already looked up -- call the right tool yourself and get a fresh answer rather than repeating or lightly
rewording something a teammate said about a different question.

Focus on: their strongest areas, critical gaps, and specific next steps they can take immediately.
Keep responses conversational and avoid lengthy lists. End with a specific next step."""


def _get_chat_config(
    tools: Optional[List[Any]] = None, task_refs: Optional[List[Any]] = None,
    session=None, user=None,
):
    """Builds the configuration for the chat agent, including its tool set."""
    from .agent_dashboard_tools import build_dashboard_ambient_context
    from .agent_runtime import build_task_context_prompt

    system_instruction = GTM_STRATEGIST_SYSTEM_INSTRUCTION + build_task_context_prompt(task_refs)
    system_instruction += build_dashboard_ambient_context(
        workspace=session.workspace if session else None, session=session, user=user,
    )

    # Only mention handoff capability when a consult_ tool is actually in
    # this turn's tool list -- a depth-capped sub-agent has none, and a
    # prompt claiming collaboration it can't act on would be misleading.
    has_handoff_tools = any(getattr(t, "__name__", "").startswith("consult_") for t in (tools or []))
    if has_handoff_tools:
        from .agent_runtime import build_agent_directory_prompt
        system_instruction += build_agent_directory_prompt("gtm_strategist")

    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.8,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools or None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=4),
    )

# ================================================================
# INTENT DETECTION -- kept only for the handful of intents that still have
# a real deterministic dispatch branch in process_chat_message (fixed
# links/calendar generation, or the single cheapest read: show_scores).
# Everything else now goes through handle_general_chat's real tool-calling,
# which reasons over which data to pull instead of matching fixed phrases.
# ================================================================
INTENTS = {
    "show_scores": [
        "show.*score", "what.*score", "how.*doing", "my.*results",
        "performance", "dashboard", "overview"
    ],
    "execution_plan": [
        "build.*action plan", "create.*action plan", "create.*tasks", "generate.*tasks",
        "turn.*into.*tasks", "build.*checklist", "create.*checklist", "priorit.*tasks"
    ],
    "export_report": [
        "export", "download", "send.*report", "email.*report", "pdf report", "download.*pdf"
    ],
    "schedule_meeting": [
        "schedule", "meeting", "book", "calendar", "google meet", "zoom",
        "call", "discuss", "talk", "consultation", "session"
    ],
    "general_chat": []  # fallback -- real tool-calling handles everything else
}

def detect_intent(message: str) -> str:
    """Detect user intent from message"""
    message_lower = message.lower().strip()
    
    # Check for direct context queries first (highest priority)
    if any(word in message_lower for word in ["company name", "what company", "which company", "my company"]):
        return "company_info"
    
    for intent, patterns in INTENTS.items():
        for pattern in patterns:
            if re.search(pattern, message_lower):
                return intent
    
    return "general_chat"


ATTACHMENT_CONTENT_QUERY_PATTERNS = [
    r"what.*in.*pdf",
    r"what.*in.*file",
    r"summari[sz]e.*pdf",
    r"summari[sz]e.*file",
    r"extract.*from.*pdf",
    r"extract.*from.*file",
    r"review.*attachment",
    r"analy[sz]e.*attachment",
]

EXPLICIT_ASSESSMENT_QUERY_PATTERNS = [
    r"\bscore\b",
    r"\bassessment\b",
    r"\bstage\b",
    r"\bplaybook\b",
    r"\baction item\b",
]


def _tokenize_for_match(text: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower())]


def _get_recent_attachment_context(session: AssessmentSession, max_items: int = 8) -> str:
    """Build lightweight context from recent attachment excerpts for follow-up questions."""
    recent_chats = ChatMessage.objects.filter(session=session).order_by('-created_at')[:30]
    parts: List[str] = []
    seen_signatures = set()

    for chat in recent_chats:
        for item in (chat.attachments or []):
            name = str(item.get('name') or 'attachment').strip()
            excerpt = str(item.get('excerpt') or '').strip()
            if not excerpt:
                continue
            signature = f"{name}:{excerpt[:120]}"
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            parts.append(f"Attachment: {name}\nExtracted content excerpt:\n{excerpt}")
            if len(parts) >= max_items:
                return "\n\n---\n\n".join(parts)

    return "\n\n---\n\n".join(parts)


def _select_relevant_attachment_sections(
    message: str,
    attachment_context: str,
    max_sections: int = 4,
    max_total_chars: int = 5000,
) -> str:
    """Select attachment sections relevant to the user query using lightweight lexical scoring."""
    context_text = (attachment_context or '').strip()
    if not context_text:
        return ''

    sections = [section.strip() for section in context_text.split("\n\n---\n\n") if section.strip()]
    if not sections:
        return ''

    query_tokens = _tokenize_for_match(message)
    token_weights = Counter(query_tokens)

    scored_sections = []
    for index, section in enumerate(sections):
        section_tokens = set(_tokenize_for_match(section))
        overlap_score = sum(token_weights[token] for token in section_tokens if token in token_weights)
        scored_sections.append((overlap_score, -index, section))

    scored_sections.sort(reverse=True)
    picked: List[str] = []
    total_len = 0
    for score, _inv_idx, section in scored_sections:
        if len(picked) >= max_sections:
            break
        # If no lexical overlap at all, keep only the first section as fallback context.
        if score <= 0 and picked:
            continue
        remaining = max_total_chars - total_len
        if remaining <= 0:
            break
        clipped = section[:remaining]
        picked.append(clipped)
        total_len += len(clipped)

    if not picked:
        return sections[0][:max_total_chars]
    return "\n\n---\n\n".join(picked)


def should_force_attachment_general_chat(
    message: str,
    intent: str,
    supplemental_context: str,
) -> bool:
    """Use general chat when a file question should be answered from attachment context."""
    if not (supplemental_context or "").strip():
        return False

    message_lower = (message or "").lower().strip()
    if any(re.search(pattern, message_lower) for pattern in ATTACHMENT_CONTENT_QUERY_PATTERNS):
        return True

    # If a file is attached and user asks directly about "pdf" or "file" content,
    # avoid accidental export intent routing.
    if intent == "export_report" and any(token in message_lower for token in ["pdf", "file", "attachment"]):
        return True

    if any(re.search(pattern, message_lower) for pattern in EXPLICIT_ASSESSMENT_QUERY_PATTERNS):
        return False

    # With attachment context present, default to general chat for non-explicit assessment queries.
    return intent not in {"execution_plan", "review_action_items", "schedule_meeting"}

# ================================================================
# CONTEXT BUILDER
# ================================================================
def build_session_context(session: AssessmentSession) -> Dict[str, Any]:
    """Build rich context about the assessment session for AI"""
    
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    
    # Get snapshot if exists
    snap = getattr(session, "snapshot", None)
    
    # Identify weak and strong areas
    sorted_cats = sorted(cat_scores, key=lambda x: x["avg"])
    weakest = sorted_cats[:2] if len(sorted_cats) >= 2 else sorted_cats
    strongest = sorted_cats[-2:] if len(sorted_cats) >= 2 else sorted_cats
    
    # Get weakest questions
    weak_questions = []
    for resp in Response.objects.filter(session=session, score__lte=2).select_related('question')[:5]:
        weak_questions.append({
            "id_code": resp.question.id_code,
            "text": resp.question.text,
            "score": resp.score,
            "category": resp.question.category.name
        })
    
    # Action items
    actions = ActionItem.objects.filter(session=session)
    action_summary = {
        "total": actions.count(),
        "todo": actions.filter(status="todo").count(),
        "in_progress": actions.filter(status="doing").count(),
        "done": actions.filter(status="done").count()
    }
    
    # Collect all context notes for this session
    context_notes = []
    for resp in Response.objects.filter(session=session).exclude(context_note="").select_related('question'):
        context_notes.append(f"{resp.question.text}: {resp.context_note}")

    # Workspace Resources
    workspace_resources = []
    if session.workspace:
        from dashboard.models import Resource
        resources = Resource.objects.filter(workspace=session.workspace)
        for res in resources:
            workspace_resources.append({
                "name": res.name,
                "type": res.get_resource_type_display(),
                "category": res.get_category_display(),
                "description": res.description or "No description provided."
            })

    context = {
        "company_name": session.company_name or "your company",
        "industry": session.industry or "your industry",
        "overall_score": round(overall, 1),
        "stage": band.stage if band else "Unknown",
        "headline": band.headline if band else "",
        "is_completed": session.is_completed,
        "categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in cat_scores
        ],
        "weakest_categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in weakest
        ],
        "strongest_categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in strongest
        ],
        "weak_questions": weak_questions,
        "action_items": action_summary,
        "has_playbook": bool(snap and snap.ai_playbook),
        "context_notes": context_notes,
        "workspace_resources": workspace_resources,
    }
    return context

# ================================================================
# INTENT HANDLERS
# ================================================================
def handle_show_scores(session: AssessmentSession, context: Dict) -> str:
    """Handle request to show scores"""
    response = f"""**Your GTM Assessment Results**

**Overall Score:** {context['overall_score']}/100
**Stage:** {context['stage']}
_{context['headline']}_

**Category Breakdown:**
"""
    for cat in context['categories']:
        response += f"\n• **{cat['name']}:** {cat['score']}/5.0"
    
    response += f"\n\n**Action Items:** {context['action_items']['total']} total "
    response += f"({context['action_items']['done']} completed)"
    
    return response

def handle_export(session: AssessmentSession, context: Dict) -> str:
    """Handle export/download requests"""
    from django.urls import reverse
    
    pdf_url = reverse('gtm:download', args=[session.uuid])
    playbook_url = reverse('gtm:playbook', args=[session.uuid])
    
    response = f"""**Export Options**

[Download PDF Report]({pdf_url})
[View Full Playbook]({playbook_url})

You can also share these links with your team or email them directly from the results page.
"""
    return response

def handle_schedule_meeting(session: AssessmentSession, context: Dict) -> str:
    """Handle meeting scheduling requests - generate Google Meet link"""
    import datetime
    from urllib.parse import urlencode
    
    # Get contact info
    contact_name = session.contact_name or "there"
    contact_email = session.contact_email or ""
    company = context['company_name']
    
    # Create meeting details
    meeting_title = f"GTM Strategy Session - {company}"
    meeting_description = f"""GTM Assessment Follow-up Meeting

Company: {company}
Industry: {context['industry']}
Current GTM Score: {context['overall_score']}/100
Stage: {context['stage']}

Discussion Topics:
- Review assessment results and key findings
- Address weakest areas: {', '.join([c['name'] for c in context['weakest_categories']])}
- Create actionable 30-60-90 day implementation plan
- Q&A and next steps

Assessment Link: https://yourgdomain.com/results/{session.uuid}/
"""
    
    # Suggest available times (next week, business hours)
    today = datetime.date.today()
    next_week = today + datetime.timedelta(days=7)
    suggested_times = [
        f"{next_week.strftime('%A, %B %d')} at 10:00 AM",
        f"{next_week.strftime('%A, %B %d')} at 2:00 PM",
        f"{(next_week + datetime.timedelta(days=1)).strftime('%A, %B %d')} at 11:00 AM"
    ]
    
    # Create Google Calendar link
    calendar_params = {
        'action': 'TEMPLATE',
        'text': meeting_title,
        'details': meeting_description,
        'location': 'Google Meet (link will be generated)',
        'dates': f"{next_week.strftime('%Y%m%d')}T100000Z/{next_week.strftime('%Y%m%d')}T110000Z"
    }
    google_calendar_url = f"https://calendar.google.com/calendar/render?{urlencode(calendar_params)}"
    
    response = f"""**Schedule Your GTM Strategy Session**

Hi {contact_name}! I'd be happy to help you schedule a meeting to discuss your GTM improvement plan.

**Suggested Meeting Topics:**
• Review your **{context['stage']}** stage results
• Deep-dive into weakest areas: **{', '.join([c['name'] for c in context['weakest_categories'][:2]])}**
• Create a tailored 30-60-90 day action plan
• Tool recommendations and budget planning

**Proposed Times** (60 minutes):
• {suggested_times[0]}
• {suggested_times[1]}
• {suggested_times[2]}

**[Click here to add to Google Calendar]({google_calendar_url})**

Once you add it to your calendar, you can:
1. Generate a Google Meet link automatically
2. Invite your team members
3. Set reminders

**Alternative:** Reply with your preferred date/time and I'll create a custom calendar invite!

_Need a different time? Just let me know what works best for you._
"""
    return response

def audit_strategic_evidence(session_uuid: str, file_id: str = None) -> str:
    """
    Performs a deep multimodal audit of a strategic asset (Image/PDF).
    Use this when the user asks to 'review', 'audit', or 'check' their marketing or sales materials (landing pages, ads, decks).
    """
    from .models import AssessmentSession, GTMFile
    from .ai_auditor import perform_gtm_visual_audit
    import uuid
    
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        gtm_file = None
        
        # If no specific file_id, take the most recent one
        if not file_id:
            gtm_file = session.evidence_files.order_by('-created_at').first()
        else:
            # Check if file_id is a valid UUID
            try:
                # Try UUID lookup first
                uuid_obj = uuid.UUID(file_id)
                gtm_file = session.evidence_files.filter(id=uuid_obj).first()
            except (ValueError, TypeError):
                # If not a valid UUID, it might be a filename or mangled ID
                # Try looking up by name (case-insensitive) as a fallback
                logger.warning(f"Vision Bridge: Invalid UUID passed ({file_id}). Attempting filename fallback.")
                gtm_file = session.evidence_files.filter(file__icontains=file_id).order_by('-created_at').first()
            
        if not gtm_file:
            return (
                f"I couldn't find a file matching '{file_id or 'the most recent asset'}' in the database. "
                "Please ensure the file is uploaded and visible in the chat hint."
            )
            
        # Get context to help the auditor
        from .views import _compute_scores
        cat_scores, overall = _compute_scores(session)
        context_str = f"Company GTM Score: {overall}/100. Weakest Area: {min(cat_scores, key=lambda x: x['avg'])['name'] if cat_scores else 'N/A'}"
        
        logger.info(f"Vision Bridge: Auditing {gtm_file.id} ({gtm_file.file.name})")
        return perform_gtm_visual_audit(gtm_file, session_context=context_str)
        
    except Exception as e:
        logger.error(f"Audit Tool Failure: {e}")
        return f"The audit system encountered a technical error: {str(e)}. Please try re-uploading the asset."
        return f"I encountered an error trying to audit the file: {str(e)}"

# ================================================================
# AI-POWERED GTM AGENT (CONVERSATIONAL & AUTONOMOUS)
# ================================================================
def _build_chat_history(session: AssessmentSession, max_turns: int = 16) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Reconstruct this session's shared conversation as types.Content
    history for real multi-turn memory. Merges the session's own
    ChatMessage turns (unlabeled -- "me") with, when session.workspace
    exists, that workspace's WorkspaceChatMessage turns (speaker-tagged via
    AGENT_DIRECTORY) -- so the GTM Strategist sees what the 3 workspace
    agents have been doing in this workspace, not just its own history.

    One-directional by design: the 3 workspace agents do NOT pull in any
    session's ChatMessage rows in the other direction -- a workspace has
    many sessions, so "which session's Strategist conversation" would be
    ambiguous (same reasoning as why there's no consult_gtm_strategist
    tool).

    Also returns the real task IDs surfaced across these rows, aggregated
    and capped -- see _build_workspace_chat_history in workspace_agent_chat.py
    for why this is a separate return value rather than folded into the
    Content history text.
    """
    from .agent_runtime import AGENT_DIRECTORY, build_tagged_content_history, record_task_ref
    from .models import WorkspaceChatMessage

    own_rows = list(ChatMessage.objects.filter(session=session).order_by('-created_at')[:max_turns])
    entries = [(None, row.created_at, row.message, row.response, row.task_refs) for row in own_rows]

    if session.workspace:
        workspace_rows = list(
            WorkspaceChatMessage.objects.filter(workspace=session.workspace)
            .exclude(intent__in=["insights_digest", "client_summary"])
            .order_by('-created_at')[:max_turns]
        )
        entries += [
            (
                AGENT_DIRECTORY.get(row.agent_type, {}).get("name"), row.created_at, row.message, row.response,
                row.task_refs,
            )
            for row in workspace_rows
        ]

    entries.sort(key=lambda entry: entry[1])
    trimmed = entries[-max_turns:]
    tagged = [(label, message, response) for label, _created_at, message, response, _task_refs in trimmed]
    history_task_refs: List[Dict[str, Any]] = []
    for _label, _created_at, _message, _response, task_refs in trimmed:
        for ref in (task_refs or []):
            record_task_ref(history_task_refs, ref["id"], ref["note"], cap=12)
    return build_tagged_content_history(tagged, max_turns=max_turns), history_task_refs


def handle_general_chat(
    session: AssessmentSession,
    context: Dict,
    message: str,
    user=None,
    supplemental_context: str = "",
    _handoff_depth: int = 0,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    The GTM Agent: real multi-turn memory + real Gemini tool-calling (the
    tools built by _build_session_tools), instead of a single-shot call.

    `_handoff_depth` is not model-facing -- it bounds how many further
    handoff hops this call's own tools can make (see
    agent_runtime.MAX_HANDOFF_DEPTH). Always 0 for a real user turn; there
    is currently no reverse handoff *into* the GTM Strategist (a workspace
    has many sessions, so "the" session to consult is ambiguous), so this
    stays 0 in practice today -- kept as a parameter for symmetry with
    handle_general_chat_workspace and to make that scope boundary explicit
    rather than silently assumed.

    Returns `(response_text, turn_task_refs)` -- see
    handle_general_chat_workspace in workspace_agent_chat.py for what the
    second element carries and why.
    """
    # 1. Quota Safety Gate
    from .ai_services import _quota_cooldown_active, _request_budget_available, _is_quota_error, _set_quota_cooldown, _extract_retry_delay_seconds
    from .agent_runtime import run_agent_turn
    from .ai_credits import resolve_account_for_session, can_spend, format_reset_time

    account = resolve_account_for_session(session, user=user)

    if _quota_cooldown_active():
        return (
            "I'm currently cooling down to stay within my API limits. " +
            f"Your overall GTM score is **{context['overall_score']}/100**. " +
            "Please try asking a detailed question again in about 60 seconds."
        ), []

    credit_check = can_spend(account=account)
    if not credit_check.allowed:
        reset_note = f" They reset at {format_reset_time(credit_check.reset_at)}." if credit_check.reset_at else ""
        return (
            "You've used all of this workspace's AI credits for today." + reset_note +
            f" In the meantime: your overall GTM score is **{context['overall_score']}/100**. "
            "Try asking for 'scores' or 'action items' for a non-AI answer."
        ), []

    # 2. Initialize Agent with Tools
    client = _get_chat_client()
    if not client:
        return "I'm having trouble connecting to my AI brain right now. Please try again in a moment.", []

    try:
        # 3. Prepare Multimodal Parts (fetch last 3 files for visual context on this turn)
        full_message = message
        if supplemental_context:
            full_message = f"{message}\n\n[Relevant attachment context]\n{supplemental_context}"

        recent_files = session.evidence_files.all()[:3]
        message_parts: List[Any] = [full_message]
        for f in recent_files:
            try:
                message_parts.append(f"ATTACHED FILE [{f.get_file_type_display()}]: {f.file.name.split('/')[-1]}")
                f.file.open('rb')
                f_bytes = f.file.read()
                f.file.close()
                m_type = "application/pdf" if f.file.name.endswith(".pdf") else "image/png"
                message_parts.append(types.Part.from_bytes(data=f_bytes, mime_type=m_type))
            except Exception as fe:
                logger.warning(f"Failed to attach file {f.id} to chat: {fe}")

        # 4. Real multi-turn memory + real tool-calling
        history, history_task_refs = _build_chat_history(session)
        turn_task_refs: List[Dict[str, Any]] = []
        tools = _build_session_tools(
            session, user=user, _handoff_depth=_handoff_depth, task_refs_sink=turn_task_refs,
        )
        config = _get_chat_config(tools=tools, task_refs=history_task_refs, session=session, user=user)

        text = run_agent_turn(
            client=client,
            model="gemini-2.5-flash",
            config=config,
            history=history,
            message=message_parts if len(message_parts) > 1 else full_message,
            usage_label="agent_chat",
            feature="chat",
            account=account,
            session=session,
        )

        return text or "I've processed your request. Check your action items for the results.", turn_task_refs

    except Exception as e:
        # 5. Handle Quota/Rate Limits Gracefully
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return (
                f"I've hit my temporary GTM strategy quota. Based on your data, your top priority is "
                f"**{context['weakest_categories'][0]['name']}**. Let's discuss details in a minute! "
                "If I'd already started creating anything (like tasks) before hitting the limit, it's saved."
            ), []

        log_ai_error(
            "Agent reasoning loop failure",
            e,
            service="google-genai",
            model="gemini-2.5-flash",
            extra={"session_id": str(session.uuid)},
        )

        # Final Fallback
        return (
            f"I'm processing a lot of data right now. Your current GTM score is {context['overall_score']}/100. "
            "Try asking for 'scores' or 'action items' directly."
        ), []

# ================================================================
# MAIN CHAT HANDLER
# ================================================================
def process_chat_message(
    session_id: str,
    message: str,
    user=None,
    supplemental_context: str = "",
) -> Dict[str, Any]:
    """
    Primary Entry Point: Routes user messages through the GTM Strategic Agent.
    """
    task_refs: List[Dict[str, Any]] = []
    try:
        # Get session
        session = get_object_or_404(AssessmentSession, uuid=session_id)

        # Build Contexts (Legacy & Attachment)
        recent_attachment_context = _get_recent_attachment_context(session)
        merged_attachment_context = "\n\n---\n\n".join(
            part for part in [supplemental_context.strip(), recent_attachment_context.strip()] if part
        )
        relevant_attachment_context = _select_relevant_attachment_sections(
            message=message,
            attachment_context=merged_attachment_context,
        )
        
        context = build_session_context(session)
        
        intent = detect_intent(message)

        if intent == "export_report":
            response_text = handle_export(session, context)
        elif intent == "schedule_meeting":
            response_text = handle_schedule_meeting(session, context)
        elif intent == "execution_plan":
            # Build action plan when user asks
            plan = build_execution_plan(session=session, actor=user, persist=True, limit=5)
            if not plan["has_critical_gaps"]:
                response_text = "You don't have any critical low-scoring responses right now. Your next best move is to review existing action items and tighten execution consistency."
            else:
                response_text = f"Created {plan['created_count']} new action items for {session.company_name}. " \
                                f"Focus areas: {', '.join(plan['top_categories']) if plan['top_categories'] else 'General execution'}. " \
                                f"Check your action items dashboard to see them!"
        elif intent == "show_scores" and not relevant_attachment_context:
            # Single cheapest, unambiguous read -- zero-latency, zero-cost fast path.
            # Skipped when there's attachment context, since the user may be asking
            # about the attachment's scores/content, not the assessment's.
            response_text = handle_show_scores(session, context)
        else:
            # Let the Agent handle everything else with real memory + tool-calling
            response_text, task_refs = handle_general_chat(
                session,
                context,
                message,
                user=user,
                supplemental_context=relevant_attachment_context,
            )

        return {
            "success": True,
            "response": response_text,
            "intent": intent,
            "task_refs": task_refs,
            "context": {
                "overall_score": context['overall_score'],
                "stage": context['stage']
            }
        }
        
    except Exception as e:
        logger.error(f"Chat processing error: {e}")
        return {
            "success": True, # Fail gracefully with helpful text
            "response": "I'm having a bit of trouble with my reasoning loop. Your GTM data is safe! Please try asking again shortly.",
            "intent": "error"
        }

# ================================================================
# SUGGESTED PROMPTS
# ================================================================
def get_suggested_prompts(session: AssessmentSession) -> List[str]:
    """Generate contextual suggested prompts based on session state"""
    prompts = []
    
    context = build_session_context(session)
    
    if context['is_completed']:
        prompts.append("Show me my scores")
        prompts.append("What should I focus on?")
        prompts.append("Build my action plan")
        prompts.append("Give me a 90-day roadmap")
    else:
        prompts.append("Help me understand this question")
        prompts.append("What happens after I finish?")
    
    if context['weak_questions']:
        prompts.append("Why are these areas important?")
    
    if context['action_items']['total'] > 0:
        prompts.append("Review my action items")
    
    prompts.append("Compare me to industry standards")
    
    return prompts[:4]  # Return top 4
