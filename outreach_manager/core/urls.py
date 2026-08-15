# openoutreach/core/urls.py
from django.urls import path
from outreach_manager.core.views import DashboardView, SessionHistoryView, SessionDetailView

app_name = "core"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
    path("history/", SessionHistoryView.as_view(), name="session_history"),
    path("history/<str:session_id>/", SessionDetailView.as_view(), name="session_detail"),
]
