$ErrorActionPreference='Stop'
Set-Location 'D:\WeChatMaa'
& '.\.venv\Scripts\python.exe' -m unittest test_store test_layout test_list_rows test_flows test_contact_alignment test_ocr_context test_admin_export test_admin_agent test_activity_scheduler test_message_vision test_send test_reply_client test_watch test_capture_guard test_selected_row test_manual_gui test_contact_identity test_private_baseline test_inbound_work test_creation_client test_context_recovery test_group_policy_admin test_send_retry_policy -v
if ($LASTEXITCODE -ne 0) { throw 'MAA_TESTS_FAILED' }
