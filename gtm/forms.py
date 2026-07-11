from django import forms
from .models import AssessmentSession
from .models_workspace import WorkspaceInvitation

# Curated dropdown options for start.html's "select + Other" fields.
# Plain string lists (not model choices=) so a legacy/custom value that isn't
# in the list can still round-trip via the "Other" fallback.
INDUSTRY_OPTIONS = [
    "SaaS/Software", "Fintech", "Healthcare/HealthTech", "E-commerce/Retail",
    "Marketing/AdTech", "EdTech", "Real Estate/PropTech", "Manufacturing",
    "Professional Services/Consulting", "Media/Entertainment",
    "Logistics/Supply Chain", "Cybersecurity", "HR/Recruiting", "Non-profit",
]

ROLE_OPTIONS = [
    "Founder/CEO", "Co-Founder", "CMO/VP Marketing", "Head of Growth",
    "VP Sales/CRO", "Head of Product", "Operations Lead", "Consultant/Advisor",
]

COUNTRY_OPTIONS = [
    "South Africa", "United States", "United Kingdom", "Canada", "Australia",
    "Germany", "France", "Netherlands", "Nigeria", "Kenya", "Ghana", "India",
    "Singapore", "United Arab Emirates", "Brazil", "Ireland", "Sweden",
    "Spain", "Italy", "New Zealand", "Israel", "Switzerland",
]

CRM_OPTIONS = [
    "HubSpot", "Salesforce", "Pipedrive", "Zoho CRM", "Close",
    "Microsoft Dynamics 365", "Freshsales", "Copper", "ActiveCampaign",
    "monday.com CRM", "None / Not using a CRM",
]


class StartAssessmentForm(forms.ModelForm):
    class Meta:
        model = AssessmentSession
        fields = [
            "company_name", "industry", "website", "contact_name",
            "contact_email", "contact_role", "phone", "company_size",
            "revenue_range", "country", "crm", "company_stage", "notes",
            "utm_source", "utm_medium", "utm_campaign", "referrer"
        ]
        # Basic widgets; customize attrs as needed for styling (e.g., 'form-control', Tailwind classes)
        # For simplicity, I'm adding `forms.TextInput` as a default, assuming a simple text input for all.
        # HiddenInput for UTM/referrer fields if they are captured from hidden form fields/JS
        widgets = {
            "company_name": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "industry": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "website": forms.URLInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "contact_name": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "contact_email": forms.EmailInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "contact_role": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "phone": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "company_size": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "revenue_range": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "country": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "crm": forms.TextInput(attrs={'class': 'w-full border rounded px-3 py-2'}),
            "notes": forms.Textarea(attrs={'class': 'w-full border rounded px-3 py-2', 'rows': 3}),
            "utm_source": forms.HiddenInput(),
            "utm_medium": forms.HiddenInput(),
            "utm_campaign": forms.HiddenInput(),
            "referrer": forms.HiddenInput(),
        }

class WorkspaceInvitationForm(forms.ModelForm):
    class Meta:
        model = WorkspaceInvitation
        fields = ['email', 'role']
        widgets = {
            'email': forms.EmailInput(attrs={'class': 'w-full border rounded px-3 py-2', 'placeholder': 'Enter email to invite'}),
            'role': forms.Select(attrs={'class': 'w-full border rounded px-3 py-2'}),
        }
