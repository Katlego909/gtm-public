# gtm/management/commands/load_tool_recommendations.py
from django.core.management.base import BaseCommand
from gtm.models import Category, ToolRecommendation

class Command(BaseCommand):
    help = "Load default tool recommendations for GTM categories"

    def handle(self, *args, **options):
        # Clear existing recommendations
        ToolRecommendation.objects.all().delete()
        
        # Get all categories (matching your 3-category structure: Demand, Conversion, Delivery)
        try:
            demand = Category.objects.get(name__iexact="Demand")
            conversion = Category.objects.get(name__iexact="Conversion")
            delivery = Category.objects.get(name__iexact="Delivery")
        except Category.DoesNotExist as e:
            self.stdout.write(self.style.ERROR(f"Category not found: {e}"))
            self.stdout.write(self.style.WARNING("Make sure you've loaded GTM defaults first: python manage.py load_gtm_defaults"))
            self.stdout.write(self.style.WARNING("Expected categories: Demand, Conversion, Delivery"))
            return

        recommendations = [
            # DEMAND GENERATION - Tools for attracting and generating leads
            {
                "category": demand,
                "keyword": "lead generation",
                "description": "Tools to capture and qualify inbound leads",
                "tools": "HubSpot, Marketo, Pardot, Drift, Intercom",
                "url": "https://www.hubspot.com"
            },
            {
                "category": demand,
                "keyword": "marketing automation",
                "description": "Platforms for automating marketing campaigns",
                "tools": "HubSpot Marketing Hub, ActiveCampaign, Mailchimp, Klaviyo",
                "url": "https://www.hubspot.com/products/marketing"
            },
            {
                "category": demand,
                "keyword": "SEO",
                "description": "Search engine optimization and content discovery",
                "tools": "Ahrefs, SEMrush, Moz, Clearscope, SurferSEO",
                "url": "https://ahrefs.com"
            },
            {
                "category": demand,
                "keyword": "content",
                "description": "Content creation and management platforms",
                "tools": "Jasper.ai, Copy.ai, Grammarly, Canva, Figma",
                "url": "https://www.jasper.ai"
            },
            {
                "category": demand,
                "keyword": "advertising",
                "description": "Paid advertising platforms and analytics",
                "tools": "Google Ads, LinkedIn Ads, Facebook Ads, AdRoll, Metadata.io",
                "url": "https://ads.google.com"
            },
            {
                "category": demand,
                "keyword": "social media",
                "description": "Social media management and scheduling",
                "tools": "Hootsuite, Buffer, Sprout Social, Later, Planoly",
                "url": "https://hootsuite.com"
            },
            {
                "category": demand,
                "keyword": "webinar",
                "description": "Webinar and virtual event platforms",
                "tools": "Zoom, Demio, WebinarJam, GoToWebinar, Livestorm",
                "url": "https://zoom.us"
            },
            {
                "category": demand,
                "keyword": "email",
                "description": "Email marketing and campaign management",
                "tools": "Mailchimp, SendGrid, Constant Contact, Campaign Monitor",
                "url": "https://mailchimp.com"
            },
            {
                "category": demand,
                "keyword": "analytics",
                "description": "Website analytics and visitor tracking",
                "tools": "Google Analytics, Mixpanel, Heap, Hotjar, Amplitude",
                "url": "https://analytics.google.com"
            },
            {
                "category": demand,
                "keyword": "attribution",
                "description": "Marketing attribution and ROI tracking",
                "tools": "Bizible, DreamData, HockeyStack, Attribution, Ruler Analytics",
                "url": "https://www.adobe.com/marketo/bizible"
            },
            
            # CONVERSION - Tools for converting leads to customers
            {
                "category": conversion,
                "keyword": "CRM",
                "description": "Customer relationship management systems",
                "tools": "Salesforce, HubSpot CRM, Pipedrive, Zoho CRM, Copper",
                "url": "https://www.salesforce.com"
            },
            {
                "category": conversion,
                "keyword": "sales engagement",
                "description": "Tools for sales outreach and follow-up",
                "tools": "Outreach, SalesLoft, Apollo.io, Reply.io, Lemlist",
                "url": "https://www.outreach.io"
            },
            {
                "category": conversion,
                "keyword": "demo",
                "description": "Product demo and interactive walkthrough tools",
                "tools": "Navattic, Demostack, Storylane, Walnut, Reprise",
                "url": "https://www.navattic.com"
            },
            {
                "category": conversion,
                "keyword": "proposal",
                "description": "Proposal and quote generation software",
                "tools": "PandaDoc, Proposify, Better Proposals, Qwilr, DocSend",
                "url": "https://www.pandadoc.com"
            },
            {
                "category": conversion,
                "keyword": "meeting",
                "description": "Scheduling and calendar management",
                "tools": "Calendly, Chili Piper, Savvycal, Cal.com, Reclaim.ai",
                "url": "https://calendly.com"
            },
            {
                "category": conversion,
                "keyword": "qualification",
                "description": "Lead scoring and qualification platforms",
                "tools": "Clearbit, ZoomInfo, 6sense, Demandbase, Bombora",
                "url": "https://clearbit.com"
            },
            {
                "category": conversion,
                "keyword": "sales enablement",
                "description": "Sales content and training platforms",
                "tools": "Highspot, Seismic, Showpad, Lessonly, Allego",
                "url": "https://www.highspot.com"
            },
            {
                "category": conversion,
                "keyword": "call",
                "description": "Sales call recording and intelligence",
                "tools": "Gong, Chorus.ai, Jiminny, ExecVision, Wingman",
                "url": "https://www.gong.io"
            },
            {
                "category": conversion,
                "keyword": "form",
                "description": "Form and landing page optimization",
                "tools": "Unbounce, Instapage, Leadpages, Typeform, Tally",
                "url": "https://unbounce.com"
            },
            {
                "category": conversion,
                "keyword": "pricing",
                "description": "Pricing optimization and CPQ tools",
                "tools": "ProfitWell, Chargify, Chargebee, DealHub, Conga CPQ",
                "url": "https://www.profitwell.com"
            },
            
            # DELIVERY - Tools for customer success, retention, and expansion
            {
                "category": delivery,
                "keyword": "customer success",
                "description": "Customer success and health monitoring",
                "tools": "Gainsight, ChurnZero, Totango, Planhat, Vitally",
                "url": "https://www.gainsight.com"
            },
            {
                "category": delivery,
                "keyword": "support",
                "description": "Customer support and helpdesk systems",
                "tools": "Zendesk, Intercom, Freshdesk, Help Scout, Front",
                "url": "https://www.zendesk.com"
            },
            {
                "category": delivery,
                "keyword": "onboarding",
                "description": "Customer onboarding and product adoption",
                "tools": "Userpilot, Appcues, Pendo, WalkMe, Chameleon",
                "url": "https://userpilot.com"
            },
            {
                "category": delivery,
                "keyword": "feedback",
                "description": "Customer feedback and survey tools",
                "tools": "Typeform, SurveyMonkey, Delighted, Qualtrics, UserVoice",
                "url": "https://www.typeform.com"
            },
            {
                "category": delivery,
                "keyword": "community",
                "description": "Community building and engagement platforms",
                "tools": "Circle, Discourse, Slack, Discord, Mighty Networks",
                "url": "https://circle.so"
            },
            {
                "category": delivery,
                "keyword": "product analytics",
                "description": "Product usage analytics and insights",
                "tools": "Amplitude, Mixpanel, Heap, Pendo, PostHog",
                "url": "https://amplitude.com"
            },
            {
                "category": delivery,
                "keyword": "upsell",
                "description": "Account expansion and upsell intelligence",
                "tools": "Pocus, Correlated, Catalyst, ChurnKey, ProfitWell",
                "url": "https://www.pocus.com"
            },
            {
                "category": delivery,
                "keyword": "account management",
                "description": "Strategic account management platforms",
                "tools": "Planhat, ClientSuccess, Strikedeck, Kapta, Catalyst",
                "url": "https://www.planhat.com"
            },
            {
                "category": delivery,
                "keyword": "training",
                "description": "Customer training and certification programs",
                "tools": "Skilljar, Thought Industries, LearnWorlds, TalentLMS, Docebo",
                "url": "https://www.skilljar.com"
            },
            {
                "category": delivery,
                "keyword": "documentation",
                "description": "Customer-facing docs and knowledge bases",
                "tools": "Notion, Confluence, GitBook, Document360, Readme.io",
                "url": "https://www.notion.so"
            },
            {
                "category": delivery,
                "keyword": "referral",
                "description": "Referral and advocacy program management",
                "tools": "ReferralCandy, Influitive, Cello, Refersion, Ambassador",
                "url": "https://www.referralcandy.com"
            },
            {
                "category": delivery,
                "keyword": "retention",
                "description": "Churn prediction and retention automation",
                "tools": "ChurnZero, ProfitWell Retain, Brightback, Recurly",
                "url": "https://churnzero.com"
            },
        ]

        created_count = 0
        for rec_data in recommendations:
            ToolRecommendation.objects.create(**rec_data)
            created_count += 1
            self.stdout.write(self.style.SUCCESS(f"Created: {rec_data['keyword']} for {rec_data['category'].name}"))

        self.stdout.write(self.style.SUCCESS(f"\nSuccessfully loaded {created_count} tool recommendations!"))
