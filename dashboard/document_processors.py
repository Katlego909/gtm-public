import os
import uuid
import mimetypes
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

from django.utils import timezone
from django.utils.text import slugify
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

AGENT_ATTACHMENT_MAX_FILES = 4
AGENT_ATTACHMENT_MAX_BYTES = 6 * 1024 * 1024
AGENT_ATTACHMENT_TEXT_LIMIT = 6000
AGENT_ATTACHMENT_EXCERPT_LIMIT = 600
AGENT_ALLOWED_EXTENSIONS = {
    '.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml',
    '.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp',
}
AGENT_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


def _normalize_content_type(uploaded_file) -> str:
    guessed, _ = mimetypes.guess_type(uploaded_file.name or '')
    return (uploaded_file.content_type or guessed or '').lower()


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from PDF bytes when pypdf is available."""
    try:
        from pypdf import PdfReader
    except Exception:
        return ''

    text_parts: List[str] = []
    try:
        reader = PdfReader(BytesIO(file_bytes))
        for page in reader.pages[:8]:
            page_text = page.extract_text() or ''
            if page_text:
                text_parts.append(page_text)
            if sum(len(part) for part in text_parts) >= AGENT_ATTACHMENT_TEXT_LIMIT:
                break
    except Exception:
        return ''

    return '\n'.join(text_parts).strip()[:AGENT_ATTACHMENT_TEXT_LIMIT]


def _extract_pdf_text_with_ai(file_bytes: bytes) -> str:
    """Fallback PDF text extraction through Vertex AI when parser extraction is unavailable."""
    try:
        from gtm.ai_services import _get_client
    except Exception:
        return ''

    client = _get_client()
    if not client:
        return ''

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                "Extract all readable text from this PDF document. Return plain text only.",
                {
                    "mime_type": "application/pdf",
                    "data": file_bytes,
                },
            ]
        )
        extracted = (getattr(response, 'text', '') or '').strip()
        return extracted[:AGENT_ATTACHMENT_TEXT_LIMIT]
    except Exception:
        return ''


def _extract_image_text_with_ai(file_bytes: bytes) -> str:
    """Use Vertex AI vision to OCR meaningful text from image attachments."""
    try:
        from PIL import Image
        from gtm.ai_services import _get_client
    except Exception:
        return ''

    client = _get_client()
    if not client:
        return ''

    try:
        image = Image.open(BytesIO(file_bytes))
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                (
                    "Extract all readable text from this image. "
                    "Return plain text only, preserving important headings and bullet points."
                ),
                image,
            ]
        )
        extracted = (getattr(response, 'text', '') or '').strip()
        return extracted[:AGENT_ATTACHMENT_TEXT_LIMIT]
    except Exception:
        return ''


def _extract_attachment_text(uploaded_file, file_bytes: bytes) -> Tuple[str, str]:
    """Return extracted text and extraction status for an uploaded attachment."""
    suffix = Path(uploaded_file.name or '').suffix.lower()
    content_type = _normalize_content_type(uploaded_file)

    if content_type.startswith('text/') or suffix in {'.txt', '.md', '.csv', '.json', '.log', '.xml', '.yaml', '.yml'}:
        text = file_bytes.decode('utf-8', errors='ignore').strip()
        return text[:AGENT_ATTACHMENT_TEXT_LIMIT], 'text_extracted'

    if suffix == '.pdf' or content_type == 'application/pdf':
        pdf_text = _extract_pdf_text(file_bytes)
        if pdf_text:
            return pdf_text, 'pdf_extracted'
        pdf_text_ai = _extract_pdf_text_with_ai(file_bytes)
        if pdf_text_ai:
            return pdf_text_ai, 'pdf_ai_extracted'
        return '', 'pdf_parse_unavailable'

    if suffix in AGENT_IMAGE_EXTENSIONS or content_type.startswith('image/'):
        image_text = _extract_image_text_with_ai(file_bytes)
        if image_text:
            return image_text, 'image_ocr_extracted'
        return '', 'image_ocr_unavailable'

    return '', 'unsupported_for_extraction'


def _save_agent_attachment(uploaded_file, file_bytes: bytes) -> Tuple[str, str]:
    suffix = Path(uploaded_file.name or '').suffix.lower()
    stem = slugify(Path(uploaded_file.name or '').stem) or 'attachment'
    stamp = timezone.now().strftime('%Y/%m')
    filename = f"{uuid.uuid4().hex}_{stem[:60]}{suffix}"
    storage_path = os.path.join('agent_uploads', stamp, filename).replace('\\\\', '/')
    saved_path = default_storage.save(storage_path, ContentFile(file_bytes))
    file_url = default_storage.url(saved_path)
    return saved_path, file_url


def _process_agent_attachments(uploaded_files) -> Tuple[List[Dict[str, Any]], str, List[str]]:
    """Validate, persist, and extract text context from uploaded files."""
    attachments: List[Dict[str, Any]] = []
    context_parts: List[str] = []
    warnings: List[str] = []

    files = list(uploaded_files or [])
    if len(files) > AGENT_ATTACHMENT_MAX_FILES:
        warnings.append(f"Only the first {AGENT_ATTACHMENT_MAX_FILES} attachments were processed.")
        files = files[:AGENT_ATTACHMENT_MAX_FILES]

    for uploaded_file in files:
        file_name = uploaded_file.name or 'attachment'
        suffix = Path(file_name).suffix.lower()
        content_type = _normalize_content_type(uploaded_file)

        if suffix not in AGENT_ALLOWED_EXTENSIONS:
            warnings.append(f"Unsupported file type skipped: {file_name}")
            continue

        if uploaded_file.size and uploaded_file.size > AGENT_ATTACHMENT_MAX_BYTES:
            warnings.append(f"File exceeds 6 MB limit and was skipped: {file_name}")
            continue

        file_bytes = uploaded_file.read() or b''
        uploaded_file.seek(0)
        if not file_bytes:
            warnings.append(f"Empty file skipped: {file_name}")
            continue

        extracted_text, extraction_status = _extract_attachment_text(uploaded_file, file_bytes)
        saved_path, file_url = _save_agent_attachment(uploaded_file, file_bytes)

        excerpt = extracted_text[:AGENT_ATTACHMENT_EXCERPT_LIMIT] if extracted_text else ''
        attachments.append(
            {
                'name': file_name,
                'content_type': content_type,
                'size': uploaded_file.size or len(file_bytes),
                'path': saved_path,
                'url': file_url,
                'extraction_status': extraction_status,
                'excerpt': excerpt,
            }
        )

        if extracted_text:
            context_parts.append(
                f"Attachment: {file_name}\nExtracted content:\n{extracted_text[:AGENT_ATTACHMENT_TEXT_LIMIT]}"
            )
        else:
            context_parts.append(
                f"Attachment: {file_name}\nNo extractable text was available ({extraction_status})."
            )

    return attachments, '\n\n---\n\n'.join(context_parts), warnings
