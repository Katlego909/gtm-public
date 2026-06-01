#!/usr/bin/env python
"""
Comprehensive AI Configuration Diagnosis

Shows exactly what Django sees and what the AI modules are using.
"""

import os
import sys
import json

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
import django
django.setup()

from django.conf import settings
from google import genai

print("=" * 70)
print("AI CONFIGURATION DIAGNOSIS")
print("=" * 70)

# 1. Environment Variables
print("\n[1] ENVIRONMENT VARIABLES")
print("-" * 70)
print(f"GOOGLE_APPLICATION_CREDENTIALS: {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'NOT SET')}")
print(f"GCP_PROJECT_ID (env): {os.environ.get('GCP_PROJECT_ID', 'NOT SET')}")
print(f"GCP_LOCATION (env): {os.environ.get('GCP_LOCATION', 'NOT SET')}")

# 2. Django Settings
print("\n[2] DJANGO SETTINGS")
print("-" * 70)
print(f"GCP_PROJECT_ID: {getattr(settings, 'GCP_PROJECT_ID', 'NOT SET')}")
print(f"GCP_LOCATION: {getattr(settings, 'GCP_LOCATION', 'NOT SET')}")
print(f"GOOGLE_APPLICATION_CREDENTIALS: {getattr(settings, 'GOOGLE_APPLICATION_CREDENTIALS', 'NOT SET')}")

# 3. Service Account Credentials File
print("\n[3] SERVICE ACCOUNT FILE")
print("-" * 70)
sa_path = getattr(settings, 'GOOGLE_APPLICATION_CREDENTIALS', '')
if sa_path:
    try:
        with open(sa_path) as f:
            creds = json.load(f)
        print(f"File exists: YES")
        print(f"Project ID in file: {creds.get('project_id', 'NOT FOUND')}")
        print(f"Service account: {creds.get('client_email', 'NOT FOUND')}")
    except Exception as e:
        print(f"File exists: NO - {e}")
else:
    print("Path not configured")

# 4. Test AI Clients (skipped due to circular imports)

# 5. Direct genai.Client test
print("\n[5] DIRECT GENAI.CLIENT TEST")
print("-" * 70)
try:
    project = getattr(settings, 'GCP_PROJECT_ID', None)
    location = getattr(settings, 'GCP_LOCATION', 'us-central1')

    print(f"Creating client with:")
    print(f"  project={project}")
    print(f"  location={location}")
    print(f"  vertexai=True")

    test_client = genai.Client(
        vertexai=True,
        project=project,
        location=location
    )
    print(f"✓ Direct client created: {type(test_client).__name__}")

    # Try a simple API call
    print("\nAttempting test API call...")
    response = test_client.models.generate_content(
        model='gemini-2.5-flash',
        contents='Say "OK"'
    )
    print(f"✓ API call succeeded")
    print(f"  Response: {response.text[:50] if response.text else 'No response'}")

except Exception as e:
    print(f"✗ Failed: {type(e).__name__}: {str(e)[:200]}")

    # If there's an error, extract the project being used
    error_str = str(e)
    if "projects/" in error_str:
        import re
        match = re.search(r'projects/([a-z0-9-]+)', error_str)
        if match:
            used_project = match.group(1)
            expected_project = getattr(settings, 'GCP_PROJECT_ID', None)
            print(f"\n⚠️  MISMATCH DETECTED:")
            print(f"    Expected project: {expected_project}")
            print(f"    API used project: {used_project}")

# 6. Google Auth Investigation
print("\n[6] GOOGLE AUTH INVESTIGATION")
print("-" * 70)
try:
    from google.auth import default as get_default_credentials

    # Try to get default credentials
    creds, project = get_default_credentials()
    if creds:
        print(f"✓ Default credentials found")
        print(f"  Type: {type(creds).__class__.__name__}")
        print(f"  Project from credentials: {project}")
    else:
        print("✗ No default credentials available")
except Exception as e:
    print(f"⚠️  Could not determine default creds: {e}")

print("\n" + "=" * 70)
print("END OF DIAGNOSIS")
print("=" * 70)
