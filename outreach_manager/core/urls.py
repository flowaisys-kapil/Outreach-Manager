# outreach_manager/core/urls.py
from django.urls import path
from outreach_manager.core.views import DashboardView

app_name = "core"

urlpatterns = [
    path("", DashboardView.as_view(), name="dashboard"),
]
