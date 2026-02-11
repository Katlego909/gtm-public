from django import forms
from django.contrib.auth.models import User
from .models import GapAnalysisMetric
from gtm.models import ActionItem

class GapAnalysisMetricForm(forms.ModelForm):
    # Use choice fields so Category and Metric pull from database-defined choices
    category = forms.ChoiceField(
        choices=GapAnalysisMetric.CATEGORY_CHOICES,
        widget=forms.Select(attrs={'class': 'w-full p-2 border rounded'})
    )

    metric = forms.ChoiceField(
        choices=[(k, k) for k in GapAnalysisMetric.METRIC_FIELD_MAPPING.keys()],
        widget=forms.Select(attrs={'class': 'w-full p-2 border rounded'})
    )

    class Meta:
        model = GapAnalysisMetric
        fields = ['category', 'metric', 'current', 'target', 'priority', 'recommendation']
        widgets = {
            'current': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded'}),
            'target': forms.NumberInput(attrs={'class': 'w-full p-2 border rounded'}),
            'priority': forms.Select(attrs={'class': 'w-full p-2 border rounded'}),
            'recommendation': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # If editing an existing instance, keep the metric choices but ensure the current value is present
        if self.instance and self.instance.metric:
            metric_value = self.instance.metric
            if metric_value not in dict(self.fields['metric'].choices):
                # Add current metric to choices so it can be displayed for existing records
                self.fields['metric'].choices = [(metric_value, metric_value)] + list(self.fields['metric'].choices)

class ActionItemForm(forms.ModelForm):
    due_date = forms.DateField(
        required=False, 
        input_formats=['%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d'],
        widget=forms.DateInput(attrs={'class': 'w-full p-2 border rounded', 'type': 'date'})
    )
    owner = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Assignee'})
    )

    class Meta:
        model = ActionItem
        fields = ['note', 'status', 'due_date', 'owner']
        widgets = {
            'note': forms.TextInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Task description'}),
            'status': forms.Select(attrs={'class': 'w-full p-2 border rounded'}),
        }


class UserProfileForm(forms.ModelForm):
    first_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent', 'placeholder': 'First Name'})
    )
    last_name = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent', 'placeholder': 'Last Name'})
    )
    email = forms.EmailField(
        required=True,
        widget=forms.EmailInput(attrs={'class': 'w-full p-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-indigo-500 focus:border-transparent', 'placeholder': 'Email Address'})
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']

