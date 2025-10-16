from django.contrib import admin
from .models import Category, Question, AssessmentSession, Response, RecommendationBand, ToolRecommendation

admin.site.register(Category)
admin.site.register(Question)
admin.site.register(AssessmentSession)
admin.site.register(Response)
admin.site.register(RecommendationBand)


@admin.register(ToolRecommendation)
class ToolRecommendationAdmin(admin.ModelAdmin):
    list_display = ("category", "keyword", "tools")
    search_fields = ("keyword", "tools", "description")
