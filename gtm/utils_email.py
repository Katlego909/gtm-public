# gtm/utils_email.py
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.defaultfilters import floatformat
from django.template.loader import render_to_string
from django.utils.html import strip_tags

def send_snapshot_report_email(snapshot):
    """
    Renders and sends a premium HTML + plaintext email for a completed assessment.
    Uses Gmail SMTP via settings.py. Supports staging redirection.
    Includes comprehensive assessment data, action items, and downloadable resources.
    """
    from django.urls import reverse
    from .models import ActionItem
    import markdown
    
    session = snapshot.session
    subject = f"Your GTM Assessment Results — {snapshot.company_name or 'Untitled'} ({floatformat(snapshot.overall, 0)}/100)"

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

    # Sort categories to identify strengths and weaknesses. Threshold-gated
    # (matching gtm/utils_pdf.py's PDF report) rather than a pure top-N/bottom-N
    # rank slice -- with only 3 pillars total, slicing bottom-2 and top-2
    # unconditionally guarantees the middle-ranked category lands in both lists.
    categories_sorted = sorted(snapshot.category_breakdown, key=lambda x: x.get('avg', 0))
    weakest_areas = [c for c in categories_sorted if c.get('avg', 0) < 3.0][:2]
    strongest_areas = [c for c in reversed(categories_sorted) if c.get('avg', 0) >= 4.0 and c not in weakest_areas][:2]
    
    # Get action items count
    action_items_count = ActionItem.objects.filter(session=session).count()
    
    # Convert band actions markdown to HTML
    band_actions_html = ""
    if snapshot.band and snapshot.band.actions_markdown:
        band_actions_html = markdown.markdown(snapshot.band.actions_markdown)

    # Build absolute URLs
    base_url = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000")
    
    context = {
        "snap": snapshot,
        "session": session,
        "overall": snapshot.overall,
        "band": snapshot.band,
        "categories": snapshot.category_breakdown,
        "weakest_areas": weakest_areas,
        "strongest_areas": strongest_areas,
        "action_items_count": action_items_count,
        "band_actions_html": band_actions_html,
        "radar_labels": snapshot.radar_labels,
        "radar_values": snapshot.radar_values,
        
        # Absolute URLs for email links
        "admin_session_url": base_url + f"/admin/{session._meta.app_label}/{session._meta.model_name}/{session.pk}/change/",
        "admin_snapshot_url": base_url + f"/admin/{snapshot._meta.app_label}/{snapshot._meta.model_name}/{snapshot.pk}/change/",
        "public_report_url": base_url + reverse('gtm:results', args=[session.uuid]),
        "playbook_url": base_url + reverse('gtm:playbook', args=[session.uuid]),
        "pdf_download_url": base_url + reverse('gtm:download', args=[session.uuid]),
        
        # Branding
        "brand_name": getattr(settings, "BRAND_NAME", "ForgeGTM"),
        "brand_url": getattr(settings, "BRAND_URL", base_url),
        "preheader": f"Your GTM score: {floatformat(snapshot.overall, 0)}/100 • {snapshot.band.stage if snapshot.band else 'Assessment Complete'} • Download your playbook now!",
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

def send_workspace_invitation_email(invitation, request=None):
    """
    Sends a workspace invitation email with both HTML and plaintext versions.
    """
    from django.urls import reverse
    
    workspace = invitation.workspace
    inviter = invitation.invited_by
    email = invitation.email
    
    # Build absolute URL for invitation link
    if request:
        invite_url = request.build_absolute_uri(
            reverse('gtm:workspace:join', args=[invitation.token])
        )
    else:
        base_url = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000")
        invite_url = base_url + reverse('gtm:workspace:join', args=[invitation.token])
        
    inviter_name = inviter.get_full_name() or inviter.username
    
    context = {
        'workspace': workspace,
        'inviter_name': inviter_name,
        'invite_url': invite_url,
        'role': invitation.role.replace('_', ' '),
        'brand_name': getattr(settings, "BRAND_NAME", "ForgeGTM"),
    }

    subject = f"You've been invited to join {workspace.name} on {getattr(settings, 'BRAND_NAME', 'ForgeGTM')}"
    html_body = render_to_string("emails/workspace_invitation.html", context)
    text_body = render_to_string("emails/workspace_invitation.txt", context)
    
    if not text_body.strip():
        text_body = strip_tags(html_body)
        
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


def send_beta_invite_email(invite_code, to_email, request=None):
    """
    Sends a closed-beta invite code + signup link to a prospective tester.
    Mirrors send_workspace_invitation_email's pattern (HTML+text templates,
    SITE_BASE_URL fallback for building the link without a request).
    """
    from django.urls import reverse

    signup_path = f"{reverse('account_signup')}?code={invite_code.code}"
    if request:
        signup_url = request.build_absolute_uri(signup_path)
    else:
        base_url = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000")
        signup_url = base_url + signup_path

    context = {
        'invite_code': invite_code.code,
        'signup_url': signup_url,
    }

    subject = f"You're invited to the {getattr(settings, 'BRAND_NAME', 'ForgeGTM')} beta"
    html_body = render_to_string("emails/beta_invite.html", context)
    text_body = render_to_string("emails/beta_invite.txt", context)

    if not text_body.strip():
        text_body = strip_tags(html_body)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[to_email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)


def send_global_ai_cap_alert_email(to_email):
    """Plain-text operator alert when gtm/ai_credits.py's global daily AI
    spend ceiling trips -- deliberately no HTML/branding, this is an
    internal ops signal, not tester-facing. Deduped to once/day by the
    caller (gtm/ai_credits.py::_maybe_alert_global_cap_exceeded)."""
    from django.core.mail import send_mail
    send_mail(
        subject="[ForgeGTM] Global AI daily spend cap reached",
        message=(
            "The global AI_GLOBAL_DAILY_TOKEN_CAP has been reached today. "
            "New AI generation requests are being blocked app-wide until the "
            "daily period rolls over. Check the AI Credit Transactions admin "
            "list to see which accounts are driving usage."
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=[to_email],
        fail_silently=True,
    )


def send_notification_email(recipient, title, message, link=""):
    """
    Sends a generic notification email for any Notification (task update,
    invite, AI report, etc). Kept intentionally simple/type-agnostic so
    callers don't need a bespoke template per notification_type.
    """
    base_url = getattr(settings, "SITE_BASE_URL", "http://127.0.0.1:8000")
    absolute_link = (base_url + link) if link and not link.startswith("http") else link

    context = {
        'recipient': recipient,
        'title': title,
        'message': message,
        'link': absolute_link,
        'brand_name': getattr(settings, "BRAND_NAME", "ForgeGTM"),
    }

    subject = title
    html_body = render_to_string("emails/notification.html", context)
    text_body = render_to_string("emails/notification.txt", context)

    if not text_body.strip():
        text_body = strip_tags(html_body)

    msg = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        to=[recipient.email],
    )
    msg.attach_alternative(html_body, "text/html")
    msg.send(fail_silently=False)
