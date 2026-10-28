from django.core.management.base import BaseCommand

from gtm.automations import run_due_automations, sweep_stale_runs


class Command(BaseCommand):
    help = (
        "Run every automation that's due, and fail agent runs whose worker died so they can be retried. "
        "Scheduled hourly (Cloud Run Job + Cloud Scheduler); safe to run more often or concurrently."
    )

    def handle(self, *args, **options):
        counts = run_due_automations()
        stale = sweep_stale_runs()
        self.stdout.write(
            f"Automations: {counts['acted']} acted, {counts['skipped']} skipped, {counts['failed']} failed. "
            f"Stale agent runs failed: {stale}."
        )
