"""持久化云端完成事件，接续已有本地同步；不读取认证或执行转存。"""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import stat
import time
from uuid import uuid4


def _identifier(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',value) is not None


def _root(value,cloud=False):
    if not isinstance(value,str) or not value.strip() or any(c in value for c in '\x00\n\r:'):
        return None
    value=value.strip().replace('\\','/')
    if '..' in value.split('/'):
        return None
    value=posixpath.normpath('/'+value.lstrip('/'))
    if cloud and (value=='/apps/bypy' or value.startswith('/apps/bypy/')):
        value=value[len('/apps/bypy'):]
    return value if value and value!='/' else None


class LocalSyncFollowup:
    """用完成事件驱动已有同步器，队列状态与数据下载结果分别记录。

    输入state_path为显式Path/str，无生产默认值；load_tasks为返回已配置本地任务
    列表的无参Callable，run_task接收task_id并返回(bool,message,status_dict)，
    必须同步等待实际任务结束；now默认time.time，retry_seconds默认60秒。
    仅匹配enabled和auto_run_enabled均True、规范化远端根相等的任务。
    输出见request/run_pending/snapshot；状态损坏、权限或真实I/O错误明确传播。
    不保存分享URL、提取码、认证信息或原始异常，不调用通知或改变原下载策略。
    例：queue=LocalSyncFollowup(path,load_tasks=loader,run_task=existing_sync)。
    """
    def __init__(self,state_path,*,load_tasks,run_task,now=time.time,retry_seconds=60):
        if not callable(load_tasks) or not callable(run_task) or not callable(now):
            raise TypeError('接续队列必须提供既有任务加载器、同步执行器和时钟')
        if type(retry_seconds) is not int or not 1<=retry_seconds<=3600:
            raise ValueError('接续重试间隔必须为1至3600秒')
        self.path=Path(state_path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.load_tasks,self.run_task,self.now=load_tasks,run_task,now
        self.retry_seconds=retry_seconds

    @contextmanager
    def _locked(self,kind,nonblocking=False):
        path=self.path.with_name(self.path.name+'.'+kind+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            self._protected(os.fstat(fd))
            fcntl.flock(fd,fcntl.LOCK_EX|(fcntl.LOCK_NB if nonblocking else 0))
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _protected(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1
                or info.st_uid!=os.geteuid() or info.st_mode&0o022):
            raise ValueError('接续状态必须是当前用户独有且不可被其他用户写入的普通文件')

    def _load(self):
        if self.path.is_symlink():
            raise ValueError('接续状态不允许符号链接')
        if not self.path.exists():
            return {'version':1,'tasks':{}}
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'r',encoding='utf-8') as stream:
            self._protected(os.fstat(stream.fileno()))
            value=json.load(stream)
        if not isinstance(value,dict) or set(value)!={'version','tasks'} or value['version']!=1 or not isinstance(value['tasks'],dict):
            raise ValueError('接续状态版本或结构损坏，保留原件')
        for key,item in value['tasks'].items():
            if not _identifier(key) or not isinstance(item,dict):
                raise ValueError('接续任务标识损坏')
            if (set(item)!={'root','seen','requested','completed','retry_at','failures','status','error_type'}
                    or _root(item['root'])!=item['root']
                    or type(item['requested']) is not int or type(item['completed']) is not int
                    or not 0<=item['completed']<=item['requested']
                    or type(item['failures']) is not int or item['failures']<0
                    or type(item['retry_at']) not in (int,float) or not math.isfinite(item['retry_at'])
                    or item['status'] not in {'pending','retry_pending','done','cancelled'}
                    or not isinstance(item['error_type'],str) or len(item['error_type'])>128
                    or not isinstance(item['seen'],list) or len(item['seen'])>64
                    or any(not isinstance(x,str) or re.fullmatch('[0-9a-f]{64}',x) is None for x in item['seen'])):
                raise ValueError('接续任务状态损坏，禁止清空或冒充已完成')
        return value

    def _save(self,value):
        temporary=self.path.with_name('.'+self.path.name+'.'+uuid4().hex+'.pending')
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,self.path)
        fd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _tasks(self):
        tasks=self.load_tasks()
        if not isinstance(tasks,list):
            raise ValueError('本地同步任务加载结果必须为列表')
        result={}
        for task in tasks:
            if not isinstance(task,dict) or task.get('enabled') is not True or task.get('auto_run_enabled') is not True:
                continue
            key,root=task.get('task_id'),_root(task.get('remote_root'))
            if not _identifier(key) or root is None:
                continue
            if key in result:
                raise ValueError('本地同步任务ID重复，禁止歧义执行')
            result[key]=root
        return result

    def request(self,cloud_task):
        """输入已完成云端任务dict，返回新增或合并请求的本地task_id列表。

        必需task_uid、save_dir、last_execute_time和success/skipped状态；时间为
        实际正有限Unix时间，允许120秒时钟偏差。坏、未完成、空或不匹配事件
        返回[]，不写入任务数据。重复事件持久去重，执行中的新事件保留新序号。
        例：ids=queue.request(completed_cloud_task)。无外部I/O或网络请求。
        """
        if not isinstance(cloud_task,dict) or cloud_task.get('status') not in {'success','skipped'}:
            return []
        uid=cloud_task.get('task_uid');stamp=cloud_task.get('last_execute_time')
        root=_root(cloud_task.get('save_dir'),cloud=True)
        if (not _identifier(uid) or root is None or type(stamp) not in (int,float)
                or not math.isfinite(stamp) or stamp<=0 or stamp>self.now()+120):
            return []
        targets=[key for key,path in self._tasks().items() if path==root]
        if not targets:return []
        digest=hashlib.sha256(json.dumps([uid,float(stamp)],separators=(',',':')).encode()).hexdigest()
        changed=[]
        with self._locked('state'):
            state=self._load()
            for key in targets:
                item=state['tasks'].get(key)
                if item is None or item['root']!=root:
                    item=dict(root=root,seen=[],requested=0,completed=0,retry_at=0,
                              failures=0,status='pending',error_type='')
                    state['tasks'][key]=item
                if digest in item['seen']:continue
                item['seen']=(item['seen']+[digest])[-64:]
                item['requested']+=1;item['retry_at']=self.now();item['status']='pending'
                changed.append(key)
            if changed:self._save(state)
        return changed

    def snapshot(self):
        """无参数，返回已校验队列dict副本；不获取认证、执行任务或变更状态。

        字段version固定1，tasks为ID到根、去重摘要、请求/完成序号、重试时间、
        失败次数、状态及脱敏异常类型；损坏状态ValueError。例：queue.snapshot()。
        """
        with self._locked('state'):return self._load()

    def run_pending(self,cloud_tasks=()):
        """先恢复完成事件，再串行执行到期请求；返回本轮完成、失败和取消计数。

        cloud_tasks为可选已完成云端任务可迭代对象，默认空；供重启恢复与周期兜底。
        使用跨实例非阻塞执行锁，不重复并发下载；任务失败保留并至少间隔重试秒数，
        不吞掉状态/配置I/O错误。同步器网络异常只保存异常类型，不保存消息。
        执行期间新请求不能被旧结果清除，禁用、删除或根变更取消旧请求。
        例：queue.run_pending(current_cloud_task_states)。仅此方法调用既有下载器。
        """
        for task in cloud_tasks:self.request(task)
        summary=dict(completed=0,retry_pending=0,cancelled=0,busy=False)
        with ExitStack() as locks:
            try:
                locks.enter_context(self._locked('drain',nonblocking=True))
            except BlockingIOError:
                summary['busy']=True
                return summary
            # 只将执行锁竞争视为忙；状态写入失败必须明确传播，保留待办。
            self._drain(summary)
        return summary

    def _drain(self,summary):
        with self._locked('state'):
            state=self._load()
            claims=[(key,item['root'],item['requested']) for key,item in state['tasks'].items()
                    if item['requested']>item['completed'] and item['retry_at']<=self.now()]
        for key,root,sequence in claims:
            if self._tasks().get(key)!=root:
                self._finish(key,root,sequence,'cancelled','')
                summary['cancelled']+=1
                continue
            try:
                result=self.run_task(key)
                if not isinstance(result,tuple) or len(result)!=3 or type(result[0]) is not bool or not isinstance(result[2],dict):
                    raise ValueError('本地同步执行器返回协议无效')
                success,_,status=result
                if success and (status.get('running') or status.get('status') in {'failed','stopped','cancelled'}):
                    raise ValueError('本地同步尚未完成，禁止清除接续请求')
                outcome='done' if success else 'retry_pending'
                error='' if success else ('SyncBusy' if status.get('running') else 'SyncFailed')
            except Exception as exc:
                outcome,error='retry_pending',type(exc).__name__[:128]
            self._finish(key,root,sequence,outcome,error)
            summary['completed' if outcome=='done' else 'retry_pending']+=1

    def _finish(self,key,root,sequence,outcome,error):
        with self._locked('state'):
            state=self._load();item=state['tasks'].get(key)
            if item is None or item['root']!=root:return
            if outcome in {'done','cancelled'}:
                item['completed']=max(item['completed'],sequence)
                item['status']=outcome if item['requested']==item['completed'] else 'pending'
                item['retry_at']=self.now();item['failures']=0;item['error_type']=''
            else:
                item['status']='retry_pending';item['failures']+=1;item['error_type']=error
                item['retry_at']=self.now()+self.retry_seconds
            self._save(state)
