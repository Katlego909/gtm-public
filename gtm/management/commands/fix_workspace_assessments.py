# gtm/management/commands/fix_workspace_assessments.py
"""
Management command to associate orphaned assessments with workspaces.
Usage: python manage.py fix_workspace_assessments
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from gtm.models import AssessmentSession
from gtm.models_workspace import WorkspaceMembership

User = get_user_model()


class Command(BaseCommand):
    help = 'Associate orphaned assessments with user workspaces'

    def handle(self, *args, **options):
        # Find assessments without workspace but with authenticated users
        orphaned = AssessmentSession.objects.filter(
            workspace__isnull=True,
            user__isnull=False
        )
        
        count = orphaned.count()
        self.stdout.write(f'Found {count} assessment(s) without workspace')
        
        fixed = 0
        for assessment in orphaned:
            # Get user's first active workspace
            membership = WorkspaceMembership.objects.filter(
                user=assessment.user,
                is_active=True
            ).first()
            
            if membership:
                assessment.workspace = membership.workspace
                assessment.save(update_fields=['workspace'])
                fixed += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Linked "{assessment.company_name or "Untitled"}" '
                        f'to workspace "{membership.workspace.name}"'
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f'No workspace found for user {assessment.user.username}'
                    )
                )
        
        self.stdout.write(
            self.style.SUCCESS(f'\nFixed {fixed} of {count} assessment(s)')
        )
