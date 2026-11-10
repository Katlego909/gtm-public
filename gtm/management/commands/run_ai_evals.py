"""
Run the fixed AI output test set against the live model (costs tokens).

    python manage.py run_ai_evals                          # current GEMINI_MODEL
    python manage.py run_ai_evals --model gemini-x.y-flash # try a different model
    python manage.py run_ai_evals --out ai_eval_results    # where the JSON goes

Exits non-zero on any hard failure. See gtm/ai_evals.py for the checks.
"""
import json
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from gtm.ai_evals import results_to_json, run_evals


class Command(BaseCommand):
    help = "Check AI insights and playbooks for five fixed companies against the live model."

    def add_arguments(self, parser):
        from gtm.ai_services import GEMINI_MODEL
        parser.add_argument("--model", default=GEMINI_MODEL)
        parser.add_argument("--out", default="ai_eval_results")

    def handle(self, *args, model, out, **options):
        from gtm.ai_services import _get_client
        client = _get_client()
        if client is None:
            raise CommandError("No Gemini client. Set GOOGLE_APPLICATION_CREDENTIALS (see CLAUDE.md).")

        self.stdout.write(f"Running AI evals on {model} ...")
        try:
            results, tokens = run_evals(client, model)
        except RuntimeError as exc:
            raise CommandError(str(exc))

        tally = defaultdict(lambda: [0, 0, False])  # (kind, check) -> [passed, total, hard]
        for r in results:
            for c in r.checks:
                row = tally[(r.kind, c.name)]
                row[0] += c.passed
                row[1] += 1
                row[2] = c.hard
        for (kind, name), (passed, total, hard) in sorted(tally.items()):
            label = "hard" if hard else "soft"
            self.stdout.write(f"  {kind:9} {name:26} {passed}/{total}  ({label})")

        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{timezone.now():%Y%m%d-%H%M%S}-{model}.json"
        path.write_text(json.dumps(results_to_json(results, model, tokens), indent=2), encoding="utf-8")
        self.stdout.write(f"Tokens used: {tokens:,}. Full outputs: {path}")

        failures = [(r, c) for r in results for c in r.hard_failures]
        if failures:
            for r, c in failures:
                self.stdout.write(self.style.ERROR(f"  FAIL {r.case} / {r.target} / {c.name} {c.detail}"))
            raise CommandError(f"{len(failures)} hard check(s) failed.")
        self.stdout.write(self.style.SUCCESS("All hard checks passed."))
