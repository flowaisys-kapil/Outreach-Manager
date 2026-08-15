# openoutreach/chat/admin.py
from django.contrib import admin

from outreach_manager.chat.models import ChatMessage


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("deal", "is_outgoing", "owner", "creation_date")
    list_filter = ("is_outgoing", "owner")
    raw_id_fields = ("deal", "owner", "answer_to", "topic")
    date_hierarchy = "creation_date"
    readonly_fields = ("deal", "content", "owner", "creation_date")
