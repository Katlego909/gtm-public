import logging
from django.conf import settings
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    
logger = logging.getLogger(__name__)

def perform_gtm_visual_audit(session_file, session_context=None):
    """
    Performs a specialized GTM strategic audit of a visual asset (screenshot/PDF).
    Uses the gemini-2.5-flash model via the Unified Google GenAI SDK.
    """
    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    location = getattr(settings, "GCP_LOCATION", "europe-west1") # Use our provisioned region
    
    if not GENAI_AVAILABLE or not project_id:
        logger.error("google-genai not available or GCP_PROJECT_ID missing for audit.")
        return "Multimodal AI services are currently unavailable."

    client = genai.Client(
        vertexai=True,
        project=project_id,
        location=location
    )
    
    # 1) Prepare file data
    try:
        session_file.file.open('rb')
        file_bytes = session_file.file.read()
        session_file.file.close()
        
        # Determine MIME type
        filename = session_file.file.name.lower()
        if filename.endswith(".pdf"):
            mime_type = "application/pdf"
        elif filename.endswith(".png"):
            mime_type = "image/png"
        elif filename.endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        else:
            mime_type = "application/octet-stream"
            
    except Exception as e:
        logger.error(f"Failed to read file for audit: {e}")
        return "ERROR: Could not read the evidence file."

    # 2) Construct Prompt
    asset_name = session_file.get_file_type_display()
    audit_prompt = f"""You are the 'GTM Strategic Auditor'. 
Perform a professional strategic critique of this {asset_name}.

CONTEXT FROM GTM ASSESSMENT:
{session_context or 'Standard GTM strategic planning.'}

CRITIQUE CATEGORIES:
1. **Messaging Clarity**: Is the core Value Proposition obvious within 3 seconds?
2. **Conversion Friction**: Are there barriers to user action or a confusing CTA?
3. **Strategic Alignment**: Does this asset solve the gaps identified in the company's GTM scores?
4. **Professionalism & Trust**: Does the design/structure inspire confidence in the target segment?

REPORT FORMAT:
- Use clear headers.
- Provide 3-5 specific 'Power Moves' for improvement.
- Assign an 'Evidence Score' (1-10) for this asset's GTM readiness.
"""

    # 3) Generate Multimodal Content
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                        types.Part.from_text(text=audit_prompt)
                    ]
                )
            ]
        )
        
        # Save audit back to the model for the Playbook to find
        session_file.ai_audit_notes = response.text
        # Optional: AI could suggest a score modifier (parsing logic could go here)
        session_file.save()
        
        return response.text
        
    except Exception as e:
        logger.error(f"Multimodal Audit API Call Failed: {e}")
        return f"The Auditor could not process this image right now: {str(e)}"
