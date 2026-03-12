from django import forms
from django.contrib.auth.models import User
from .models import GapAnalysisMetric, Resource

class ResourceForm(forms.ModelForm):
    class Meta:
        model = Resource
        fields = ['name', 'description', 'resource_type', 'category', 'file', 'url']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'Resource Name'}),
            'description': forms.Textarea(attrs={'class': 'w-full p-2 border rounded', 'rows': 2, 'placeholder': 'Brief description'}),
            'resource_type': forms.Select(attrs={'class': 'w-full p-2 border rounded', 'onchange': 'toggleResourceFields(this.value)'}),
            'category': forms.Select(attrs={'class': 'w-full p-2 border rounded'}),
            'file': forms.FileInput(attrs={'class': 'w-full p-2 border rounded'}),
            'url': forms.URLInput(attrs={'class': 'w-full p-2 border rounded', 'placeholder': 'https://...'}),
        }

    def clean(self):
        cleaned_data = super().clean()
        resource_type = cleaned_data.get('resource_type')
        file = cleaned_data.get('file')
        url = cleaned_data.get('url')

        if resource_type == 'file' and not file:
            self.add_error('file', 'Please upload a file for this resource type.')
        elif resource_type == 'link' and not url:
            self.add_error('url', 'Please provide a URL for this resource type.')
        
        return cleaned_data
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
    assigned_to = forms.ModelChoiceField(
        queryset=User.objects.none(),  # Start with empty, will be set in __init__
        required=False,
        empty_label="Select team member...",
        widget=forms.Select(attrs={'class': 'w-full p-2 border rounded'})
    )

    def __init__(self, *args, workspace=None, **kwargs):
        super().__init__(*args, **kwargs)
        
        if workspace:
            try:
                # Get team members from the workspace
                from gtm.models_workspace import WorkspaceMembership
                memberships = WorkspaceMembership.objects.filter(workspace=workspace).select_related('user')
                user_ids = [m.user.id for m in memberships]
                self.fields['assigned_to'].queryset = User.objects.filter(id__in=user_ids)
            except Exception as e:
                # Fallback to empty queryset if workspace model issues
                self.fields['assigned_to'].queryset = User.objects.none()
        else:
            self.fields['assigned_to'].queryset = User.objects.none()

    class Meta:
        model = ActionItem
        fields = ['note', 'status', 'due_date', 'assigned_to']
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

