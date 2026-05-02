from django.urls import path
from . import views

urlpatterns = [
    path('', views.landing, name='landing'),
    path('home/', views.home, name='home'),
    path('generate/', views.generate_letter, name='generate_letter'),
    path('generate-resume/', views.generate_resume, name='generate_resume'),
    path('download-pdf/', views.generate_pdf, name='download_pdf'),
    path('export-resume-pdf/', views.export_resume_pdf, name='export_resume_pdf'),
    path('history/', views.history, name='history'),
    path('history/<int:pk>/delete/', views.delete_generation, name='delete_generation'),

    # New features
    path('scrape-job/', views.scrape_job_url, name='scrape_job'),
    path('parse-resume-pdf/', views.parse_resume_pdf, name='parse_resume_pdf'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('tools/', views.tools, name='tools'),
    path('interview-prep/', views.interview_prep, name='interview_prep'),
    path('followup-email/', views.followup_email, name='followup_email'),
    path('ats-score/', views.ats_score, name='ats_score'),
    path('tracker/', views.tracker, name='tracker'),
    path('tracker/<int:pk>/update/', views.tracker_update, name='tracker_update'),
    path('tracker/<int:pk>/delete/', views.tracker_delete, name='tracker_delete'),
]