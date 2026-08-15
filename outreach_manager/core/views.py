# openoutreach/core/views.py
import os
import sys
import subprocess
import logging
from django.views.generic import View
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.contrib import messages
from django.conf import settings
from django.utils import timezone
from django.db import models
from django.core.paginator import Paginator

from outreach_manager.core.models import Campaign, SiteConfig, Task
from outreach_manager.core.config import get_config
from outreach_manager.core import config_service as _cfg_svc
from outreach_manager.crm.models.lead import Lead
from outreach_manager.crm.models.deal import Deal, DealState, Outcome
from outreach_manager.chat.models import ChatMessage
from outreach_manager.linkedin.models import LinkedInProfile

logger = logging.getLogger(__name__)

def is_pid_running(pid):
    if pid <= 0:
        return False
    try:
        # Standard way to check if process exists on Windows
        output = subprocess.check_output(["tasklist", "/FI", f"PID eq {pid}"], text=True)
        return str(pid) in output
    except Exception:
        return False

def get_log_tail(n=100):
    log_path = os.path.join(settings.BASE_DIR, "data", "outreach.log")
    if not os.path.exists(log_path):
        return "No outreach cycles have run yet. Click 'Start Outreach Cycle' to begin."
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception as e:
        return f"Error reading log file: {str(e)}"

def read_env_file():
    """Return managed config keys from the live .env via ConfigurationService.

    Kept for backward compatibility with template context that expects
    ``env_config`` to be a dict.  New code should call
    ``config_service.read_current_config()`` directly.
    """
    return _cfg_svc.read_current_config()

class DashboardView(View):
    def get(self, request):
        campaign_id = request.GET.get("campaign_id")
        if campaign_id:
            campaign = Campaign.objects.filter(pk=campaign_id).first()
        else:
            campaign = Campaign.objects.first()

        all_campaigns = list(Campaign.objects.order_by("name"))
        main_campaign = campaign

        profile = LinkedInProfile.objects.first()
        site_config = SiteConfig.load()
        env_config = read_env_file()
        
        # Check and auto-expire task override
        if site_config.simulated_task and site_config.override_expires_at:
            if timezone.now() > site_config.override_expires_at:
                site_config.simulated_task = ""
                site_config.override_expires_at = None
                site_config.save()

        seconds_left = 0
        if site_config.simulated_task and site_config.override_expires_at:
            seconds_left = max(0, int((site_config.override_expires_at - timezone.now()).total_seconds()))

        pid_file = os.path.join(settings.BASE_DIR, "data", "daemon.pid")
        is_running = False
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    pid = int(f.read().strip())
                is_running = is_pid_running(pid)
            except Exception:
                pass

        # AJAX Endpoint for log polling
        if request.GET.get("action") == "get_logs":
            return JsonResponse({
                "logs": get_log_tail(),
                "is_running": is_running,
                "simulated_task": site_config.simulated_task,
                "seconds_left": seconds_left,
            })

        # Check for "today" toggle for funnel stats
        is_today = request.GET.get("today") == "1"

        # Calculate metrics
        if campaign:
            deals_for_campaign = Deal.objects.filter(campaign=campaign)
            total_leads = deals_for_campaign.count()
            disqualified_leads = deals_for_campaign.filter(lead__disqualified=True).count()
            active_deals = deals_for_campaign.filter(lead__disqualified=False).count()
            converted_deals = deals_for_campaign.filter(outcome=Outcome.CONVERTED).count()

            # Funnel stats (filtered by today if today toggle active)
            if is_today:
                import datetime
                today_start = timezone.make_aware(datetime.datetime.combine(timezone.localdate(), datetime.time.min))
                funnel_deals = deals_for_campaign.filter(update_date__gte=today_start)
            else:
                funnel_deals = deals_for_campaign

            deals_by_state = {state[0]: funnel_deals.filter(state=state[0]).count() for state in DealState.choices}
            meetings_count = deals_for_campaign.filter(state=DealState.MEETING_SCHEDULED).count()
            closed_won_count = deals_for_campaign.filter(state=DealState.CLOSED_WON).count()
            closed_lost_count = deals_for_campaign.filter(state=DealState.CLOSED_LOST).count()
            
            outgoing_msg = ChatMessage.objects.filter(deal__campaign=campaign, is_outgoing=True).count()
            incoming_msg = ChatMessage.objects.filter(deal__campaign=campaign, is_outgoing=False).count()

            requests_sent = (
                deals_for_campaign.filter(state__in=[DealState.PENDING, DealState.CONNECTED, DealState.COMPLETED]).count()
                + deals_for_campaign.filter(state=DealState.FAILED, connect_attempts__gt=0).count()
            )
            requests_accepted = deals_for_campaign.filter(state__in=[DealState.CONNECTED, DealState.COMPLETED]).count()
            acceptance_rate = round(requests_accepted / requests_sent * 100, 1) if requests_sent > 0 else 0.0

            replied_deals_count = ChatMessage.objects.filter(deal__campaign=campaign, is_outgoing=False).values('deal').distinct().count()
            reply_rate = round(replied_deals_count / requests_accepted * 100, 1) if requests_accepted > 0 else 0.0
            conversion_rate = round(converted_deals / deals_for_campaign.count() * 100, 1) if deals_for_campaign.exists() else 0.0

            # Tasks stats
            pending_tasks = Task.objects.pending().filter(payload__campaign_id=campaign.pk)
            next_task = pending_tasks.first()
            tasks_stats = {
                "pending": Task.objects.filter(status=Task.Status.PENDING, payload__campaign_id=campaign.pk).count(),
                "running": Task.objects.filter(status=Task.Status.RUNNING, payload__campaign_id=campaign.pk).count(),
                "completed": Task.objects.filter(status=Task.Status.COMPLETED, payload__campaign_id=campaign.pk).count(),
                "failed": Task.objects.filter(status=Task.Status.FAILED, payload__campaign_id=campaign.pk).count(),
            }
        else:
            total_leads = 0
            disqualified_leads = 0
            active_deals = 0
            converted_deals = 0
            meetings_count = 0
            closed_won_count = 0
            closed_lost_count = 0
            deals_by_state = {state[0]: 0 for state in DealState.choices}
            outgoing_msg = 0
            incoming_msg = 0
            requests_sent = 0
            requests_accepted = 0
            acceptance_rate = 0.0
            reply_rate = 0.0
            conversion_rate = 0.0
            next_task = None
            tasks_stats = {"pending": 0, "running": 0, "completed": 0, "failed": 0}

        # Paginate leads table
        state_filter = request.GET.get("state", "")
        search_query = request.GET.get("q", "")
        deals_query = Deal.objects.select_related("lead", "campaign").order_by("-update_date")
        if campaign:
            deals_query = deals_query.filter(campaign=campaign)

        if state_filter:
            deals_query = deals_query.filter(state=state_filter)
        if search_query:
            deals_query = deals_query.filter(
                models.Q(lead__public_identifier__icontains=search_query) |
                models.Q(lead__linkedin_url__icontains=search_query)
            )

        paginator = Paginator(deals_query, 10)
        page_number = request.GET.get("page", 1)
        deals_page = paginator.get_page(page_number)

        # Selected date for daily stats filtering
        import datetime
        from django.db.models.functions import TruncDate
        from django.db.models import Count
        from outreach_manager.crm.models.event_log import EventLog

        selected_date_str = request.GET.get("stats_date", "")
        if selected_date_str:
            try:
                selected_date = datetime.datetime.strptime(selected_date_str, "%Y-%m-%d").date()
            except ValueError:
                selected_date = timezone.localdate()
        else:
            selected_date = timezone.localdate()
            
        selected_date_formatted = selected_date.strftime("%Y-%m-%d")
        
        # Filter event logs and stats for the selected date
        if campaign:
            day_start = timezone.make_aware(datetime.datetime.combine(selected_date, datetime.time.min))
            day_end = timezone.make_aware(datetime.datetime.combine(selected_date, datetime.time.max))
            
            daily_events = list(EventLog.objects.filter(
                campaign=campaign,
                created_at__range=(day_start, day_end)
            ).select_related("deal__lead").order_by("created_at"))
            
            daily_stats = {
                "connect_requested": sum(1 for e in daily_events if e.event_type == EventLog.EventType.CONNECT_REQUESTED),
                "connect_accepted": sum(1 for e in daily_events if e.event_type == EventLog.EventType.CONNECT_ACCEPTED),
                "message_sent": sum(1 for e in daily_events if e.event_type == EventLog.EventType.MESSAGE_SENT),
                "email_sent": sum(1 for e in daily_events if e.event_type == EventLog.EventType.EMAIL_SENT),
                "reply_received": sum(1 for e in daily_events if e.event_type == EventLog.EventType.MESSAGE_RECEIVED),
            }
        else:
            daily_events = []
            daily_stats = {
                "connect_requested": 0,
                "connect_accepted": 0,
                "message_sent": 0,
                "email_sent": 0,
                "reply_received": 0,
            }

        # Daily Stats Chart Data (last 7 days by default)
        today_local = timezone.localdate()
        date_list = [today_local - datetime.timedelta(days=i) for i in range(6, -1, -1)]
        date_strs = [d.strftime("%Y-%m-%d") for d in date_list]
        
        chart_data = {
            "dates": date_strs,
            "connect_requested": [0] * 7,
            "connect_accepted": [0] * 7,
            "message_sent": [0] * 7,
            "email_sent": [0] * 7,
        }
        
        seven_days_ago = timezone.now() - datetime.timedelta(days=7)
        if campaign:
            event_logs_last_7 = (
                EventLog.objects.filter(campaign=campaign, created_at__gte=seven_days_ago)
                .annotate(day=TruncDate("created_at"))
                .values("day", "event_type")
                .annotate(count=Count("id"))
            )
            for entry in event_logs_last_7:
                entry_date_str = entry["day"].strftime("%Y-%m-%d")
                if entry_date_str in date_strs:
                    idx = date_strs.index(entry_date_str)
                    etype = entry["event_type"]
                    if etype in chart_data:
                        chart_data[etype][idx] = entry["count"]

        # Convert lists to JS-friendly lists for Template
        import json
        chart_data_json = json.dumps(chart_data)

        context = {
            "campaign": campaign,
            "is_today": is_today,
            "selected_date": selected_date_formatted,
            "daily_events": daily_events,
            "daily_stats": daily_stats,
            "chart_data_json": chart_data_json,
            "main_campaign": main_campaign,
            "all_campaigns": all_campaigns,
            "profile": profile,
            "site_config": site_config,
            "env_config": env_config,
            "config": get_config(),
            "is_running": is_running,
            "seconds_left": seconds_left,
            "total_leads": total_leads,
            "disqualified_leads": disqualified_leads,
            "active_deals": active_deals,
            "converted_deals": converted_deals,
            "meetings_count": meetings_count,
            "closed_won_count": closed_won_count,
            "closed_lost_count": closed_lost_count,
            "deals_by_state": deals_by_state,
            "outgoing_msg": outgoing_msg,
            "incoming_msg": incoming_msg,
            "requests_sent": requests_sent,
            "requests_accepted": requests_accepted,
            "acceptance_rate": acceptance_rate,
            "reply_rate": reply_rate,
            "conversion_rate": conversion_rate,
            "next_task": next_task,
            "tasks_stats": tasks_stats,
            "deals_page": deals_page,
            "state_filter": state_filter,
            "search_query": search_query,
            "deal_states": DealState.choices,
            "deal_outcomes": Outcome.choices,
            "mailboxes": lambda: [
                {"id": m.pk, "username": m.username}
                for m in __import__("outreach_manager.emails.models", fromlist=["Mailbox"]).Mailbox.objects.all()
            ],
        }
        return render(request, "core/dashboard.html", context)

    def post(self, request):
        action = request.POST.get("action")
        campaign_id = request.POST.get("campaign_id")

        if campaign_id:
            campaign = Campaign.objects.filter(pk=campaign_id).first()
        else:
            campaign = Campaign.objects.first()

        profile = LinkedInProfile.objects.first()
        site_config = SiteConfig.load()

        if action == "test_connection":
            provider = request.POST.get("provider", "").strip()
            model = request.POST.get("model", "").strip()
            api_key = request.POST.get("api_key", "").strip()
            api_base = request.POST.get("api_base", "").strip() or None

            from outreach_manager.core.llm import test_provider_connection
            is_ok, msg = test_provider_connection(provider=provider, model=model, api_key=api_key, api_base=api_base)
            return JsonResponse({"success": is_ok, "message": msg, "error": "" if is_ok else msg})

        elif action == "save_configuration":
            from outreach_manager.core.config import get_config, reset_config, SUPPORTED_AI_PROVIDERS

            # --- Extract input fields ---
            campaign_obj_text = request.POST.get("campaign_objective", "").strip()
            product_docs_text = request.POST.get("product_docs", "").strip()
            booking_link_text = request.POST.get("booking_link", "").strip()

            execution_mode = request.POST.get("execution_mode", "manual").strip().lower()
            try:
                sessions_per_day = int(request.POST.get("sessions_per_day", 1))
            except ValueError:
                sessions_per_day = 0

            try:
                working_start_hour = int(request.POST.get("working_start_hour", 9))
                working_end_hour = int(request.POST.get("working_end_hour", 19))
            except ValueError:
                working_start_hour, working_end_hour = 9, 19

            active_days = request.POST.getlist("active_days")
            if not active_days:
                active_days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

            browser_visibility = request.POST.get("browser_visibility", "hidden").strip().lower()
            enabled_workflows = request.POST.getlist("enabled_workflows")
            if not enabled_workflows:
                enabled_workflows = ["connect", "reply", "follow_up", "first_message", "check_pending", "extract_leads", "email"]

            try:
                connect_limit = int(request.POST.get("connect_daily_limit", 20))
                reply_limit = int(request.POST.get("reply_daily_limit", 40))
                follow_up_limit = int(request.POST.get("follow_up_daily_limit", 30))
                first_message_limit = int(request.POST.get("first_message_daily_limit", 20))
                check_pending_limit = int(request.POST.get("check_pending_daily_limit", 50))
                extract_leads_limit = int(request.POST.get("extract_leads_daily_limit", 100))
                email_limit = int(request.POST.get("email_daily_limit", 30))
            except ValueError:
                connect_limit = reply_limit = follow_up_limit = first_message_limit = check_pending_limit = extract_leads_limit = email_limit = -1

            session_history_enabled = request.POST.get("session_history_enabled") in ("1", "true", "on") or "session_history_enabled" in request.POST
            ai_usage_tracking_enabled = request.POST.get("ai_usage_tracking_enabled") in ("1", "true", "on") or "ai_usage_tracking_enabled" in request.POST
            notifications_enabled = request.POST.get("notifications_enabled") in ("1", "true", "on") or "notifications_enabled" in request.POST
            notify_on_success = request.POST.get("notify_on_success") in ("1", "true", "on") or "notify_on_success" in request.POST
            notify_on_warning = request.POST.get("notify_on_warning") in ("1", "true", "on") or "notify_on_warning" in request.POST
            notify_on_failure = request.POST.get("notify_on_failure") in ("1", "true", "on") or "notify_on_failure" in request.POST
            notify_on_info = request.POST.get("notify_on_info") in ("1", "true", "on") or "notify_on_info" in request.POST
            notification_delivery_mode = request.POST.get("notification_delivery_mode", "toast").strip().lower()
            color_enabled = request.POST.get("color_enabled") in ("1", "true", "on") or "color_enabled" in request.POST

            primary_provider = request.POST.get("primary_provider", "google").strip().lower()
            primary_model = request.POST.get("primary_model", "").strip()
            primary_api_key = request.POST.get("primary_api_key", "").strip()
            primary_api_base = request.POST.get("primary_api_base", "").strip()

            fallback_provider = request.POST.get("fallback_provider", "").strip().lower()
            fallback_model = request.POST.get("fallback_model", "").strip()
            fallback_api_key = request.POST.get("fallback_api_key", "").strip()
            fallback_api_base = request.POST.get("fallback_api_base", "").strip()

            try:
                rate_limit_delay = float(request.POST.get("rate_limit_delay", 3.0))
            except ValueError:
                rate_limit_delay = -1.0

            enable_fallback = request.POST.get("enable_fallback") in ("1", "true", "on") or "enable_fallback" in request.POST
            structured_output = request.POST.get("structured_output") in ("1", "true", "on") or "structured_output" in request.POST

            # --- Validation ---
            errors = []
            if not (1 <= sessions_per_day <= 5):
                errors.append("Sessions per day must be an integer between 1 and 5.")
            if working_start_hour < 0 or working_end_hour > 24 or working_start_hour >= working_end_hour:
                errors.append("Working start hour must be strictly earlier than working end hour.")
            if any(l <= 0 for l in (connect_limit, reply_limit, follow_up_limit, first_message_limit, check_pending_limit, extract_leads_limit, email_limit)):
                errors.append("Daily workflow limits must be positive integers.")
            if primary_provider not in SUPPORTED_AI_PROVIDERS:
                errors.append(f"Invalid primary provider '{primary_provider}'.")
            if enable_fallback and fallback_provider and fallback_provider != "none":
                if fallback_provider not in SUPPORTED_AI_PROVIDERS:
                    errors.append(f"Invalid fallback provider '{fallback_provider}'.")
                if fallback_provider == primary_provider:
                    errors.append("Fallback provider cannot be identical to primary provider.")
            if rate_limit_delay < 0:
                errors.append("Rate limit delay must be a non-negative number.")

            if errors:
                error_str = "Validation failed: " + "; ".join(errors)
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                    return JsonResponse({"success": False, "error": error_str})
                messages.error(request, error_str)
                if campaign:
                    return redirect(f"/?campaign_id={campaign.pk}")
                return redirect("/")

            # --- Persist Settings ---
            if campaign:
                campaign.campaign_objective = campaign_obj_text
                campaign.product_docs = product_docs_text
                campaign.booking_link = booking_link_text
                campaign.save()

                seeds_text = request.POST.get("seeds_text", "").strip()
                if seeds_text:
                    from outreach_manager.linkedin.setup.seeds import create_seed_leads, parse_seed_urls
                    public_ids = parse_seed_urls(seeds_text)
                    if public_ids:
                        created = create_seed_leads(campaign, public_ids)
                        messages.success(request, f"Successfully seeded {created} target profiles.")
                    else:
                        messages.warning(request, "No valid LinkedIn URLs found in seed field.")

            if profile:
                profile.connect_daily_limit = connect_limit
                profile.follow_up_daily_limit = follow_up_limit
                profile.save()

            site_config.ai_model = primary_model
            site_config.llm_api_key = primary_api_key
            site_config.llm_api_base = primary_api_base
            site_config.last_config_save = timezone.now()
            site_config.save()

            env_updates = {
                "EXECUTION_MODE": execution_mode,
                "SESSIONS_PER_DAY": str(sessions_per_day),
                "WORKING_HOURS_START": str(working_start_hour),
                "WORKING_HOURS_END": str(working_end_hour),
                "ACTIVE_DAYS": ",".join(active_days),
                "BROWSER_VISIBILITY": browser_visibility,
                "ENABLED_WORKFLOWS": ",".join(enabled_workflows),
                "PRIMARY_AI_PROVIDER": primary_provider,
                "AI_MODEL": primary_model,
                "LLM_API_KEY": primary_api_key,
                "LLM_API_BASE": primary_api_base,
                "FALLBACK_AI_PROVIDER": fallback_provider if (enable_fallback and fallback_provider != "none") else "",
                "BACKUP_AI_MODEL": fallback_model,
                "BACKUP_LLM_API_KEY": fallback_api_key,
                "BACKUP_LLM_API_BASE": fallback_api_base,
                "LLM_RATE_LIMIT_DELAY": str(rate_limit_delay),
                "BACKUP_STRUCTURED_OUTPUT_COMPATIBLE": str(structured_output),
                "BACKUP_MODEL_STRUCTURED_OUTPUT_COMPATIBLE": str(structured_output),
                "SESSION_HISTORY_ENABLED": str(session_history_enabled),
                "AI_USAGE_TRACKING_ENABLED": str(ai_usage_tracking_enabled),
                "NOTIFICATIONS_ENABLED": str(notifications_enabled),
                "NOTIFY_ON_SUCCESS": str(notify_on_success),
                "NOTIFY_ON_WARNING": str(notify_on_warning),
                "NOTIFY_ON_FAILURE": str(notify_on_failure),
                "NOTIFY_ON_INFO": str(notify_on_info),
                "NOTIFICATION_DELIVERY_MODE": notification_delivery_mode,
                "DEFAULT_CONNECT_DAILY_LIMIT": str(connect_limit),
                "DEFAULT_REPLY_DAILY_LIMIT": str(reply_limit),
                "DEFAULT_FOLLOW_UP_DAILY_LIMIT": str(follow_up_limit),
                "DEFAULT_FIRST_MESSAGE_DAILY_LIMIT": str(first_message_limit),
                "DEFAULT_CHECK_PENDING_DAILY_LIMIT": str(check_pending_limit),
                "DEFAULT_EXTRACT_LEADS_DAILY_LIMIT": str(extract_leads_limit),
                "DEFAULT_EMAIL_DAILY_LIMIT": str(email_limit),
            }
            # --- Persist via ConfigurationService (validate → atomic write → backup → reload) ---
            from outreach_manager.core.config import ConfigurationError
            try:
                _cfg_svc.save(env_updates)
            except ConfigurationError as cfg_err:
                err_msg = f"Validation failed: {cfg_err}"
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                    return JsonResponse({"success": False, "error": err_msg})
                messages.error(request, err_msg)
                if campaign:
                    return redirect(f"/?campaign_id={campaign.pk}")
                return redirect("/")

            if execution_mode == "manual":
                from outreach_manager.core.windows_scheduler import remove_windows_scheduled_task
                remove_windows_scheduled_task()
            elif execution_mode == "automatic":
                from outreach_manager.core.windows_scheduler import update_windows_scheduled_task
                try:
                    update_windows_scheduled_task(timezone.now())
                except Exception as e:
                    logger.warning("Could not register Windows task: %s", e)

            if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                return JsonResponse({"success": True, "message": "Configuration saved successfully.", "last_saved": "Just now"})

            messages.success(request, "Configuration saved successfully.")
            if campaign:
                return redirect(f"/?campaign_id={campaign.pk}")
            return redirect("/")

        if action == "set_override":
            task = request.POST.get("task", "").strip()
            valid_tasks = ["reply_unread", "follow_up", "first_message", "check_pending", "connect", "extract_leads", "extract"]
            if task in valid_tasks:
                site_config.simulated_task = task
                site_config.override_expires_at = timezone.now() + timezone.timedelta(minutes=30)
                site_config.save()
                messages.success(request, f"Task phase override set to '{task}' (bypassing cycle sequence for 30 minutes).")
            else:
                site_config.simulated_task = ""
                site_config.override_expires_at = None
                site_config.save()
                messages.success(request, "Task phase override cleared. Reverted to probabilistic sequence cycle.")

            if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                return JsonResponse({
                    "success": True,
                    "simulated_task": site_config.simulated_task,
                    "seconds_left": 1800 if site_config.simulated_task else 0,
                })
            return redirect("/")

        if action == "launch_chrome":
            from outreach_manager.linkedin.browser.stealth_profile import get_chrome_launch_args
            
            def find_chrome_path():
                import os
                paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
                    "chrome.exe"
                ]
                for p in paths:
                    if os.path.exists(p):
                        return p
                return "chrome.exe"

            chrome_path = find_chrome_path()
            profile_dir = os.path.join(settings.BASE_DIR, "data", "chrome_profile")
            os.makedirs(profile_dir, exist_ok=True)

            # Purge Chrome cache before launching to avoid login collision & bloat
            from outreach_manager.linkedin.browser.launch import purge_chrome_cache
            try:
                purge_chrome_cache()
            except Exception as ex:
                logger.warning("Cache purge failed: %s", ex)

            import urllib.parse
            env_config = read_env_file()
            cdp_url = env_config.get("CDP_URL", "http://127.0.0.1:9228")
            try:
                parsed = urllib.parse.urlparse(cdp_url)
                debug_port = parsed.port or 9222
            except Exception:
                debug_port = 9222

            args = get_chrome_launch_args(debug_port=debug_port, profile_dir=profile_dir)
            cmd = [chrome_path] + args
            try:
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(cmd, creationflags=DETACHED_PROCESS, close_fds=True)
                messages.success(request, "Chrome session launched on port 9222 with stealth profile.")
            except Exception as e:
                messages.error(request, f"Failed to launch Chrome: {str(e)}")

        elif action == "run_outreach":
            pid_file = os.path.join(settings.BASE_DIR, "data", "daemon.pid")
            is_running = False
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r") as f:
                        pid = int(f.read().strip())
                    is_running = is_pid_running(pid)
                except Exception:
                    pass

            if is_running:
                messages.warning(request, "Outreach cycle is already running.")
            else:
                log_path = os.path.join(settings.BASE_DIR, "data", "outreach.log")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                
                with open(log_path, "a", encoding="utf-8") as log_file:
                    log_file.write(f"\n--- MANUAL OUTREACH CYCLE STARTED AT {timezone.now().isoformat()} ---\n")
                    log_file.flush()
                
                log_file = open(log_path, "a", encoding="utf-8")
                
                cmd = [sys.executable, "manage.py", "rundaemon", "--exit-on-empty"]
                if campaign:
                    cmd.extend(["--campaign-id", str(campaign.pk)])
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=settings.BASE_DIR,
                )
                with open(pid_file, "w") as f:
                    f.write(str(proc.pid))
                
                messages.success(request, "Outreach cycle triggered in background.")

        elif action == "terminate_cycle":
            pid_file = os.path.join(settings.BASE_DIR, "data", "daemon.pid")
            terminated = False
            error_msg = ""
            if os.path.exists(pid_file):
                try:
                    with open(pid_file, "r") as f:
                        pid = int(f.read().strip())
                    if is_pid_running(pid):
                        # Windows: taskkill /F /T kills the process tree
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            capture_output=True,
                        )
                        terminated = True
                    # Remove the PID file regardless
                    try:
                        os.remove(pid_file)
                    except OSError:
                        pass
                except Exception as exc:
                    error_msg = str(exc)

            # Support both AJAX (fetch) and form POST
            if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.content_type == "application/json":
                return JsonResponse({"terminated": terminated, "error": error_msg})

            if terminated:
                messages.success(request, "Outreach cycle terminated successfully.")
            elif error_msg:
                messages.error(request, f"Failed to terminate: {error_msg}")
            else:
                messages.warning(request, "No active outreach cycle found.")



        elif action == "clear_logs":
            log_path = os.path.join(settings.BASE_DIR, "data", "outreach.log")
            try:
                if os.path.exists(log_path):
                    with open(log_path, "w", encoding="utf-8") as f:
                        f.write("")
                messages.success(request, "Execution logs cleared successfully.")
            except Exception as e:
                messages.error(request, f"Failed to clear logs: {str(e)}")
            return redirect("/")

        elif action == "download_logs":
            log_path = os.path.join(settings.BASE_DIR, "data", "outreach.log")
            if os.path.exists(log_path):
                from django.http import FileResponse
                response = FileResponse(open(log_path, 'rb'), content_type='text/plain')
                response['Content-Disposition'] = 'attachment; filename="outreach.log"'
                return response
            else:
                messages.error(request, "Log file not found.")
                return redirect("/")

        elif action == "edit_deal":
            deal_id = request.POST.get("deal_id")
            deal = Deal.objects.filter(pk=deal_id).first()
            if deal:
                state = request.POST.get("state")
                outcome = request.POST.get("outcome", "").strip()
                connect_attempts = request.POST.get("connect_attempts")
                reason = request.POST.get("reason", "").strip()
                mailbox_id = request.POST.get("mailbox_id")

                if state:
                    deal.state = state
                deal.outcome = outcome
                if connect_attempts is not None:
                    try:
                        deal.connect_attempts = int(connect_attempts)
                    except ValueError:
                        pass
                deal.reason = reason
                
                if mailbox_id:
                    from outreach_manager.emails.models import Mailbox
                    deal.mailbox = Mailbox.objects.filter(pk=mailbox_id).first()
                else:
                    deal.mailbox = None
                    
                deal.save()
                messages.success(request, f"Prospect {deal.lead.public_identifier} updated successfully.")
            else:
                messages.error(request, "Prospect not found.")
            if campaign:
                return redirect(f"/?campaign_id={campaign.pk}")
            return redirect("/")

        elif action == "add_seeds":
            urls_text = request.POST.get("seeds_text", "").strip()
            if urls_text and campaign:
                from outreach_manager.linkedin.setup.seeds import create_seed_leads, parse_seed_urls
                public_ids = parse_seed_urls(urls_text)
                if public_ids:
                    created = create_seed_leads(campaign, public_ids)
                    messages.success(request, f"Successfully added {created} seed profiles as QUALIFIED leads.")
                else:
                    messages.error(request, "No valid LinkedIn URLs found.")
            else:
                messages.error(request, "Please enter some LinkedIn URLs.")

        elif action == "update_campaign":
            if campaign:
                campaign.campaign_objective = request.POST.get("campaign_objective", "").strip()
                campaign.product_docs = request.POST.get("product_docs", "").strip()
                campaign.booking_link = request.POST.get("booking_link", "").strip()
                campaign.save()
                messages.success(request, "Campaign settings updated successfully.")
            else:
                messages.error(request, "No campaign found to update.")

        elif action == "update_linkedin":
            if profile:
                profile.connect_daily_limit = int(request.POST.get("connect_daily_limit", 20))
                profile.follow_up_daily_limit = int(request.POST.get("follow_up_daily_limit", 25))
                profile.save()
                messages.success(request, "LinkedIn limits updated successfully.")
            else:
                messages.error(request, "No LinkedIn profile found.")

        elif action == "update_ai":
            primary_model = request.POST.get("ai_model", "").strip()
            primary_api_key = request.POST.get("llm_api_key", "").strip()
            primary_api_base = request.POST.get("llm_api_base", "").strip()

            from outreach_manager.core.config import _infer_provider
            primary_provider = _infer_provider(primary_model, "google")

            env_updates = {
                "PRIMARY_AI_PROVIDER": primary_provider,
                "AI_MODEL": primary_model,
                "LLM_API_KEY": primary_api_key,
                "LLM_API_BASE": primary_api_base,
                "BACKUP_LLM_API_KEY": request.POST.get("backup_llm_api_key", "").strip(),
                "BACKUP_AI_MODEL": request.POST.get("backup_ai_model", "").strip(),
                "BACKUP_LLM_API_BASE": request.POST.get("backup_llm_api_base", "").strip(),
                "LLM_RATE_LIMIT_DELAY": request.POST.get("llm_rate_limit_delay", "3.0").strip(),
            }
            site_config.ai_model = primary_model
            site_config.llm_api_key = primary_api_key
            site_config.llm_api_base = primary_api_base
            site_config.last_config_save = timezone.now()
            site_config.save()

            _cfg_svc.save(env_updates)
            messages.success(request, "AI and LLM configurations updated successfully.")

        if campaign:
            return redirect(f"/?campaign_id={campaign.pk}")
        return redirect("/")


class SessionHistoryView(View):
    """Dedicated view for browsing, inspecting, and filtering completed outreach session history."""

    def get(self, request):
        import datetime
        from django.db.models import Avg, Q
        from outreach_manager.core.models import SessionHistory, Campaign
        from outreach_manager.core.config import get_config

        qs = SessionHistory.objects.all().prefetch_related("ai_usage").order_by("-start_time")

        # --- Filters ---
        status_filter = request.GET.get("status", "").strip().lower()
        if status_filter:
            if status_filter == "completed":
                qs = qs.filter(status__iexact="Completed")
            elif status_filter in ("issues", "completed_with_issues"):
                qs = qs.filter(status__icontains="Completed with Errors")
            elif status_filter == "failed":
                qs = qs.filter(Q(status__iexact="Failed") | Q(fatal_errors__gt=0))
            elif status_filter in ("no_work", "skipped"):
                qs = qs.filter(Q(status__icontains="Skipped") | Q(actions_completed=0))

        mode_filter = request.GET.get("execution_mode", "").strip().lower()
        if mode_filter in ("manual", "automatic"):
            qs = qs.filter(execution_mode__iexact=mode_filter)

        provider_filter = request.GET.get("provider", "").strip().lower()
        if provider_filter:
            qs = qs.filter(ai_usage__primary_provider__iexact=provider_filter)

        search_query = request.GET.get("q", "").strip()
        if search_query:
            qs = qs.filter(session_id__icontains=search_query)

        start_date_str = request.GET.get("start_date", "").strip()
        if start_date_str:
            try:
                s_date = datetime.datetime.strptime(start_date_str, "%Y-%m-%d").date()
                qs = qs.filter(start_time__date__gte=s_date)
            except ValueError:
                pass

        end_date_str = request.GET.get("end_date", "").strip()
        if end_date_str:
            try:
                e_date = datetime.datetime.strptime(end_date_str, "%Y-%m-%d").date()
                qs = qs.filter(start_time__date__lte=e_date)
            except ValueError:
                pass

        # --- Summary Cards (4 Cards max for recent stats) ---
        total_sessions = qs.count()
        if total_sessions > 0:
            successful_count = qs.filter(Q(status__iexact="Completed") | Q(total_errors=0)).count()
            success_rate = round((successful_count / total_sessions) * 100)
            avg_duration_sec = qs.aggregate(Avg("duration_seconds"))["duration_seconds__avg"] or 0.0
            avg_actions = round(qs.aggregate(Avg("actions_completed"))["actions_completed__avg"] or 0.0, 1)
        else:
            success_rate = 100
            avg_duration_sec = 0.0
            avg_actions = 0.0

        if avg_duration_sec >= 60:
            avg_duration_str = f"{int(avg_duration_sec // 60)} min"
        else:
            avg_duration_str = f"{int(avg_duration_sec)}s"

        # --- Pagination (20 per page) ---
        paginator = Paginator(qs, 20)
        page_number = request.GET.get("page", 1)
        sessions_page = paginator.get_page(page_number)

        sessions_data = []
        for s in sessions_page:
            ai_log = s.ai_usage.first()
            p_name = ai_log.primary_provider if ai_log and ai_log.primary_provider else "google"

            dur_sec = int(s.duration_seconds or 0)
            if dur_sec >= 60:
                dur_str = f"{dur_sec // 60}m {dur_sec % 60}s"
            else:
                dur_str = f"{dur_sec}s"

            st_lower = s.status.lower()
            if "completed with" in st_lower or s.total_errors > 0:
                badge_type = "warning"
                badge_text = "🟡 " + s.status
            elif st_lower == "failed" or s.fatal_errors > 0:
                badge_type = "danger"
                badge_text = "🔴 " + s.status
            elif "skipped" in st_lower or s.actions_completed == 0:
                badge_type = "secondary"
                badge_text = "⚪ " + s.status
            else:
                badge_type = "success"
                badge_text = "🟢 Completed"

            sessions_data.append({
                "object": s,
                "session_id": s.session_id,
                "start_time": s.start_time,
                "finish_time": s.finish_time,
                "duration_str": dur_str,
                "execution_mode": s.execution_mode.capitalize(),
                "actions_completed": s.actions_completed,
                "primary_provider": p_name.capitalize(),
                "total_errors": s.total_errors,
                "status": s.status,
                "badge_type": badge_type,
                "badge_text": badge_text,
            })

        pid_file = os.path.join(settings.BASE_DIR, "data", "daemon.pid")
        is_running = False
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    pid = int(f.read().strip())
                is_running = is_pid_running(pid)
            except Exception:
                pass

        all_campaigns = Campaign.objects.all()
        campaign = Campaign.objects.first()

        context = {
            "sessions_page": sessions_page,
            "sessions_data": sessions_data,
            "total_sessions": total_sessions,
            "success_rate": success_rate,
            "avg_duration_str": avg_duration_str,
            "avg_actions": avg_actions,
            "status_filter": status_filter,
            "mode_filter": mode_filter,
            "provider_filter": provider_filter,
            "start_date": start_date_str,
            "end_date": end_date_str,
            "search_query": search_query,
            "all_campaigns": all_campaigns,
            "campaign": campaign,
            "is_running": is_running,
            "config": get_config(),
            "active_tab": "session_history",
        }
        return render(request, "core/session_history.html", context)


class SessionDetailView(View):
    """Detailed view and JSON endpoint for inspecting a single outreach session."""

    def get(self, request, session_id):
        from outreach_manager.core.models import SessionHistory, AIUsageLog, ProviderHealth
        from outreach_manager.crm.models.event_log import EventLog

        session = SessionHistory.objects.filter(session_id=session_id).prefetch_related("ai_usage").first()
        if not session:
            return JsonResponse({"success": False, "error": "Session record not found."}, status=404)

        ai_log = session.ai_usage.first()
        primary_provider = (ai_log.primary_provider if ai_log and ai_log.primary_provider else "google").lower()
        fallback_provider = ai_log.fallback_provider if ai_log and ai_log.fallback_provider else "None"
        ai_calls = (ai_log.primary_calls + ai_log.fallback_calls) if ai_log else 0
        fallback_used = (ai_log.fallback_calls > 0) if ai_log else False
        provider_failures = ai_log.failed_calls if ai_log else 0

        # Provider Health status
        ph = ProviderHealth.objects.filter(provider_name__iexact=primary_provider).first()
        if ph and ph.total_calls > 0:
            rate = ph.success_rate
            if rate >= 90.0:
                ph_status = "🟢 Healthy"
                ph_type = "success"
            elif rate >= 50.0:
                ph_status = "🟡 Intermittent Failures"
                ph_type = "warning"
            else:
                ph_status = "🔴 Failing"
                ph_type = "danger"
        else:
            ph_status = "🟢 Healthy"
            ph_type = "success"

        # Work Completed metrics
        start_t = session.start_time
        finish_t = session.finish_time or timezone.now()

        event_logs = list(EventLog.objects.filter(created_at__range=(start_t, finish_t)))

        connect_sent = sum(1 for e in event_logs if e.event_type == EventLog.EventType.CONNECT_REQUESTED)
        connect_accepted = sum(1 for e in event_logs if e.event_type == EventLog.EventType.CONNECT_ACCEPTED)
        first_messages = sum(1 for e in event_logs if e.event_type == EventLog.EventType.MESSAGE_SENT and "first" in e.detail.lower())
        replies_sent = sum(1 for e in event_logs if e.event_type == EventLog.EventType.MESSAGE_SENT and "reply" in e.detail.lower())
        follow_ups = sum(1 for e in event_logs if e.event_type == EventLog.EventType.MESSAGE_SENT and "follow" in e.detail.lower())
        emails_sent = sum(1 for e in event_logs if e.event_type == EventLog.EventType.EMAIL_SENT)
        pending_checked = sum(1 for e in event_logs if "check_pending" in e.detail.lower())
        withdrawn_requests = sum(1 for e in event_logs if "withdraw" in e.detail.lower())
        leads_extracted = sum(1 for e in event_logs if "extract" in e.detail.lower())

        work_completed = []
        if connect_sent > 0:
            work_completed.append({"label": "Connection Requests Sent", "value": connect_sent})
        if first_messages > 0:
            work_completed.append({"label": "First Messages Sent", "value": first_messages})
        if replies_sent > 0:
            work_completed.append({"label": "Replies Sent", "value": replies_sent})
        if follow_ups > 0:
            work_completed.append({"label": "Follow-Ups Sent", "value": follow_ups})
        if pending_checked > 0 or "check_pending" in session.workflows_executed:
            work_completed.append({"label": "Pending Requests Checked", "value": max(pending_checked, 1)})
        if connect_accepted > 0:
            work_completed.append({"label": "Accepted Connections", "value": connect_accepted})
        if withdrawn_requests > 0:
            work_completed.append({"label": "Withdrawn Requests", "value": withdrawn_requests})
        if leads_extracted > 0:
            work_completed.append({"label": "Leads Extracted", "value": leads_extracted})
        if emails_sent > 0:
            work_completed.append({"label": "Emails Sent", "value": emails_sent})

        if not work_completed and session.actions_completed > 0:
            work_completed.append({"label": "Total Actions Completed", "value": session.actions_completed})

        log_snippet = self._get_session_log_snippet(start_t, finish_t, session.session_id)

        error_list = []
        if session.deal_errors > 0:
            error_list.append(f"{session.deal_errors} deal processing error(s) occurred.")
        if session.workflow_errors > 0:
            error_list.append(f"{session.workflow_errors} workflow error(s) encountered.")
        if session.fatal_errors > 0:
            error_list.append(f"{session.fatal_errors} fatal execution error(s).")
        if session.browser_recoveries > 0:
            error_list.append(f"Browser recovery was triggered {session.browser_recoveries} time(s).")
        if session.llm_deferrals > 0:
            error_list.append(f"AI provider deferrals occurred {session.llm_deferrals} time(s).")

        dur_sec = int(session.duration_seconds or 0)
        dur_str = f"{dur_sec // 60}m {dur_sec % 60}s" if dur_sec >= 60 else f"{dur_sec}s"

        detail_data = {
            "session_id": session.session_id,
            "status": session.status,
            "execution_mode": session.execution_mode.capitalize(),
            "start_time": session.start_time.strftime("%Y-%m-%d %H:%M:%S") if session.start_time else "N/A",
            "finish_time": session.finish_time.strftime("%Y-%m-%d %H:%M:%S") if session.finish_time else "N/A",
            "duration_str": dur_str,
            "work_completed": work_completed,
            "workflows_executed": session.workflows_executed,
            "workflows_disabled": session.workflows_disabled,
            "workflows_skipped": session.workflows_skipped,
            "primary_provider": primary_provider.capitalize(),
            "fallback_provider": fallback_provider.capitalize(),
            "ai_calls": ai_calls,
            "fallback_used": fallback_used,
            "provider_failures": provider_failures,
            "provider_health_status": ph_status,
            "provider_health_type": ph_type,
            "deal_errors": session.deal_errors,
            "workflow_errors": session.workflow_errors,
            "fatal_errors": session.fatal_errors,
            "browser_recoveries": session.browser_recoveries,
            "llm_deferrals": session.llm_deferrals,
            "total_errors": session.total_errors,
            "error_list": error_list,
            "log_snippet": log_snippet,
        }

        if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
            return JsonResponse({"success": True, "detail": detail_data})

        return JsonResponse({"success": True, "detail": detail_data})

    def _get_session_log_snippet(self, start_time, finish_time, session_id):
        log_path = os.path.join(settings.BASE_DIR, "data", "outreach.log")
        if not os.path.exists(log_path):
            return "No execution log file found."
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            recent = lines[-300:] if len(lines) > 300 else lines
            return "".join(recent) if recent else "Log is empty."
        except Exception as exc:
            return f"Error reading log file: {str(exc)}"
