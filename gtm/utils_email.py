# gtm/utils_email.py
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags

def send_snapshot_report_email(snapshot):
    """
    Renders and sends a premium HTML + plaintext email for a completed assessment.
    Uses Gmail SMTP via settings.py. Supports staging redirection.
    """
    session = snapshot.session
    subject = f"GTM Assessment Report — {snapshot.company_name or 'Untitled'} ({int(round(snapshot.overall))}/100)"

    # Recipients
    to_recipients = []
    if session.contact_email:
        to_recipients.append(session.contact_email)

    # If EMAIL_REDIRECT_TO is set (staging), override recipients
    redirect = getattr(settings, "EMAIL_REDIRECT_TO", "")
    if redirect:
        to_recipients = [redirect]
        bcc_recipients = []
    else:
        bcc_recipients = list(getattr(settings, "GTM_REPORT_INTERNAL_TO", []))

    context = {
        "snap": snapshot,
        "session": session,
        "overall": snapshot.overall,
        "band": snapshot.band,
        "categories": snapshot.category_breakdown,
        "radar_labels": snapshot.radar_labels,
        "radar_values": snapshot.radar_values,
        # handy links (adjust to your domains / admin URL)
        "admin_session_url": f"/admin/{session._meta.app_label}/{session._meta.model_name}/{session.pk}/change/",
        "admin_snapshot_url": f"/admin/{snapshot._meta.app_label}/{snapshot._meta.model_name}/{snapshot.pk}/change/",
        "public_report_url": f"/results/{session.uuid}/",  # if you expose a public results page
        "brand_name": "Funti3r GTM",
        "brand_url": "https://funti3r.xyz",
        "logo_url": "https://funti3r.xyz/static/brand/funti3r-logo.png",  # use your hosted HTTPS logo
        "preheader": f"{(snapshot.company_name or 'Company')} scored {int(round(snapshot.overall))}/100 • {snapshot.band.stage if snapshot.band else ''}",
    }

    html_body = render_to_string("emails/assessment_report.html", context)
    text_body = render_to_string("emails/assessment_report.txt", context)
    if not text_body.strip():
        text_body = strip_tags(html_body)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=to_recipients or getattr(settings, "GTM_REPORT_INTERNAL_TO", []),
        bcc=bcc_recipients,
    )
    msg.attach_alternative(html_body, "text/html")

    # If you later want PDF attachments, generate bytes and attach here.
    # msg.attach(filename, pdf_bytes, "application/pdf")

    msg.send(fail_silently=False)
