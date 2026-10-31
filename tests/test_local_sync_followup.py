"""覆盖云端成功后接续同步的真实状态与并发边界，不访问外部数据。"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

import importlib.util
module_path=Path(__file__).resolve().parents[1]/'backend/local_sync_followup.py'
spec=importlib.util.spec_from_file_location('candidate_local_sync_followup',module_path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LocalSyncFollowup=module.LocalSyncFollowup


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'pending.json'
        self.tasks = [dict(task_id='fund',enabled=True,auto_run_enabled=True,remote_root='/基金_分笔成交')]
        self.calls = []
        self.clock = [1000.0]
        self.runner = lambda task_id: self.record(task_id)
        self.queue = self.make()

    def make(self):
        return LocalSyncFollowup(self.path,load_tasks=lambda:copy.deepcopy(self.tasks),
            run_task=lambda task_id:self.runner(task_id),now=lambda:self.clock[0])

    def record(self,task_id):
        self.calls.append(task_id)
        return True,'ok',{'status':'completed','running':False}

    def event(self,stamp=10,status='success',root='/apps/bypy/基金_分笔成交'):
        return dict(task_uid='cloud-fund',last_execute_time=stamp,status=status,save_dir=root)

    def test_numeric_event_timestamp_representation_does_not_repeat_download(self):
        self.queue.request(self.event(stamp=10));self.queue.run_pending()
        self.assertEqual(self.queue.request(self.event(stamp=10.0)),[])
        self.queue.run_pending();self.assertEqual(self.calls,['fund'])

    def test_state_io_blocking_error_is_not_misreported_as_another_running_sync(self):
        from unittest.mock import patch
        self.queue.request(self.event())
        with patch.object(self.queue,'_save',side_effect=BlockingIOError('state unavailable')):
            with self.assertRaises(BlockingIOError):self.queue.run_pending()
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['completed'],0)

    def test_cloud_completion_after_cron_is_followed_without_waiting_next_hour(self):
        self.queue.run_pending()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.queue.request(self.event()),['fund'])
        self.queue.run_pending()
        self.assertEqual(self.calls,['fund'])
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['status'],'done')

    def test_same_event_is_idempotent_across_restart(self):
        self.queue.request(self.event());self.queue.run_pending()
        other = self.make()
        self.assertEqual(other.request(self.event()),[])
        other.run_pending()
        self.assertEqual(self.calls,['fund'])

    def test_completed_events_reconciled_after_restart(self):
        self.make().run_pending([self.event()])
        self.assertEqual(self.calls,['fund'])

    def test_pending_intent_survives_restart_and_midnight(self):
        self.queue.request(self.event());self.clock[0]+=36000
        self.make().run_pending()
        self.assertEqual(self.calls,['fund'])

    def test_failed_running_cancelled_or_unknown_upstream_not_followed(self):
        for status in ['failed','running','cancelled',None]:
            self.assertEqual(self.queue.request(self.event(status=status)),[])
        self.queue.run_pending();self.assertEqual(self.calls,[])

    def test_skipped_success_can_recover_previous_local_failure(self):
        self.queue.request(self.event(status='skipped'));self.queue.run_pending()
        self.assertEqual(self.calls,['fund'])

    def test_invalid_empty_future_or_nonfinite_event_is_ignored(self):
        for event in [{},None,self.event(stamp=None),self.event(stamp=float('nan')),self.event(stamp=2000),self.event(root=''),self.event(root='/apps/bypy/../基金_分笔成交')]:
            self.assertEqual(self.queue.request(event),[])
        self.assertFalse(self.path.exists())

    def test_disabled_or_manual_only_local_tasks_do_not_start(self):
        for key in ['enabled','auto_run_enabled']:
            self.tasks[0][key]=False
            self.assertEqual(self.queue.request(self.event()),[])
            self.tasks[0][key]=True
        self.assertEqual(self.calls,[])

    def test_match_is_exact_not_root_prefix(self):
        self.assertEqual(self.queue.request(self.event(root='/apps/bypy')),[])
        self.assertEqual(self.queue.request(self.event(root='/apps/bypy/基金_分笔成交-其他')),[])
        self.assertEqual(self.queue.request(self.event(root='/apps/bypy//基金_分笔成交/')),['fund'])

    def test_new_event_during_download_is_not_lost(self):
        def runner(task_id):
            self.queue.request(self.event(stamp=11))
            return self.record(task_id)
        self.runner=runner;self.queue.request(self.event());self.queue.run_pending()
        item=self.queue.snapshot()['tasks']['fund']
        self.assertGreater(item['requested'],item['completed'])
        self.runner=self.record;self.queue.run_pending()
        self.assertEqual(self.calls,['fund','fund'])

    def test_many_requests_before_start_coalesce(self):
        for stamp in range(1,21):self.queue.request(self.event(stamp=stamp))
        self.queue.run_pending();self.assertEqual(self.calls,['fund'])

    def test_old_duplicate_after_new_event_does_not_run_again(self):
        self.queue.request(self.event(10));self.queue.request(self.event(11));self.queue.run_pending()
        self.assertEqual(self.queue.request(self.event(10)),[])
        self.queue.run_pending();self.assertEqual(self.calls,['fund'])

    def test_busy_sync_is_retained_for_retry(self):
        self.runner=lambda task_id:(False,'busy',{'running':True})
        self.queue.request(self.event());self.queue.run_pending()
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['status'],'retry_pending')
        self.runner=self.record;self.queue.run_pending();self.assertEqual(self.calls,[])
        self.clock[0]+=61;self.queue.run_pending();self.assertEqual(self.calls,['fund'])

    def test_network_error_does_not_mark_complete_or_leak_secret(self):
        def fail(task_id):raise RuntimeError('secret-network-token')
        self.runner=fail;self.queue.request(self.event());self.queue.run_pending()
        data=self.path.read_text()
        self.assertNotIn('secret-network-token',data)
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['completed'],0)
        self.clock[0]+=61;self.runner=self.record;self.queue.run_pending()
        self.assertEqual(self.calls,['fund'])

    def test_invalid_runner_result_not_counted_as_success(self):
        self.runner=lambda task_id:True
        self.queue.request(self.event());self.queue.run_pending()
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['completed'],0)

    def test_disabled_or_remapped_pending_task_is_cancelled(self):
        self.queue.request(self.event());self.tasks[0]['remote_root']='/another'
        self.queue.run_pending();self.assertEqual(self.calls,[])
        self.assertEqual(self.queue.snapshot()['tasks']['fund']['status'],'cancelled')

    def test_corrupt_state_is_preserved_and_rejected(self):
        self.path.write_text('{bad')
        with self.assertRaises(ValueError):self.queue.run_pending()
        self.assertEqual(self.path.read_text(),'{bad')

    def test_symlink_state_is_not_followed(self):
        other=Path(self.temp.name)/'other.json';other.write_text('{}');self.path.symlink_to(other)
        with self.assertRaises(ValueError):self.queue.request(self.event())
        self.assertEqual(other.read_text(),'{}')

    def test_two_instances_cannot_download_the_same_pending_work(self):
        entered=threading.Event();release=threading.Event()
        def runner(task_id):
            entered.set();self.assertTrue(release.wait(3));return self.record(task_id)
        self.runner=runner;self.queue.request(self.event())
        first=threading.Thread(target=self.queue.run_pending);first.start()
        self.assertTrue(entered.wait(3))
        self.make().run_pending();release.set();first.join(3)
        self.assertFalse(first.is_alive());self.assertEqual(self.calls,['fund'])

    def test_no_cloud_secrets_are_persisted(self):
        event=self.event();event.update(url='private-share-url',pwd='private-password')
        self.queue.request(event)
        text=self.path.read_text();self.assertNotIn('private-share-url',text);self.assertNotIn('private-password',text)


if __name__=='__main__':unittest.main()
