from django.urls import path
from . import views

app_name = "gtm"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("start/", views.start_assessment, name="start"),
    path("resume/<uuid:session_id>/", views.resume_assessment, name="resume"),
    path("resume/latest/", views.resume_latest, name="resume_latest"),
    path("assessment/<uuid:session_id>/<int:step>/", views.assessment_step, name="assessment_step"),
    path("results/<uuid:session_id>/", views.results, name="results"),
    path("playbook/<uuid:session_id>/", views.playbook, name="playbook"),
    path("download/<uuid:session_id>/", views.download_report_pdf, name="download"),
    path("history/", views.history, name="history"),
    
    path("actions/add/<uuid:session_id>/", views.action_add, name="action_add"),
    path("actions/toggle/<int:action_id>/", views.action_toggle, name="action_toggle"),
    path("actions/update/<int:action_id>/", views.action_update, name="action_update"),
    path("actions/delete/<int:action_id>/", views.action_delete, name="action_delete"),

]
