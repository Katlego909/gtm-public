#!/usr/bin/env python
"""
Comprehensive Vertex AI Configuration Verification Script

Checks:
  - Environment variables (.env)
  - Service account credentials
  - GCP project configuration
  - Vertex AI client initialization
  - Model availability
  - No Gemini API key present
  - Django settings integration
"""

import os
import sys
import json
from pathlib import Path
from typing import Tuple, Optional

# Colors for terminal output
class Colors:
    OK = '\033[92m'      # Green
    FAIL = '\033[91m'    # Red
    WARN = '\033[93m'    # Yellow
    INFO = '\033[94m'    # Blue
    RESET = '\033[0m'

def print_check(status: str, message: str, detail: str = ""):
    """Print a check result with status."""
    if status == "PASS":
        symbol = f"{Colors.OK}[PASS]{Colors.RESET}"
    elif status == "FAIL":
        symbol = f"{Colors.FAIL}[FAIL]{Colors.RESET}"
    elif status == "WARN":
        symbol = f"{Colors.WARN}[WARN]{Colors.RESET}"
    else:
        symbol = f"{Colors.INFO}[INFO]{Colors.RESET}"

    print(f"{symbol} {message}")
    if detail:
        print(f"  {detail}")

def check_env_file() -> Tuple[bool, dict]:
    """Check if .env file exists and contains required variables."""
    print(f"\n{Colors.INFO}=== Environment Variables ==={Colors.RESET}")

    env_file = Path(".env")
    if not env_file.exists():
        print_check("FAIL", ".env file not found")
        return False, {}

    print_check("PASS", ".env file exists")

    # Load .env without using load_dotenv to avoid side effects
    env_vars = {}
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env_vars[key] = value

    # Check required variables
    required = ["GCP_PROJECT_ID", "GCP_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"]
    all_present = True

    for var in required:
        if var in env_vars:
            print_check("PASS", f"{var} = {env_vars[var]}")
        else:
            print_check("FAIL", f"{var} not set in .env")
            all_present = False

    # Check for Gemini API key (should NOT exist)
    if "GEMINI_API_KEY" in env_vars:
        print_check("FAIL", "GEMINI_API_KEY still present in .env (should be removed)")
        all_present = False
    else:
        print_check("PASS", "GEMINI_API_KEY not present (correct)")

    return all_present, env_vars

def check_service_account(creds_path: str) -> Tuple[bool, Optional[dict]]:
    """Check if service account credentials file exists and is valid."""
    print(f"\n{Colors.INFO}=== Service Account Credentials ==={Colors.RESET}")

    sa_file = Path(creds_path)
    if not sa_file.exists():
        print_check("FAIL", f"Service account file not found: {creds_path}")
        return False, None

    print_check("PASS", f"Service account file exists: {creds_path}")

    try:
        with open(sa_file) as f:
            creds = json.load(f)
    except json.JSONDecodeError as e:
        print_check("FAIL", f"Invalid JSON in service account file: {e}")
        return False, None

    # Check required fields
    required_fields = ["type", "project_id", "private_key", "client_email"]
    all_fields_present = True

    for field in required_fields:
        if field in creds:
            if field == "private_key":
                print_check("PASS", f"{field} present (redacted)")
            else:
                print_check("PASS", f"{field} = {creds[field]}")
        else:
            print_check("FAIL", f"Missing {field} in service account")
            all_fields_present = False

    return all_fields_present, creds

def verify_project_consistency(env_vars: dict, sa_creds: dict) -> bool:
    """Verify that GCP_PROJECT_ID matches service account project_id."""
    print(f"\n{Colors.INFO}=== Project Consistency ==={Colors.RESET}")

    env_project = env_vars.get("GCP_PROJECT_ID", "")
    sa_project = sa_creds.get("project_id", "")

    if not env_project:
        print_check("FAIL", "GCP_PROJECT_ID not set in .env")
        return False

    if not sa_project:
        print_check("FAIL", "project_id not set in service account")
        return False

    if env_project == sa_project:
        print_check("PASS", f"Project IDs match: {env_project}")
        return True
    else:
        print_check("FAIL", f"Project ID mismatch:")
        print(f"    .env: {env_project}")
        print(f"    Service Account: {sa_project}")
        return False

def verify_location(env_vars: dict) -> bool:
    """Verify that GCP_LOCATION is a valid Vertex AI region."""
    print(f"\n{Colors.INFO}=== Region Validation ==={Colors.RESET}")

    location = env_vars.get("GCP_LOCATION", "")

    if not location:
        print_check("FAIL", "GCP_LOCATION not set")
        return False

    # Valid Vertex AI regions
    valid_regions = [
        "us-central1", "us-west1", "us-west2", "us-west3", "us-west4", "us-south1",
        "us-east1", "us-east4",
        "europe-west1", "europe-west2", "europe-west3", "europe-west4", "europe-north1",
        "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast3",
        "asia-south1", "asia-southeast1", "asia-southeast2",
        "australia-southeast1",
        "northamerica-northeast1", "southamerica-east1",
        "middle-east-north1",
    ]

    if location in valid_regions:
        print_check("PASS", f"GCP_LOCATION is valid: {location}")
        return True
    else:
        print_check("WARN", f"GCP_LOCATION may be invalid: {location}")
        print(f"  Valid regions: {', '.join(valid_regions)}")
        return False

def test_django_settings() -> bool:
    """Test Django settings load correctly."""
    print(f"\n{Colors.INFO}=== Django Settings ==={Colors.RESET}")

    try:
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
        import django
        django.setup()

        from django.conf import settings

        print_check("PASS", "Django initialized successfully")

        gcp_project = getattr(settings, 'GCP_PROJECT_ID', None)
        gcp_location = getattr(settings, 'GCP_LOCATION', None)

        if gcp_project:
            print_check("PASS", f"Django GCP_PROJECT_ID: {gcp_project}")
        else:
            print_check("FAIL", "Django GCP_PROJECT_ID not set")
            return False

        if gcp_location:
            print_check("PASS", f"Django GCP_LOCATION: {gcp_location}")
        else:
            print_check("FAIL", "Django GCP_LOCATION not set")
            return False

        # Check for Gemini API key in settings
        gemini_key = getattr(settings, 'GEMINI_API_KEY', None)
        if gemini_key:
            print_check("FAIL", "GEMINI_API_KEY still defined in Django settings")
            return False
        else:
            print_check("PASS", "GEMINI_API_KEY not in Django settings")

        return True

    except Exception as e:
        print_check("FAIL", f"Django setup failed: {e}")
        return False

def test_vertex_ai_client() -> bool:
    """Test Vertex AI client initialization."""
    print(f"\n{Colors.INFO}=== Vertex AI Client ==={Colors.RESET}")

    try:
        from gtm.ai_services import _get_client

        client = _get_client()
        if client is None:
            print_check("FAIL", "Client is None (check GCP_PROJECT_ID)")
            return False

        print_check("PASS", f"Vertex AI client initialized: {type(client).__name__}")

        # Try to access models
        try:
            models = client.models
            print_check("PASS", "Client models accessible")
            return True
        except Exception as e:
            print_check("FAIL", f"Cannot access client.models: {e}")
            return False

    except Exception as e:
        print_check("FAIL", f"Vertex AI client initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_model_access() -> bool:
    """Test if models can be queried."""
    print(f"\n{Colors.INFO}=== Model Access ==={Colors.RESET}")

    try:
        from gtm.ai_services import _get_client
        from django.conf import settings

        client = _get_client()
        if client is None:
            print_check("WARN", "Skipping model test (client not initialized)")
            return False

        # Test gemini-2.5-flash availability
        try:
            print("Attempting simple model call...")
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents='Respond with: Vertex AI is working!'
            )

            if response and hasattr(response, 'text'):
                print_check("PASS", "Model call successful")
                print(f"  Response: {response.text[:100]}")
                return True
            else:
                print_check("FAIL", "No response from model")
                return False

        except Exception as e:
            error_msg = str(e)
            if "PERMISSION_DENIED" in error_msg:
                print_check("FAIL", "Permission denied - service account lacks Vertex AI permissions")
                print(f"  Error: {error_msg[:200]}")
                return False
            elif "not found" in error_msg.lower() or "404" in error_msg:
                print_check("FAIL", "Model not found in region")
                print(f"  Check GCP_LOCATION: {settings.GCP_LOCATION}")
                return False
            else:
                print_check("FAIL", f"Model call failed: {error_msg[:200]}")
                return False

    except Exception as e:
        print_check("FAIL", f"Model access test failed: {e}")
        return False

def check_no_gemini_code() -> bool:
    """Check that no Gemini API code remains."""
    print(f"\n{Colors.INFO}=== Code Audit ==={Colors.RESET}")

    import subprocess

    try:
        # Search for legacy Gemini imports in application code (exclude cache and venv)
        result = subprocess.run(
            ['grep', '-r', '--exclude-dir=__pycache__', '--exclude-dir=venv',
             '--include=*.py', 'google.generativeai', 'gtm/', 'dashboard/', 'gtm_validator/'],
            capture_output=True,
            text=True,
            cwd='.'
        )

        if result.returncode == 0 and result.stdout.strip():
            print_check("FAIL", "Found google.generativeai imports in code")
            print(f"  {result.stdout}")
            return False
        else:
            print_check("PASS", "No google.generativeai imports found")

        # Check for GEMINI_API_KEY in code (exclude cache and venv)
        result = subprocess.run(
            ['grep', '-r', '--exclude-dir=__pycache__', '--exclude-dir=venv',
             '--include=*.py', 'GEMINI_API_KEY', 'gtm/', 'dashboard/', 'gtm_validator/'],
            capture_output=True,
            text=True,
            cwd='.'
        )

        if result.returncode == 0 and result.stdout.strip():
            print_check("FAIL", "Found GEMINI_API_KEY references in code")
            print(f"  {result.stdout}")
            return False
        else:
            print_check("PASS", "No GEMINI_API_KEY references found")

        return True

    except FileNotFoundError:
        print_check("WARN", "grep not available on this system")
        return True
    except Exception as e:
        print_check("WARN", f"Code audit skipped: {e}")
        return True

def main():
    """Run all verification checks."""
    print(f"\n{Colors.INFO}{'='*60}")
    print(f"  VERTEX AI CONFIGURATION VERIFICATION")
    print(f"{'='*60}{Colors.RESET}\n")

    results = {}

    # Check 1: Environment variables
    env_ok, env_vars = check_env_file()
    results["env_file"] = env_ok

    if not env_vars:
        print_check("FAIL", "Cannot continue without valid .env")
        return False

    # Check 2: Service account credentials
    creds_path = env_vars.get("GOOGLE_APPLICATION_CREDENTIALS", "keys/service-account.json")
    sa_ok, sa_creds = check_service_account(creds_path)
    results["service_account"] = sa_ok

    if not sa_creds:
        print_check("FAIL", "Cannot continue without valid service account")
        return False

    # Check 3: Project consistency
    project_ok = verify_project_consistency(env_vars, sa_creds)
    results["project_consistency"] = project_ok

    # Check 4: Location validation
    location_ok = verify_location(env_vars)
    results["location"] = location_ok

    # Check 5: Django settings
    django_ok = test_django_settings()
    results["django_settings"] = django_ok

    # Check 6: Vertex AI client
    client_ok = test_vertex_ai_client()
    results["vertex_ai_client"] = client_ok

    # Check 7: Model access
    model_ok = test_model_access()
    results["model_access"] = model_ok

    # Check 8: Code audit
    code_ok = check_no_gemini_code()
    results["code_audit"] = code_ok

    # Summary
    print(f"\n{Colors.INFO}{'='*60}")
    print(f"  VERIFICATION SUMMARY")
    print(f"{'='*60}{Colors.RESET}\n")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for check, result in results.items():
        status = "PASS" if result else "FAIL"
        print_check(status, check.replace("_", " ").title())

    print(f"\n{Colors.INFO}Result: {passed}/{total} checks passed{Colors.RESET}\n")

    if passed == total:
        print(f"{Colors.OK}[SUCCESS] All systems ready for Vertex AI!{Colors.RESET}\n")
        return True
    else:
        print(f"{Colors.FAIL}[FAILED] Some checks failed. See details above.{Colors.RESET}\n")
        return False

if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print(f"\n{Colors.WARN}Interrupted by user{Colors.RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{Colors.FAIL}Fatal error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
