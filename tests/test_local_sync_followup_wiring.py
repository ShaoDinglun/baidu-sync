import ast
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from datetime import datetime,timezone
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('followup_isolated',ROOT/'backend/local_sync_followup.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def actual_class(file,name,namespace):
    # 编译真实类的 AST；仅替换外部依赖，避免初始化认证、Web 服务或通知。
    tree=ast.parse((ROOT/file).read_text())
    node=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/file),'exec'),namespace)
    return namespace[name]

class EntryTests(unittest.TestCase):
    def setUp(self):
        self.logger=Mock()
        self.task=dict(task_uid='cloud_fund',order=1,name='fund',url='private_url',pwd='private_pwd',save_dir='/apps/bypy/fund')
        self.storage=Mock()
        self.storage.config={'baidu':{'tasks':[self.task]}}
        self.storage.get_task_by_uid.side_effect=lambda uid:self.task
        self.storage.is_valid.return_value=True
        def update(order,status,*args,**kwargs):
            self.task.update(status=status,last_execute_time=int(time.time()))
            return True
        self.storage.update_task_status_by_order.side_effect=update
        cls=actual_class('backend/scheduler.py','TaskScheduler',dict(logger=self.logger,time=time))
        self.scheduler=cls.__new__(cls)
        self.scheduler.storage=self.storage
        self.scheduler._execution_lock=threading.Lock()
        self.scheduler._mark_task_running=Mock()
        self.scheduler._mark_task_finished=Mock()
        self.scheduler._is_shutdown_requested=lambda:False
        self.scheduler._add_to_notification_buffer=Mock()
        self.scheduler.on_task_completed=Mock()

    def test_real_success_calls_hook_with_completed_event_only(self):
        self.storage.transfer_share.return_value={'success':True,'transferred_files':['a.zip']}
        self.assertTrue(self.scheduler._execute_single_task(self.task))
        self.scheduler.on_task_completed.assert_called_once()
        event=self.scheduler.on_task_completed.call_args.args[0]
        self.assertEqual(set(event),{'task_uid','status','last_execute_time','save_dir'})
        self.assertEqual(event['status'],'success')
        self.assertNotIn('private_',str(event))

    def test_real_skipped_success_still_requests_local_recovery(self):
        self.storage.transfer_share.return_value={'success':True,'skipped':True}
        self.assertTrue(self.scheduler._execute_single_task(self.task))
        self.assertEqual(self.scheduler.on_task_completed.call_args.args[0]['status'],'skipped')

    def test_real_failure_and_cancel_do_not_request_followup(self):
        for result in [{'success':False,'error':'not ready'},{'success':False,'cancelled':True}]:
            with self.subTest(result=result):
                self.storage.transfer_share.return_value=result
                self.assertFalse(self.scheduler._execute_single_task(self.task))
                self.scheduler.on_task_completed.assert_not_called()

    def test_hook_failure_keeps_cloud_success_and_logs_error_type(self):
        self.storage.transfer_share.return_value={'success':True}
        self.scheduler.on_task_completed.side_effect=OSError('private_pwd')
        self.assertTrue(self.scheduler._execute_single_task(self.task))
        self.assertIn('OSError',str(self.logger.error.call_args_list))
        self.assertNotIn('private_pwd',str(self.logger.method_calls))

    def test_hook_optional_for_existing_callers(self):
        self.storage.transfer_share.return_value={'success':True}
        self.scheduler.on_task_completed=None
        self.assertTrue(self.scheduler._execute_single_task(self.task))

    def test_share_url_and_password_are_not_logged(self):
        self.storage.transfer_share.return_value={'success':True}
        self.scheduler._execute_single_task(self.task)
        self.assertNotIn('private_url',str(self.logger.method_calls))
        self.assertNotIn('private_pwd',str(self.logger.method_calls))

class FakeBackground:
    def __init__(self,**kwargs):self.jobs={};self.kwargs=kwargs
    def add_job(self,func,trigger=None,**kwargs):
        self.jobs[kwargs['id']]=SimpleNamespace(id=kwargs['id'],func=func,trigger=trigger,kwargs=kwargs)
    def start(self):pass
    def shutdown(self,**kwargs):pass
    def get_jobs(self):return list(self.jobs.values())
    def remove_job(self,key):del self.jobs[key]

class SchedulerWiringTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.local=[dict(task_id='local_fund',remote_root='/fund',enabled=True,auto_run_enabled=True,cron='0 17-23 * * 1-6')]
        self.cloud=dict(task_uid='cloud_fund',save_dir='/apps/bypy/fund',status='success',last_execute_time=int(time.time()))
        self.run=Mock(return_value=(True,'completed',{'status':'success','running':False}))
        self.storage=Mock();self.storage.list_tasks.return_value=[self.cloud]
        self.ns=dict(LocalSyncFollowup=module.LocalSyncFollowup,
            LOCAL_SYNC_FOLLOWUP_STATE_FILE=Path(self.temp.name)/'followup.json',
            _load_bypy_sync_tasks=lambda **kwargs:self.local,
            _run_local_sync_task=self.run,storage=self.storage,
            BackgroundScheduler=FakeBackground,ThreadPoolExecutor=ThreadPoolExecutor,
            pytz=SimpleNamespace(timezone=lambda x:timezone.utc),datetime=datetime,
            logger=Mock(),_normalize_local_sync_cron=lambda x:x,
            convert_cron_weekday=lambda x:x,
            CronTrigger=SimpleNamespace(from_crontab=lambda x,**kwargs:x))
        cls=actual_class('backend/web_app.py','LocalSyncTaskScheduler',self.ns)
        self.scheduler=cls()

    def test_existing_executor_retained_and_60_second_recovery_registered(self):
        self.scheduler.start()
        self.assertIn('local_sync_followup_recovery',self.scheduler.scheduler.jobs)
        job=self.scheduler.scheduler.jobs['local_sync_followup_recovery']
        self.assertEqual(job.trigger,'interval')
        self.assertEqual(job.kwargs['seconds'],60)
        self.assertIn('local_sync_task_local_fund',self.scheduler.scheduler.jobs)

    def test_cloud_hook_persists_only_without_network(self):
        self.scheduler.request_followup(self.cloud)
        self.run.assert_not_called()
        self.assertEqual(self.scheduler.followup.snapshot()['tasks']['local_fund']['requested'],1)

    def test_recovery_runs_actual_existing_entry_synchronously(self):
        self.scheduler._drain_followups()
        self.run.assert_called_once_with(task_id='local_fund',dry_run=False,
            include_disabled=False,background=False,trigger_source='upstream_success')
        self.assertEqual(self.scheduler.followup.snapshot()['tasks']['local_fund']['status'],'done')

    def test_cron_reconciliation_does_not_remove_recovery_job(self):
        self.scheduler.start();self.local.clear();self.scheduler.sync_jobs()
        self.assertEqual(set(self.scheduler.scheduler.jobs),{'local_sync_followup_recovery'})

    def test_upstream_busy_task_does_not_erase_pending_completion(self):
        self.run.return_value=(False,'busy',{'running':True})
        self.scheduler._drain_followups()
        self.assertEqual(self.scheduler.followup.snapshot()['tasks']['local_fund']['status'],'retry_pending')

    def test_corrupt_state_is_observable_and_not_cleared(self):
        path=self.ns['LOCAL_SYNC_FOLLOWUP_STATE_FILE'];path.write_text('{bad')
        self.scheduler._drain_followups()
        self.assertEqual(path.read_text(),'{bad')
        self.ns['logger'].error.assert_called_once()

    def test_init_wires_hook_before_cloud_scheduler_start(self):
        tree=ast.parse((ROOT/'backend/web_app.py').read_text())
        node=next(x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=='init_app')
        source=ast.unparse(node)
        self.assertLess(source.index('local_sync_scheduler = LocalSyncTaskScheduler()'),source.index('scheduler = TaskScheduler(storage)'))
        cloud_start=next(x.lineno for x in ast.walk(node) if isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute) and isinstance(x.func.value,ast.Name) and x.func.value.id=='scheduler' and x.func.attr=='start')
        hook=next(x.lineno for x in ast.walk(node) if isinstance(x,ast.Assign) and any(isinstance(t,ast.Attribute) and t.attr=='on_task_completed' for t in x.targets))
        self.assertLess(hook,cloud_start)
        self.assertLess(source.index('_set_local_sync_in_process_running(False)'),source.index('local_sync_scheduler.start()'))

if __name__=='__main__':unittest.main()
