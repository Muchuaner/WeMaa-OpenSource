import unittest
from datetime import datetime,timezone
from watch_policy import quiet_scan_allowed,NavigationBackoff
from activity_scheduler import ActivityScheduler

class PolicyTests(unittest.TestCase):
    def test_china_window_independent_of_host_timezone(self):
        for iso,allowed in [('2026-09-30T16:59:59',False),('2026-09-30T17:00:00',True),
                            ('2026-09-30T20:59:59',True),('2026-09-30T21:00:00',False)]:
            self.assertEqual(quiet_scan_allowed(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()),allowed)
    def test_daytime_budget_retains_unread_but_excludes_due_history(self):
        scheduler=ActivityScheduler(clock=lambda:100)
        rows=[{'key':'quiet','unread':False}]
        self.assertEqual(scheduler.plan(rows,active_budget=0),[])
        rows.append({'key':'red','unread':True})
        self.assertEqual([r['key'] for r in scheduler.plan(rows,active_budget=0)],['red'])
    def test_ocr_name_variants_share_failure_cooldown_but_unread_preempts(self):
        b=NavigationBackoff();a={'key':'a','avatar_sha':'same'};variant={'key':'b','avatar_sha':'same'}
        b.failed(a,100);self.assertFalse(b.eligible(variant,129));self.assertTrue(b.eligible(variant,130))
        b.failed(variant,130);self.assertFalse(b.eligible(a,189))
        self.assertTrue(b.eligible(dict(a,unread=True),131))
        b.succeeded(a);self.assertTrue(b.eligible(variant,131))

    def test_persistent_pause_and_manual_enable(self):
        import tempfile
        from pathlib import Path
        from watch_policy import polling_enabled,set_polling
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(polling_enabled(folder))
            set_polling(folder,False);self.assertFalse(polling_enabled(folder))
            self.assertFalse(polling_enabled(Path(folder)))
            (Path(folder)/'data/polling-policy.json').write_text('invalid')
            self.assertFalse(polling_enabled(folder))
            set_polling(folder,True);self.assertTrue(polling_enabled(folder))
