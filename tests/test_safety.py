#!/usr/bin/env python3
"""Integrity and cost regressions at public CLI/hook boundaries; no paid calls.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'skills/session-handoff/hooks/handoff_ledger.py'
WATCHER = LEDGER.with_name('context_watch.py')

class SafetyReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='shk-safety-review-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        (self.root / '.handoffs').mkdir()
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('CONTEXT_WATCH_', 'HANDOFF_', 'AUTORESUME', 'TYPESAFE_', 'AGENTSROOM_'))}
        self.env.update(HOME=self.tmp.name, TMPDIR=self.tmp.name, CONTEXT_WATCH_LOG='0',
                        CONTEXT_WATCH_RESUME_LOG='0', CONTEXT_WATCH_ORIGIN='test', TYPESAFE_API_KEY='')

    def cli(self, *args, document=None):
        p = subprocess.run([sys.executable,str(LEDGER),*map(str,args)], cwd=self.root,env=self.env,
                           input=json.dumps(document) if document is not None else None,
                           capture_output=True,text=True,timeout=10)
        self.assertTrue(p.stdout, p.stderr)
        return json.loads(p.stdout)

    def save(self, session='writer', request='first', **fields):
        doc=dict(topic='work',description='A concrete task.',
                 body='## Objective\nRepair parser.\n## Current state\nOne test fails.\n## Next steps\nFix that test.\n')
        doc.update(fields)
        return self.cli('save','--session',session,'--request-id',request,document=doc)

    def hook(self,prompt):
        event=dict(hook_event_name='UserPromptSubmit',session_id='reader',cwd=str(self.root),prompt=prompt)
        p=subprocess.run([sys.executable,str(WATCHER)],cwd=self.root,env=self.env,
                         input=json.dumps(event),capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        return p.stdout

    def test_control_complete_checkpoint_round_trip(self):
        saved=self.save()
        self.assertEqual(saved['outcome'],'saved')
        self.assertIn(' resume ',self.hook('resume'))
        prepared=self.cli('prepare',saved['path'],'--session','reader','--execute')
        self.assertEqual(prepared['outcome'],'prepared')
        self.assertEqual(self.cli('acknowledge',saved['path'],'--session','reader')['outcome'],'resumed')
        self.assertEqual(self.cli('lookup')['outcome'],'none')

    def test_claimed_topic_cannot_be_hidden_by_another_writer(self):
        first=self.save()
        self.assertEqual(first['outcome'],'saved')
        prepared=self.cli('prepare',first['path'],'--session','owner','--execute')
        self.assertEqual(prepared['outcome'],'prepared')
        second=self.save(session='other',request='second')
        ack=self.cli('acknowledge',first['path'],'--session','owner')
        self.assertEqual(second['outcome'],'conflict')
        self.assertEqual(ack['outcome'],'resumed','another writer must not invalidate the existing owner')

    def test_concurrent_publishers_require_explicit_lineage(self):
        doc=dict(topic='work',body='## Objective\nFix parser.\n## Current state\nReady.\n## Next steps\nRun checks.\n')
        children=[]
        for session in ('writer-a','writer-b'):
            child=subprocess.Popen([sys.executable,str(LEDGER),'save','--session',session,'--request-id','fixture'],
                                   cwd=self.root,env=self.env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            child.stdin.write(json.dumps(doc))
            child.stdin.close()
            child.stdin=None
            children.append(child)
        outcomes=[json.loads(child.communicate(timeout=10)[0]) for child in children]
        self.assertEqual(sorted(x['outcome'] for x in outcomes),['conflict','saved'])
        saved=next(x for x in outcomes if x['outcome']=='saved')
        successor=self.save(request='explicit-next',predecessor=saved['path'])
        self.assertEqual(successor['outcome'],'saved')
        self.assertEqual(self.cli('lookup')['items'][0]['path'],successor['path'])

    def test_empty_legacy_checkpoint_cannot_be_acknowledged(self):
        path=self.root/'.handoffs/work.md'
        path.write_text('---\ntopic: work\nstatus: open\n---\n')
        result=self.cli('prepare',path,'--session','reader','--execute')
        ack=self.cli('acknowledge',path,'--session','reader')
        self.assertEqual(result['outcome'],'needs-context')
        self.assertEqual(ack['outcome'],'blocked')
        self.assertEqual(self.cli('lookup')['total'],1)

    def test_generic_two_word_requests_find_existing_handoff(self):
        self.save()
        for prompt in ['retrieve handoff','resume handoff','Retrieve handoff!','show handoff','resume the handoff','retrieve work']:
            with self.subTest(prompt=prompt):
                response=self.hook(prompt)
                self.assertRegex(response,' (prepare|resume) ')
                if prompt.lower().startswith(('retrieve', 'show')):
                    self.assertNotIn('--execute',response)

    def test_installer_preserves_existing_hook_failure(self):
        home=Path(self.tmp.name)/'codex'
        home.mkdir()
        config=home/'hooks.json'
        config.write_text(json.dumps({'hooks':{'UserPromptSubmit':[{'hooks':[{'type':'command','command':'exit 2','timeout':20}]}]}}))
        env={**self.env,'CODEX_HOME':str(home)}
        subprocess.run([sys.executable,str(LEDGER.parent.parent/'install.py'),'codex'],env=env,
                       capture_output=True,text=True,check=True,timeout=10)
        hook=json.loads(config.read_text())['hooks']['UserPromptSubmit'][0]['hooks'][0]
        completed=subprocess.run(['sh','-c',hook['command']],input='{}',env=env,cwd=self.root,
                                 capture_output=True,text=True,timeout=10)
        self.assertEqual(completed.returncode,2,'registration replaced the original hook failure with success')

    def test_auto_runner_stops_after_child_failure(self):
        child=Path(self.tmp.name)/'failed-agent.py'
        child.write_text("""import json,os,subprocess,sys
with open('calls.txt','a') as f:f.write('called\\n')
doc={'topic':'work-'+os.environ['HANDOFF_RUN_ID'],'body':'## Objective\\nFix parser.\\n## Current state\\nAttempt failed.\\n## Next steps\\nNeeds inspection.\\n'}
subprocess.run([sys.executable,os.environ['AUDIT_LEDGER'],'save','--session',os.environ['HANDOFF_RUN_ID'],'--request-id','fixture'],input=json.dumps(doc),text=True,capture_output=True,check=True)
sys.exit(1)
""")
        env={**self.env,'AUDIT_LEDGER':str(LEDGER)}
        p=subprocess.run([sys.executable,str(WATCHER),'auto','--max','3','--prompt','fixture','--',sys.executable,str(child)],
                         cwd=self.root,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,1,p.stderr)
        count=len((self.root/'calls.txt').read_text().splitlines())
        self.assertEqual(count,1,f'failed child launched {count} times before the run-count cap')

    def test_auto_runner_reports_exhaustion_instead_of_success(self):
        child=Path(self.tmp.name)/'checkpoint-agent.py'
        child.write_text("""import json,os,subprocess,sys
run=os.environ['HANDOFF_RUN_ID']
doc={'topic':'run-'+run,'body':'## Objective\\nFix parser.\\n## Current state\\nCheckpointed.\\n## Next steps\\nContinue work.\\n'}
subprocess.run([sys.executable,os.environ['AUDIT_LEDGER'],'save','--session',run,'--request-id','fixture'],input=json.dumps(doc),text=True,capture_output=True,check=True)
""")
        p=subprocess.run([sys.executable,str(WATCHER),'auto','--max','1','--',sys.executable,str(child)],
                         cwd=self.root,env={**self.env,'AUDIT_LEDGER':str(LEDGER)},capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,75,p.stderr)
        self.assertEqual(self.cli('lookup')['total'],1)

    def test_auto_runner_requires_a_positive_run_limit(self):
        for limit in ('0','-1'):
            with self.subTest(limit=limit):
                p=subprocess.run([sys.executable,str(WATCHER),'auto','--max',limit,'--','true'],
                                 cwd=self.root,env=self.env,capture_output=True,text=True,timeout=10)
                self.assertEqual(p.returncode,2)

    def stub_routing(self):
        # Stub the external HTTP boundary in the child Python process; never send a credential/request.
        adapter=Path(self.tmp.name)/'http-boundary'
        adapter.mkdir()
        calls=Path(self.tmp.name)/'requests.jsonl'
        (adapter/'sitecustomize.py').write_text('''import io,json,os,urllib.request
from pathlib import Path
def fake_urlopen(request, *args, **kwargs):
    assert request.full_url == 'https://api.typesafe.ai/v1/systemone'
    with open(os.environ['AUDIT_REQUESTS'],'a') as f: f.write(json.dumps({'url':request.full_url})+'\\n')
    return io.BytesIO(b'{"answers":{"route":{"choice":"unrelated","confidence":0.99}}}')
urllib.request.urlopen=fake_urlopen
''')
        self.env.update(PYTHONPATH=str(adapter),TYPESAFE_API_KEY='audit-dummy-not-a-secret',AUDIT_REQUESTS=str(calls))
        return calls

    def test_default_routing_does_not_make_repeated_network_calls(self):
        self.save()
        calls=self.stub_routing()
        self.hook('Add a README badge.')
        self.hook('Add a README badge.')
        count=len(calls.read_text().splitlines()) if calls.exists() else 0
        self.assertEqual(count,0,f'{count} external routing requests without explicit opt-in; same session/prompt')

    def test_opted_in_routing_is_once_per_session_and_preserves_local_commands(self):
        self.save()
        calls=self.stub_routing()
        self.env['CONTEXT_WATCH_JEV']='1'
        self.hook('Add a README badge.')
        self.hook('Add a different README badge.')
        self.assertEqual(len(calls.read_text().splitlines()),1)
        self.assertIn(' resume ',self.hook('resume'))
        self.assertEqual(len(calls.read_text().splitlines()),1)

if __name__=='__main__':
    unittest.main(verbosity=2)
