# gtm/management/commands/load_defaults.py
from django.core.management.base import BaseCommand
from gtm.models import Category, Question, RecommendationBand, ToolRecommendation  # ← ensure model exists


class Command(BaseCommand):
    help = "Load default categories, questions, bands, and tool recommendations"

    def handle(self, *args, **kwargs):
        # ---------------------------
        # Categories (update-safe)
        # ---------------------------
        demand, _ = Category.objects.update_or_create(
            name="Demand", defaults={"weight": 0.4}
        )
        conversion, _ = Category.objects.update_or_create(
            name="Conversion", defaults={"weight": 0.4}
        )
        delivery, _ = Category.objects.update_or_create(
            name="Delivery", defaults={"weight": 0.2}
        )

        # ---------------------------
        # Demand
        # ---------------------------
        Question.objects.update_or_create(id_code="D1", defaults={
            "category": demand,
            "text": "We have a defined ICP (industry, size, buyer role).",
            "weight": 1.2,
            "diagnostic_note": "Clarity on who we sell to and why they buy. Use tools like HubSpot CRM, Apollo.io or Lusha to identify and segment your audience."
        })
        Question.objects.update_or_create(id_code="D2", defaults={
            "category": demand,
            "text": "Our messaging is simple and specific (problem → promise).",
            "weight": 1.2,
            "diagnostic_note": "Prospects should ‘get it’ in 5 seconds. Use messaging tools like Wynter or Copy.ai to test clarity."
        })
        Question.objects.update_or_create(id_code="D3", defaults={
            "category": demand,
            "text": "We run at least one always-on campaign.",
            "weight": 1.0,
            "diagnostic_note": "Consistent visibility matters. Try LinkedIn Ads, Meta Ads, or HubSpot Workflows for automation."
        })
        Question.objects.update_or_create(id_code="D4", defaults={
            "category": demand,
            "text": "We can attribute leads to channels (organic, paid, partner).",
            "weight": 1.0,
            "diagnostic_note": "Implement UTM tracking or GA4 attribution. Tools: Google Analytics 4, HubSpot Tracking URLs, Dreamdata."
        })
        Question.objects.update_or_create(id_code="D5", defaults={
            "category": demand,
            "text": "We generate enough top-of-funnel traffic for our targets.",
            "weight": 1.1,
            "diagnostic_note": "Use SEO and content analytics tools like Ahrefs, Semrush, or SurferSEO to drive qualified visitors."
        })

        # ---------------------------
        # Conversion
        # ---------------------------
        Question.objects.update_or_create(id_code="C1", defaults={
            "category": conversion,
            "text": "We respond to leads quickly with clear SLAs.",
            "weight": 1.1,
            "diagnostic_note": "Speed = revenue. Automate routing via HubSpot, Pipedrive, or Calendly integration."
        })
        Question.objects.update_or_create(id_code="C2", defaults={
            "category": conversion,
            "text": "We qualify leads consistently (BANT/MEDDIC or custom).",
            "weight": 1.0,
            "diagnostic_note": "Standardize your qualification with tools like Clay, Salesforce, or Notion deal tracking."
        })
        Question.objects.update_or_create(id_code="C3", defaults={
            "category": conversion,
            "text": "Our landing pages convert well (≥2–5% baseline).",
            "weight": 1.1,
            "diagnostic_note": "Experiment and test often. Tools: Webflow, Unbounce, or Hotjar for behavior insights."
        })
        Question.objects.update_or_create(id_code="C4", defaults={
            "category": conversion,
            "text": "We have sales enablement assets (deck, one-pager, case studies).",
            "weight": 1.0,
            "diagnostic_note": "Equip reps with collateral. Use Notion, Google Slides, or Canva for structured libraries."
        })
        Question.objects.update_or_create(id_code="C5", defaults={
            "category": conversion,
            "text": "We track win/loss reasons and iterate monthly.",
            "weight": 1.0,
            "diagnostic_note": "Use CRM custom fields or tools like Attio, Pipedrive, or Airtable to analyze feedback."
        })

        # ---------------------------
        # Delivery
        # ---------------------------
        Question.objects.update_or_create(id_code="L1", defaults={
            "category": delivery,
            "text": "Onboarding is consistent and fast.",
            "weight": 1.0,
            "diagnostic_note": "Friction kills retention. Tools: ClickUp, Asana, or Userflow for onboarding templates."
        })
        Question.objects.update_or_create(id_code="L2", defaults={
            "category": delivery,
            "text": "We measure CSAT/NPS and act on insights.",
            "weight": 1.0,
            "diagnostic_note": "Use Typeform, Delighted, or Hotjar Surveys to capture sentiment and trends."
        })
        Question.objects.update_or_create(id_code="L3", defaults={
            "category": delivery,
            "text": "Renewal/retention process exists (or repeat purchase).",
            "weight": 1.0,
            "diagnostic_note": "Track retention in CRM. Use ChurnZero, HubSpot, or Retently for lifecycle automation."
        })
        Question.objects.update_or_create(id_code="L4", defaults={
            "category": delivery,
            "text": "We collect and publish references/testimonials.",
            "weight": 1.0,
            "diagnostic_note": "Use Trustpilot, Capterra, or Senja.io to collect and share social proof."
        })

        # ---------------------------
        # Recommendation Bands (markdown includes tools)
        # ---------------------------
        bands = [
            (0, 39, "Foundations", "Establish GTM basics",
             """Action Plan:
- Define ICP and buyer roles.
- Clarify your value proposition & homepage hero message.
- Launch one always-on demand motion.

Recommended Tools:
- HubSpot CRM or Pipedrive — ICP & lead tracking.
- Apollo.io — Lead sourcing and outbound sequences.
- Webflow or Framer — Landing page testing."""
             ),
            (40, 54, "Process & SLAs", "Stabilize conversion process",
             """Action Plan:
- Set lead response SLAs to improve conversion consistency.
- Add lightweight qualification and routing.
- Document the full process (discovery → proposal → close).

Recommended Tools:
- Calendly or Chili Piper — Automate booking & response SLAs.
- HubSpot Service Hub — Workflow-based response tracking.
- Notion or ClickUp — Process documentation."""
             ),
            (55, 69, "Optimisation", "Tighten the funnel",
             """Action Plan:
- Create a weekly growth board (MQL → SQL → Win).
- Run landing page A/B tests and track engagement.
- Improve attribution and analytics setup.

Recommended Tools:
- Google Analytics 4, Hotjar — Conversion tracking.
- Unbounce, VWO — Landing page A/B testing.
- Dreamdata or HubSpot Attribution — Source mapping."""
             ),
            (70, 84, "Scale", "Layer advanced motions",
             """Action Plan:
- Pilot webinars or ABM campaigns to scale reach.
- Partner with complementary brands.
- Launch SDR/outbound playbooks with guardrails.

**Recommended Tools:**
- ZoomInfo or Clay — Targeted ABM and outbound data.
- HubSpot Marketing Hub — Multi-channel automation.
- Mutiny or Clearbit — Personalized website targeting."""
             ),
            (85, 100, "Moats", "Build durable advantage",
             """Action Plan:
- Build a brand moat with community and events.
- Publish benchmarks, studies, and insights.
- Partner across your ecosystem for compounded growth.

Recommended Tools:
- Circle or Slack — Community platforms.
- Notion or Beehiiv — Content & newsletter automation.
- Zapier or Make — Workflow integrations at scale."""
             ),
        ]
        for a, b, stage, headline, actions in bands:
            RecommendationBand.objects.update_or_create(
                min_score=a, max_score=b,
                defaults={"stage": stage, "headline": headline, "actions_markdown": actions},
            )

        # ---------------------------
        # ToolRecommendations (explicit rows)
        # ---------------------------
        TOOL_RECS = [
            # (category_obj, keyword, description, tools_csv, url)
            (demand, "icp", "Clarify ideal customer profile and segments.", "HubSpot CRM, Apollo.io, Lusha", ""),
            (demand, "messaging", "Test clarity of value proposition and homepage hero.", "Wynter, Copy.ai, Notion", ""),
            (demand, "always-on", "Automate persistent demand campaigns.", "LinkedIn Ads, Meta Ads, HubSpot Workflows", ""),
            (demand, "attribution", "Improve channel attribution and UTMs/GA4 setup.", "Google Analytics 4, HubSpot Tracking URLs, Dreamdata", ""),
            (demand, "traffic", "Grow qualified top-of-funnel with SEO/content.", "Ahrefs, Semrush, SurferSEO", ""),

            (conversion, "lead response", "Reduce time-to-first-touch with routing/booking.", "Calendly, Chili Piper, HubSpot Service Hub", ""),
            (conversion, "qualification", "Standardize lead qualification and CRM fields.", "Salesforce, Pipedrive, Clay", ""),
            (conversion, "landing page", "Run conversion tests and analyze behavior.", "Unbounce, VWO, Hotjar", ""),
            (conversion, "enablement", "Centralize decks, one-pagers, and case studies.", "Notion, Google Drive, Canva", ""),
            (conversion, "win/loss", "Capture reasons and iterate monthly.", "Pipedrive, Airtable, Attio", ""),

            (delivery, "onboarding", "Orchestrate onboarding tasks and in-app walkthroughs.", "Asana, ClickUp, Userflow", ""),
            (delivery, "nps", "Measure CSAT/NPS and act on insights.", "Delighted, Typeform, Hotjar Surveys", ""),
            (delivery, "renewal", "Automate lifecycle and renewal workflows.", "HubSpot, ChurnZero, Retently", ""),
            (delivery, "testimonial", "Collect and publish social proof.", "Trustpilot, Capterra, Senja.io", ""),
        ]
        for cat, keyword, description, tools_csv, url in TOOL_RECS:
            ToolRecommendation.objects.update_or_create(
                category=cat, keyword=keyword,
                defaults={"description": description, "tools": tools_csv, "url": (url or None)},
            )

        self.stdout.write(self.style.SUCCESS("Defaults loaded (categories, questions, bands, tool recommendations)."))
