"""Nora (portfolio) and Theo (resource) were merged into Milo and Charlie.

Only live work is redirected to the successor: tasks routed to a retired
agent, runs not yet finished, and automations. Finished runs, comments,
documents, and chat history keep their original agent, so they still show
who actually wrote them (see agent_runtime.RETIRED_AGENTS)."""

from django.db import migrations

SUCCESSORS = {"portfolio": "insights", "resource": "gtm_strategist"}
UNFINISHED = ("planning", "awaiting_approval", "running", "waiting_for_input")


def redirect_live_work(apps, schema_editor):
    ActionItem = apps.get_model("gtm", "ActionItem")
    AgentRun = apps.get_model("gtm", "AgentRun")
    Automation = apps.get_model("gtm", "Automation")
    for retired, successor in SUCCESSORS.items():
        ActionItem.objects.filter(assigned_agent_type=retired).update(assigned_agent_type=successor)
        AgentRun.objects.filter(agent_type=retired, status__in=UNFINISHED).update(agent_type=successor)
        Automation.objects.filter(agent_type=retired).update(agent_type=successor)


class Migration(migrations.Migration):

    dependencies = [
        ("gtm", "0059_agent_engineer_automations"),
    ]

    operations = [
        migrations.RunPython(redirect_live_work, migrations.RunPython.noop),
    ]
