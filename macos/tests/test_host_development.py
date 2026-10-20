import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.host_tools import HostJobs, ProcessTree, ProjectLease, TERMINAL, host_environment, inventory
from app.host_development import Preferences, projects


def python_command(source):
    # A real installed interpreter, not the frozen application executable.
    import base64
    code = "exec(__import__('base64').b64decode('"+base64.b64encode(source.encode()).decode()+"'))"
    if os.name == 'nt':
        return '& "'+sys.executable.replace('"','`"')+'" -c "'+code.replace('"','`"')+'"'
    return shlex.join([sys.executable, '-c', code])


class HostExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccbox host 中文 ')
        self.path = Path(self.tmp.name).resolve()
        self.jobs = HostJobs(self.path, 'fixture-account', lambda *_:True)

    def tearDown(self):
        self.jobs.close()
        time.sleep(.1)
        self.tmp.cleanup()

    def wait(self, job, seconds=12):
        limit = time.monotonic()+seconds
        while time.monotonic()<limit:
            state = self.jobs.call('host_output', {'job_id':job['job_id']})
            if state['status'] in TERMINAL: return state
            time.sleep(.05)
        self.fail('job did not terminate')

    def test_utf8_multiline_relative_cwd_and_exit(self):
        cmd = python_command("from pathlib import Path; Path('标记.txt').write_text('你好',encoding='utf-8'); print('你好'); print(Path.cwd())")
        state = self.wait(self.jobs.call('host_run', {'command':cmd}))
        self.assertEqual(state['status'], 'completed', state)
        self.assertIn('你好',state['output']); self.assertIn(str(self.path),state['output'])
        self.assertEqual((self.path/'标记.txt').read_text(encoding='utf-8'), '你好')

    def test_nonzero_exit_reported(self):
        state = self.wait(self.jobs.call('host_run', {'command':python_command('raise SystemExit(7)')}))
        self.assertEqual(state['exit_code'], 7, state)
        self.assertEqual(state['status'], 'failed')

    def test_denied_never_executes(self):
        self.jobs.approve = lambda *_:False
        state = self.wait(self.jobs.call('host_run', {'command':python_command("open('bad','w').write('x')")}))
        self.assertEqual(state['status'],'denied'); self.assertFalse((self.path/'bad').exists())

    def test_pending_cancel_is_not_approval(self):
        self.jobs.approve = lambda _, cancel: cancel.wait(4)
        job=self.jobs.call('host_run', {'command':python_command("open('bad','w').write('x')")})
        self.jobs.call('host_cancel', {'job_id':job['job_id']})
        self.assertEqual(self.wait(job)['status'],'cancelled'); self.assertFalse((self.path/'bad').exists())

    def test_one_job_at_a_time_and_foreign_job(self):
        self.jobs.approve = lambda _, cancel: cancel.wait(4)
        self.jobs.call('host_run', {'command':'echo OK'})
        with self.assertRaises(RuntimeError): self.jobs.call('host_run', {'command':'echo repeat'})
        with self.assertRaises(ValueError): self.jobs.call('host_output', {'job_id':'foreign'})

    def test_schema_rejects_host_paths_and_invalid_limits(self):
        for args in ({'command':'echo OK','cwd':'/'},{'command':'x','timeout':False}, {'command':'x','timeout':0},
                     {'command':'x','timeout':3601},{'command':'x\0'},{'command':[]},{'command':'x'*16001}):
            with self.subTest(args=str(args)[:60]), self.assertRaises(ValueError): self.jobs.call('host_run',args)

    def test_timeout_kills_child_tree(self):
        child="import time; time.sleep(3); open('child-survived','w').write('bad')"
        source="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"+repr(child)+"]); time.sleep(10)"
        state=self.wait(self.jobs.call('host_run',{'command':python_command(source),'timeout':1}))
        self.assertEqual(state['status'],'timed_out',state)
        time.sleep(3.2); self.assertFalse((self.path/'child-survived').exists())

    def test_closed_session_rejects_new_jobs(self):
        self.jobs.close()
        with self.assertRaises(RuntimeError): self.jobs.call('host_run',{'command':'echo OK'})

    def test_output_bounded(self):
        state=self.wait(self.jobs.call('host_run',{'command':python_command("print('测'*30000)")}))
        self.assertTrue(state['truncated']); self.assertLessEqual(len(state['output']),24000)
        self.assertLess(len(json.dumps(state).encode()),262144)


class HostConfigurationTests(unittest.TestCase):
    def test_process_group_kill_is_idempotent(self):
        from unittest.mock import Mock
        with patch('app.host_tools.os.name', 'posix'), patch('app.host_tools.signal.SIGKILL', 9, create=True), patch('app.host_tools.os.killpg', create=True) as kill:
            process=Mock(pid=123456)
            tree=ProcessTree(process)
            tree.kill(); tree.close(); tree.close(); tree.kill()
            kill.assert_called_once()

    def test_corrupt_preferences_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            pref=Preferences(d)
            for value in ([], {'version':1,'selected':'main','projects':{'main':[]}}):
                pref.file.write_text(json.dumps(value))
                with self.assertRaises(ValueError): pref.read()

    def test_no_model_or_proxy_credentials_in_environment(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY':'private','HTTP_PROXY':'secret','CLAUDE_CODE_OAUTH_TOKEN':'private',
                                     'SSH_AUTH_SOCK':'agent','AWS_SECRET_ACCESS_KEY':'private'}):
            env=host_environment()
        self.assertFalse(set(env)&{'ANTHROPIC_API_KEY','HTTP_PROXY','CLAUDE_CODE_OAUTH_TOKEN','SSH_AUTH_SOCK','AWS_SECRET_ACCESS_KEY'})

    def test_inventory_does_not_execute_tools(self):
        with patch('subprocess.Popen',side_effect=AssertionError('executed')):
            data=inventory()
        self.assertIn('java', data['tools']); self.assertIn('network',data)

    def test_project_lock_released(self):
        with tempfile.TemporaryDirectory() as d:
            lease=ProjectLease(Path(d)/'locks',d)
            with self.assertRaises(RuntimeError): ProjectLease(Path(d)/'locks',d)
            lease.close(); ProjectLease(Path(d)/'locks',d).close()

    def test_preferences_default_sandbox_account_separation_path_change(self):
        with tempfile.TemporaryDirectory() as d:
            a,b=Preferences(Path(d)/'a'),Preferences(Path(d)/'b')
            project={'id':'main','path':d,'readonly':False}
            self.assertEqual(a.mode(project),'sandbox')
            a.save(project,'host'); self.assertEqual(a.mode(project),'host')
            self.assertEqual(b.mode(project),'sandbox')
            self.assertEqual(a.mode({**project,'path':d+'/different'}),'sandbox')
            self.assertEqual(a.mode({**project,'readonly':True}),'sandbox')
            with self.assertRaises(ValueError): a.save({**project,'readonly':True},'host')

    def test_project_mapping_does_not_change_existing_config(self):
        config={'workspace':'HOST/project','readonly':False,'projects':[{'id':'p-123456abcdef','name':'other','path':'HOST/other','readonly':True}]}
        before=json.dumps(config)
        result=projects(config)
        self.assertEqual(result[1]['remote'],'/projects/p-123456abcdef')
        self.assertEqual(json.dumps(config),before)

    def test_mcp_protocol_offline_without_host_exec(self):
        lines=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{}}},
               {'jsonrpc':'2.0','method':'notifications/initialized'},
               {'jsonrpc':'2.0','id':2,'method':'tools/list'},
               {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'host_info','arguments':{}}}]
        result=subprocess.run([sys.executable,ROOT/'runtime/hostdev_runtime.py','mcp','--descriptor','nonexistent'],
            input=''.join(json.dumps(x)+'\n' for x in lines),text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(len(replies),3)
        self.assertEqual(replies[0]['result']['protocolVersion'],'2025-06-18')
        self.assertEqual({t['name'] for t in replies[1]['result']['tools']},{'host_info','host_run','host_output','host_cancel'})
        self.assertTrue(replies[2]['result']['isError'])


if __name__=='__main__': unittest.main()
