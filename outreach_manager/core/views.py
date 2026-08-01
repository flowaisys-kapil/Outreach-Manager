# outreach_manager/core/views.py
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
    env_path = os.path.join(settings.BASE_DIR, ".env")
    data = {
        "BACKUP_LLM_API_KEY": "",
        "BACKUP_AI_MODEL": "",
        "BACKUP_LLM_API_BASE": "",
        "LLM_RATE_LIMIT_DELAY": "5.0",
        "USE_CDP": "True",
        "CDP_URL": "http://127.0.0.1:9222",
    }
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        data[k.strip()] = v.strip()
        except Exception:
            pass
    return data

def write_env_file(updates):
    env_path = os.path.join(settings.BASE_DIR, ".env")
    lines = []
    existing = set()
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k, v = stripped.split("=", 1)
                        k = k.strip()
                        if k in updates:
                            lines.append(f"{k}={updates[k]}\n")
                            existing.add(k)
                        else:
                            lines.append(line)
                    else:
                        lines.append(line)
        except Exception:
            pass
    for k, v in updates.items():
        if k not in existing:
            lines.append(f"{k}={v}\n")
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.error("Failed to write to .env file: %s", e)

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
        campaign_id = request.POST.get("campaign_id") or request.GET.get("campaign_id")
        if campaign_id:
            campaign = Campaign.objects.filter(pk=campaign_id).first()
        else:
            campaign = Campaign.objects.first()

        profile = LinkedInProfile.objects.first()
        site_config = SiteConfig.load()

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
            # Save SiteConfig
            site_config.ai_model = request.POST.get("ai_model", "").strip()
            site_config.llm_api_key = request.POST.get("llm_api_key", "").strip()
            site_config.llm_api_base = request.POST.get("llm_api_base", "").strip()
            site_config.save()
            
            # Save .env
            env_updates = {
                "BACKUP_LLM_API_KEY": request.POST.get("backup_llm_api_key", "").strip(),
                "BACKUP_AI_MODEL": request.POST.get("backup_ai_model", "").strip(),
                "BACKUP_LLM_API_BASE": request.POST.get("backup_llm_api_base", "").strip(),
                "LLM_RATE_LIMIT_DELAY": request.POST.get("llm_rate_limit_delay", "5.0").strip(),
            }
            write_env_file(env_updates)
            messages.success(request, "AI and LLM configurations updated successfully.")

        if campaign:
            return redirect(f"/?campaign_id={campaign.pk}")
        return redirect("/")
