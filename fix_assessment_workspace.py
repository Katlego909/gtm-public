#!/usr/bin/env python
"""
Management script to associate existing assessments with user's workspaces.
Run this once to fix assessments that were completed before workspace integration.
"""

import os
import sys
import django

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
django.setup()

from django.contrib.auth.models import User
from gtm.models import AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

def fix_assessment_workspaces():
    """Associate orphaned assessments with user's default workspace."""
    
    # Find assessments without workspace but with authenticated users
    orphaned_assessments = AssessmentSession.objects.filter(
        workspace__isnull=True,
        user__isnull=False
    )
    
    print(f"Found {orphaned_assessments.count()} assessments without workspace association")
    
    fixed_count = 0
    
    for assessment in orphaned_assessments:
        # Get user's first workspace
        membership = WorkspaceMembership.objects.filter(
            user=assessment.user,
            is_active=True
        ).first()
        
        if membership:
            assessment.workspace = membership.workspace
            assessment.save(update_fields=['workspace'])
            fixed_count += 1
            print(f"✓ Fixed assessment {assessment.uuid} for {assessment.user.username} -> {membership.workspace.name}")
        else:
            print(f"⚠ No workspace found for user {assessment.user.username}")
    
    print(f"\nFixed {fixed_count} assessments")

if __name__ == "__main__":
    fix_assessment_workspaces()