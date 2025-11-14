#!/usr/bin/env python
"""
Quick diagnostic script to check if environment variables are loaded correctly.
Run this on PythonAnywhere to verify your configuration.
"""
import os
import sys

# Add project to path
sys.path.insert(0, '/home/forge/gtm')  # Update with your path

# Load environment
from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("ENVIRONMENT CONFIGURATION CHECK")
print("=" * 60)

# Check critical environment variables
configs = {
    'DJANGO_SECRET_KEY': os.getenv('DJANGO_SECRET_KEY', 'NOT SET'),
    'DEBUG': os.getenv('DEBUG', 'NOT SET'),
    'ALLOWED_HOSTS': os.getenv('ALLOWED_HOSTS', 'NOT SET'),
    'GEMINI_API_KEY': os.getenv('GEMINI_API_KEY', 'NOT SET'),
    'EMAIL_HOST_USER': os.getenv('EMAIL_HOST_USER', 'NOT SET'),
    'EMAIL_HOST_PASSWORD': os.getenv('EMAIL_HOST_PASSWORD', 'NOT SET'),
}

for key, value in configs.items():
    if value == 'NOT SET':
        print(f"❌ {key}: NOT SET")
    elif key in ['DJANGO_SECRET_KEY', 'GEMINI_API_KEY', 'EMAIL_HOST_PASSWORD']:
        # Mask sensitive values
        if len(value) > 10:
            masked = value[:4] + '*' * (len(value) - 8) + value[-4:]
        else:
            masked = '*' * len(value)
        print(f"✅ {key}: {masked} ({len(value)} chars)")
    else:
        print(f"✅ {key}: {value}")

print("\n" + "=" * 60)
print("DJANGO SETTINGS CHECK")
print("=" * 60)

try:
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
    django.setup()
    
    from django.conf import settings
    
    print(f"DEBUG: {settings.DEBUG}")
    print(f"ALLOWED_HOSTS: {settings.ALLOWED_HOSTS}")
    print(f"GEMINI_API_KEY set: {'Yes' if settings.GEMINI_API_KEY else 'No'}")
    print(f"EMAIL_BACKEND: {settings.EMAIL_BACKEND}")
    print(f"EMAIL_HOST_USER: {settings.EMAIL_HOST_USER or 'NOT SET'}")
    
    print("\n" + "=" * 60)
    print("TESTING AI CONNECTION")
    print("=" * 60)
    
    if settings.GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=settings.GEMINI_API_KEY)
            model = genai.GenerativeModel('gemini-2.0-flash-exp')
            response = model.generate_content("Say 'Hello from PythonAnywhere!'")
            print(f"✅ AI Connection: SUCCESS")
            print(f"Response: {response.text[:100]}...")
        except Exception as e:
            print(f"❌ AI Connection: FAILED")
            print(f"Error: {str(e)}")
    else:
        print("❌ GEMINI_API_KEY not configured")
    
except Exception as e:
    print(f"❌ Django setup failed: {str(e)}")

print("\n" + "=" * 60)
