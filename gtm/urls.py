from django.urls import path, include
from . import views

app_name = "gtm"

urlpatterns = [
    path("", views.landing, name="landing"),
    path("start/", views.start_assessment, name="start"),
    path("resume/<uuid:session_id>/", views.resume_assessment, name="resume"),
    path("resume/latest/", views.resume_latest, name="resume_latest"),
    path("assessment/<uuid:session_id>/<int:step>/", views.assessment_step, name="assessment_step"),
    path("assessment/<uuid:session_id>/rewrite-context-note/", views.rewrite_context_note, name="rewrite_context_note"),
    path("assessment/<uuid:session_id>/edit-details/", views.edit_assessment_intro, name="edit_assessment_intro"),
    path("results/<uuid:session_id>/", views.results, name="results"),
    path("results/<uuid:session_id>/insight/<int:response_id>/", views.insight_status, name="insight_status"),
    path("playbook/<uuid:session_id>/", views.playbook, name="playbook"),
    path("playbook-status/<uuid:session_id>/", views.playbook_status, name="playbook_status"),
    path("playbook-content-status/<uuid:session_id>/", views.playbook_content_status, name="playbook_content_status"),
    path("next-moves-content/<uuid:session_id>/", views.next_moves_content, name="next_moves_content"),
    path("playbook-footer-status/<uuid:session_id>/", views.playbook_footer_status, name="playbook_footer_status"),
    path("enrichment-status/<uuid:session_id>/", views.enrichment_status, name="enrichment_status"),
    path("download/<uuid:session_id>/", views.download_report_pdf, name="download"),
    path("history/", views.history, name="history"),
    path("cancel/<uuid:session_id>/", views.cancel_assessment, name="cancel_assessment"),
    
    path("actions/add/<uuid:session_id>/", views.action_add, name="action_add"),
    path("actions/toggle/<int:action_id>/", views.action_toggle, name="action_toggle"),
    path("actions/update/<int:action_id>/", views.action_update, name="action_update"),
    path("actions/delete/<int:action_id>/", views.action_delete, name="action_delete"),
    
    # Evidence upload (used by workspace agent)
    path("evidence/upload/<uuid:session_id>/", views.upload_strategic_evidence, name="upload_evidence"),

    # Delivery document analysis
    path("delivery/<uuid:session_id>/upload/", views.upload_delivery_document, name="upload_delivery_document"),
    path("delivery/<uuid:session_id>/analyze/", views.analyze_delivery_documents, name="analyze_delivery_documents"),
    path("delivery/<uuid:session_id>/documents/", views.get_delivery_documents, name="get_delivery_documents"),
    path("delivery/<uuid:session_id>/delete/<uuid:doc_id>/", views.delete_delivery_document, name="delete_delivery_document"),

    # Demand / Conversion document analysis (generic category endpoints)
    path("category/<str:category>/<uuid:session_id>/upload/", views.upload_category_document, name="upload_category_document"),
    path("category/<str:category>/<uuid:session_id>/analyze/", views.analyze_category_documents, name="analyze_category_documents"),
    path("category/<str:category>/<uuid:session_id>/documents/", views.get_category_documents, name="get_category_documents"),
    path("category/<str:category>/<uuid:session_id>/delete/<uuid:doc_id>/", views.delete_category_document, name="delete_category_document"),
    
    # User Profile & Auth
    path("profile/", views.profile, name="profile"),
    path("logout/", views.logout_view, name="logout"),
    
    # Workspace management
    path("workspace/", include('gtm.urls_workspace')),

]
