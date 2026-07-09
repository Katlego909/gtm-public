# Document Processing & OCR Logic

The GTM Validator features a hybrid document processing engine in `dashboard/document_processors.py` that handles the extraction of context for the AI Agent. This system is designed for **Context Maximization** while maintaining strict performance and token safety limits.

---

## 1. The "Chain of Extraction" Pipeline

The system uses a multi-layered extraction strategy. If a fast, local method fails, it automatically escalates to high-intelligence AI extraction.

### Implementation: Hybrid PDF Extraction
This logic first attempts a local parse using `pypdf` and falls back to **Gemini-2.5-Flash** for complex or scanned documents.

```python
def _extract_pdf_text(file_bytes: bytes) -> str:
    """Fast local parsing using pypdf."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(file_bytes))
        text_parts = []
        for page in reader.pages[:8]: # Limit to first 8 pages
            page_text = page.extract_text() or ''
            text_parts.append(page_text)
        return '\n'.join(text_parts).strip()
    except Exception:
        return ''

def _extract_pdf_text_with_ai(file_bytes: bytes) -> str:
    """Sophisticated AI OCR fallback via Vertex AI."""
    client = _get_client()
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[
            "Extract all readable text from this PDF document. Return plain text only.",
            {"mime_type": "application/pdf", "data": file_bytes}
        ]
    )
    return (getattr(response, 'text', '') or '').strip()
```

---

## 2. Multi-File Orchestration

The `_process_agent_attachments` function acts as the central orchestrator for message context. It validates file types, enforces size limits, and aggregates text for the LLM.

### Implementation: `_process_agent_attachments`

```python
def _process_agent_attachments(uploaded_files) -> Tuple[List[Dict], str, List[str]]:
    """
    1. Validates: Enforces 6MB limit and whitelist extensions.
    2. Persists: Saves to storage (Local or GCP) with unique UUIDs.
    3. Contextualizes: Aggregates extracted text into a single 'Context Block'.
    """
    attachments = []
    context_parts = []
    
    for uploaded_file in uploaded_files[:AGENT_ATTACHMENT_MAX_FILES]:
        file_bytes = uploaded_file.read()
        
        # Determine extraction strategy based on MIME type
        extracted_text, status = _extract_attachment_text(uploaded_file, file_bytes)
        
        # Create persistent record path
        saved_path, file_url = _save_agent_attachment(uploaded_file, file_bytes)
        
        attachments.append({
            'name': uploaded_file.name,
            'extraction_status': status,
            'excerpt': extracted_text[:600] # For quick history reference
        })

        if extracted_text:
            context_parts.append(f"Attachment: {uploaded_file.name}\n{extracted_text}")

    return attachments, '\n\n---\n\n'.join(context_parts), warnings
```

---

## 3. Attachment Safety & Performance Limits

The engine enforces strict guardrails to prevent token overflow and infrastructure stress:

| Constraint | Value | Rationale |
| --- | --- | --- |
| **Max Files** | 4 | Prevents dilution of the AI's attention span. |
| **Max Size** | 6 MB | Protects memory during binary-to-string conversion. |
| **Text Limit** | 6,000 chars | Optimized for the LLM's 32k context window. |
| **Whitelisted Types** | `.pdf`, `.png`, `.jpg`, `.txt`, `.md`, `.json`, `.yaml` | Security: Prevents execution of malicious scripts. |

---

## 4. Vision-Based Marketing Critique

When an image (e.g., a landing page screenshot) is uploaded, the system uses **Computer Vision** to extract not just text, but **Strategic Intent**.
- **Prompt Logic:** "Extract text, preserving important headings and bullet points."
- **Outcome:** The AI understands the hierarchy of information, allowing it to critique the "Hero Message" vs. the "CTA" in the resulting GTM playbook.
