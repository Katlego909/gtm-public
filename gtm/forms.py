from django import forms
from .models import AssessmentSession

class StartAssessmentForm(forms.ModelForm):
    class Meta:
        model = AssessmentSession
        fields = [
            "company_name", "industry", "website", "contact_name",
            "contact_email", "contact_role", "phone", "company_size",
            "revenue_range", "country", "crm", "notes",
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
