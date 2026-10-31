"""在现有生产 Python 依赖中验收真实模块及 APScheduler，禁止真实网络请求。"""
import importlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class RealRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web=importlib.import_module('backend.web_app')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.done=threading.Event()
        self.local=[dict(task_id='qa_fund',remote_root='/qa_fund',enabled=True,
            auto_run_enabled=True,cron='0 17-23 * * 1-6')]
        self.cloud=dict(task_uid='qa_cloud',save_dir='/apps/bypy/qa_fund',
            status='success',last_execute_time=int(time.time()))
        self.fake_storage=Mock()
        self.fake_storage.config={'baidu':{'tasks':[]},'cron':{'default_schedule':[]}}
        self.fake_storage.list_tasks.return_value=[self.cloud]
        self.fake_storage.reset_running_tasks.return_value=[]
        self.fake_storage.is_valid.return_value=True
        def run(**kwargs):
            self.done.set()
            return True,'completed',{'status':'success','running':False}
        self.runner=Mock(side_effect=run)
        for name,value in [('storage',self.fake_storage),
                ('LOCAL_SYNC_FOLLOWUP_STATE_FILE',str(Path(self.temp.name)/'followup.json')),
                ('_load_bypy_sync_tasks',lambda **kwargs:self.local),
                ('_run_local_sync_task',self.runner)]:
            manager=patch.object(self.web,name,value);manager.start();self.addCleanup(manager.stop)

    def wait_done(self,scheduler):
        self.assertTrue(self.done.wait(3),'真实 APScheduler 未运行启动恢复检查')
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            state=scheduler.followup.snapshot()['tasks']
            if state.get('qa_fund',{}).get('status')=='done':return
            self.done.wait(.01)
        self.fail('实际同步返回后未持久化完成状态')

    def test_actual_full_module_import_does_not_initialize_credentials(self):
        self.assertTrue(callable(self.web.LocalSyncFollowup))
        self.assertTrue(callable(self.web.TaskScheduler))

    def test_real_scheduler_starts_and_preserves_original_cron(self):
        scheduler=self.web.LocalSyncTaskScheduler()
        try:
            scheduler.start();self.wait_done(scheduler)
            self.assertIsNotNone(scheduler.scheduler.get_job('local_sync_task_qa_fund'))
            job=scheduler.scheduler.get_job('local_sync_followup_recovery')
            self.assertEqual(job.trigger.interval.total_seconds(),60)
            self.runner.assert_called_once_with(task_id='qa_fund',dry_run=False,
                include_disabled=False,background=False,trigger_source='upstream_success')
        finally:
            scheduler.stop()

    def test_real_app_initialization_wires_both_schedulers(self):
        with patch.object(self.web,'BaiduStorage',return_value=self.fake_storage), \
                patch.object(self.web.TaskScheduler,'_get_current_tasks',return_value=[]):
            try:
                success,error=self.web.init_app()
                self.assertTrue(success,error)
                self.assertTrue(callable(self.web.scheduler.on_task_completed))
                self.wait_done(self.web.local_sync_scheduler)
                self.assertEqual(self.web.scheduler.on_task_completed,
                    self.web.local_sync_scheduler.request_followup)
            finally:
                if self.web.scheduler:self.web.scheduler.stop(wait=True)
                if self.web.local_sync_scheduler:self.web.local_sync_scheduler.stop()
                self.web.scheduler=None;self.web.local_sync_scheduler=None

if __name__=='__main__':
    # 所有测试共用网络禁用屏障；仅允许被 mock 的转存/下载执行器。
    with patch.object(socket.socket,'connect',side_effect=AssertionError('QA_NETWORK_DISABLED')):
        loader=unittest.TestLoader();suite=unittest.TestSuite()
        suite.addTests(loader.loadTestsFromTestCase(RealRuntimeTests))
        suite.addTests(loader.discover(str(ROOT/'tests'),pattern='test_local_sync_followup*.py'))
        suite.addTests(loader.discover(str(ROOT/'tests'),pattern='storage_retry_test.py'))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
