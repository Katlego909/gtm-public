"""
Bulk-generate closed-beta invite codes. Run without --email to pre-generate
a batch of codes to hand out manually; pass --email to also send one
immediately via gtm/utils_email.py::send_beta_invite_email.
"""
from django.core.management.base import BaseCommand, CommandError

from gtm.models_beta import BetaInviteCode


class Command(BaseCommand):
    help = "Generate one or more closed-beta invite codes"

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=1, help="How many codes to generate (default 1)")
        parser.add_argument("--note", type=str, default="", help="Free-text note, e.g. \"Beta cohort 1\"")
        parser.add_argument("--email", type=str, default="", help="Email a single generated code immediately (requires --count 1)")

    def handle(self, *args, **options):
        count = options["count"]
        note = options["note"]
        email = options["email"]

        if email and count != 1:
            raise CommandError("--email can only be used with --count 1")

        codes = [BetaInviteCode.objects.create(note=note, email=email) for _ in range(count)]

        if email:
            codes[0].send_invite_email()
            self.stdout.write(self.style.SUCCESS(f"Generated and emailed code {codes[0].code} to {email}"))
        else:
            for invite in codes:
                self.stdout.write(self.style.SUCCESS(f"Generated code: {invite.code}"))
