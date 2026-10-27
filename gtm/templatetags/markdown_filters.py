from django import template
from django.template.defaultfilters import stringfilter
from gtm.utils_markdown import render_markdown

register = template.Library()

@register.filter
@stringfilter
def markdownify(value):
    return render_markdown(value, extensions=['markdown.extensions.fenced_code'])
