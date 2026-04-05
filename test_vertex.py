import os
import django
from django.conf import settings

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gtm_validator.settings')
django.setup()

from gtm.ai_services import _init_gemini, VERTEX_AVAILABLE
from vertexai.generative_models import GenerativeModel

def test_connection():
    print(f"DEBUG: Vertex SDK Available: {VERTEX_AVAILABLE}")
    print(f"DEBUG: Project ID from Settings: '{getattr(settings, 'GCP_PROJECT_ID', 'NOT FOUND')}'")
    print(f"DEBUG: Location from Settings: '{getattr(settings, 'GCP_LOCATION', 'NOT FOUND')}'")
    print(f"DEBUG: Credentials Path: '{getattr(settings, 'GOOGLE_APPLICATION_CREDENTIALS', 'NOT FOUND')}'")
    
    print("\nScanning Regions for Active AI Handshake...")
    # Try the most common regions
    regions = ["us-central1", "us-east4", "europe-west1", "asia-southeast1"]
    
    for r in regions:
        print(f"\n--- Testing Region: {r} ---")
        try:
            import vertexai
            vertexai.init(project=settings.GCP_PROJECT_ID, location=r)
            
            # Try a very basic model name
            model = GenerativeModel("gemini-1.5-flash")
            response = model.generate_content("Ping!")
            print(f"SUCCESS! AI Handshake confirmed in {r}!")
            print(f"Response: {response.text}")
            
            # IMPORTANT: Suggest updating settings if this works
            return 
        except Exception as e:
            if "Publisher Model" in str(e) and "404" in str(e):
                print(f"Region {r}: Model not found (404).")
            else:
                print(f"Region {r}: Error: {str(e)}")

if __name__ == "__main__":
    test_connection()
