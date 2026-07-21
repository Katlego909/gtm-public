# gtm/forms_beta.py
"""
ACCOUNT_SIGNUP_FORM_CLASS mixin (see gtm_validator/settings.py) -- adds the
invite-code field allauth's stock signup form doesn't have. Per allauth's
own docs this is a plain forms.Form with just the extra field(s), not a
SignupForm subclass; its `signup(request, user)` hook is called once the
underlying user account has actually been created.
"""
from django import forms
from django.utils import timezone

from .models_beta import BetaInviteCode


class BetaInviteSignupForm(forms.Form):
    invite_code = forms.CharField(
        max_length=16,
        label="Beta invite code",
        widget=forms.TextInput(attrs={
            "class": "w-full border rounded px-3 py-2",
            "placeholder": "Enter your invite code",
            "autocomplete": "off",
        }),
    )

    def clean_invite_code(self):
        raw = self.cleaned_data["invite_code"].strip().upper()
        try:
            invite = BetaInviteCode.objects.get(code=raw)
        except BetaInviteCode.DoesNotExist:
            raise forms.ValidationError(
                "This beta is invite-only and that code wasn't recognized. "
                "Double-check the code from your invite, or contact us at hello@funti3r.xyz."
            )
        if invite.is_used:
            raise forms.ValidationError(
                "That invite code has already been used. Contact us at hello@funti3r.xyz if you need a new one."
            )
        if invite.is_expired:
            raise forms.ValidationError(
                "That invite code has expired. Contact us at hello@funti3r.xyz for a new one."
            )
        self._invite = invite
        return raw

    def signup(self, request, user):
        invite = self._invite
        invite.used_by = user
        invite.used_at = timezone.now()
        invite.save(update_fields=["used_by", "used_at"])
