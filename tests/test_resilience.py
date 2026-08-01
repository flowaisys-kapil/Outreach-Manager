import pytest
from unittest.mock import patch, MagicMock
from django.utils import timezone
from outreach_manager.crm.models.lead import Lead
from outreach_manager.crm.models.deal import Deal, DealState
from outreach_manager.core.models import Task
from outreach_manager.core.daemon import run_daemon
from outreach_manager.linkedin.tasks.reply import handle_reply_unread
from tests.conftest import FakeAccountSession

from outreach_manager.linkedin.db.leads import create_enriched_lead, promote_lead_to_deal
from outreach_manager.core.db.deals import set_profile_state

@pytest.mark.django_db
class TestResilienceAndCatchUp:
    def test_reply_unread_syncs_connected_deals(self, fake_session):
        campaign = fake_session.campaign

        url_conn = "https://linkedin.com/in/lead-conn/"
        create_enriched_lead(fake_session, url_conn, {"first_name": "Lead", "last_name": "Conn"})
        promote_lead_to_deal(fake_session, "lead-conn")
        set_profile_state(fake_session, "lead-conn", DealState.CONNECTED.value)

        url_qual = "https://linkedin.com/in/lead-qual/"
        create_enriched_lead(fake_session, url_qual, {"first_name": "Lead", "last_name": "Qual"})
        promote_lead_to_deal(fake_session, "lead-qual")
        set_profile_state(fake_session, "lead-qual", DealState.QUALIFIED.value)

        task = Task.objects.create(
            task_type=Task.TaskType.REPLY_UNREAD,
            payload={"campaign_id": campaign.pk},
            status=Task.Status.RUNNING,
            scheduled_at=timezone.now(),
        )

        # Mock sync_conversation to verify it gets called only on CONNECTED deals
        mock_result = MagicMock()
        mock_result.new_messages = []
        with patch("outreach_manager.linkedin.tasks.reply.sync_conversation", return_value=mock_result) as mock_sync:
            handle_reply_unread(task, fake_session, qualifiers={})
            mock_sync.assert_called_once_with(fake_session, "lead-conn", allow_navigation=False)

    def test_daemon_auto_resume_backoff_network_error(self, fake_session):
        campaign = fake_session.campaign

        call_count = 0

        def mock_handler(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Workflow error test")

        with patch("outreach_manager.core.session_executor.has_due_work", return_value=True), \
             patch("time.sleep"), \
             patch("outreach_manager.core.session_executor._WORKFLOW_HANDLERS", {Task.TaskType.CONNECT: mock_handler}), \
             patch("outreach_manager.core.session_executor.BalancedSequenceGenerator.get_cycle_sequence", return_value=[Task.TaskType.CONNECT]), \
             patch("outreach_manager.core.session_executor.reconcile"):

            summary = run_daemon(fake_session, exit_on_empty=True)

            assert call_count == 1
            assert summary is not None
            assert len(summary.errors) == 1
            assert "connect: Workflow error test" in summary.errors[0]

    def test_purge_chrome_cache(self):
        from outreach_manager.linkedin.browser.launch import purge_chrome_cache
        import tempfile
        import os

        # Verify that it runs without throwing error even if profile_dir doesn't exist
        with patch("django.conf.settings.BASE_DIR", "/nonexistent_directory_xyz"):
            purge_chrome_cache()

        # Verify it deletes the target subdirectories
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_dir = os.path.join(temp_dir, "data", "chrome_profile")
            cache_dir = os.path.join(profile_dir, "Default", "Cache")
            os.makedirs(cache_dir)
            
            # Create a dummy file in Cache
            with open(os.path.join(cache_dir, "dummy_cache_file"), "w") as f:
                f.write("cache_data")
                
            # Create a tmp file in root of profile
            tmp_file = os.path.join(profile_dir, "test.tmp")
            with open(tmp_file, "w") as f:
                f.write("temp_data")

            # Create a persistent Cookies file that must NOT be deleted
            cookies_file = os.path.join(profile_dir, "Default", "Cookies")
            os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
            with open(cookies_file, "w") as f:
                f.write("session_cookies")

            with patch("django.conf.settings.BASE_DIR", temp_dir):
                purge_chrome_cache()

            # The Cache directory should be gone
            assert not os.path.exists(cache_dir)
            # The tmp file should be gone
            assert not os.path.exists(tmp_file)
            # The persistent Cookies file should still exist!
            assert os.path.exists(cookies_file)

    def test_start_browser_session_cdp_auto_launch(self, fake_session):
        from outreach_manager.linkedin.browser.launch import start_browser_session
        import os

        # We will mock USE_CDP to True
        mock_env = {
            "USE_CDP": "True",
            "CDP_URL": "http://127.0.0.1:9222",
        }

        # Mock playwright sync start
        mock_playwright = MagicMock()
        mock_chromium = mock_playwright.chromium

        connect_count = 0
        mock_browser = MagicMock()
        mock_browser.contexts = [MagicMock()]

        def mock_connect_over_cdp(url):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                raise Exception("CDP connection failed - port closed")
            return mock_browser

        mock_chromium.connect_over_cdp = mock_connect_over_cdp
        fake_session.playwright = mock_playwright

        with patch.dict(os.environ, mock_env), \
             patch("playwright.sync_api.sync_playwright") as mock_sync_pw, \
             patch("subprocess.Popen") as mock_popen, \
             patch("time.sleep"), \
             patch("outreach_manager.linkedin.browser.launch.purge_chrome_cache"), \
             patch("outreach_manager.linkedin.browser.launch.apply_full_stealth"), \
             patch("outreach_manager.linkedin.browser.launch.dismiss_comply_gate"):
            
            # Setup sync_playwright to return our mock
            mock_sync_pw_inst = MagicMock()
            mock_sync_pw_inst.start.return_value = mock_playwright
            mock_sync_pw.return_value = mock_sync_pw_inst

            start_browser_session(fake_session)

            assert connect_count == 2
            mock_popen.assert_called_once()
            assert fake_session.browser == mock_browser
