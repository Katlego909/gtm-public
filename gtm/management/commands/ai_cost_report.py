"""
What AI usage has cost, from the token ledger (AICreditTransaction).

    python manage.py ai_cost_report             # last 30 days
    python manage.py ai_cost_report --days 90

The ledger records total tokens per call, not the input/output split, and
Gemini prices the two very differently. Costs are therefore shown as a range
using blended rates: LOW_BLEND for input-heavy work (agent turns and chat
re-send long context and write short replies) and HIGH_BLEND for
output-heavy work (playbooks). Read the real figure off the billing console;
this report says where the tokens go.

Prices: Vertex AI standard tier, gemini-2.5-flash, checked 2026-11-10
(input $0.30 / 1M tokens, output $2.50 / 1M). Update them when GEMINI_MODEL
or Google's prices change.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count, Sum
from django.utils import timezone

from gtm.models_ai_credits import AICreditAccount, AICreditTransaction

INPUT_PER_M = 0.30
OUTPUT_PER_M = 2.50
LOW_BLEND = 0.85 * INPUT_PER_M + 0.15 * OUTPUT_PER_M   # ~$0.63 per 1M tokens
HIGH_BLEND = 0.45 * INPUT_PER_M + 0.55 * OUTPUT_PER_M  # ~$1.51 per 1M tokens


def dollars(tokens):
    millions = (tokens or 0) / 1_000_000
    return f"${millions * LOW_BLEND:,.2f}-${millions * HIGH_BLEND:,.2f}"


class Command(BaseCommand):
    help = "Summarise AI token spend and its approximate dollar cost from the credit ledger."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30)

    def handle(self, *args, days, **options):
        since = timezone.now() - timedelta(days=days)
        qs = AICreditTransaction.objects.filter(created_at__gte=since)
        total = qs.aggregate(t=Sum("tokens_spent"))["t"] or 0
        accounts = list(qs.values("account").annotate(tokens=Sum("tokens_spent")).order_by("-tokens"))

        self.stdout.write(f"Last {days} days: {total:,} tokens, about {dollars(total)}, "
                          f"across {len(accounts)} account(s).")
        self.stdout.write("\nBy feature:")
        for row in qs.values("feature").annotate(tokens=Sum("tokens_spent"), calls=Count("id")).order_by("-tokens"):
            per_call = row["tokens"] // row["calls"] if row["calls"] else 0
            self.stdout.write(f"  {row['feature']:22} {row['tokens']:>12,} tokens  {row['calls']:>6} calls  "
                              f"{per_call:>7,}/call  {dollars(row['tokens'])}")
        if accounts:
            self.stdout.write("\nHeaviest accounts (tokens):")
            for row in accounts[:10]:
                self.stdout.write(f"  account {str(row['account'])[:8]}  {row['tokens']:>12,}  {dollars(row['tokens'])}")

        self.stdout.write("\nMost a fully used monthly allowance can cost:")
        for tier, budget in AICreditAccount.TIER_MONTHLY_TOKEN_BUDGET.items():
            self.stdout.write(f"  {tier:6} {budget:>12,} tokens  {dollars(budget)}")
