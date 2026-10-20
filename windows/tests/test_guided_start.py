import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.controller import Controller, DEFAULT, make_compose, parse_proxy
from app.guided_start import next_step, runtime_changed, open_workspace


class GuidedStartTests(unittest.TestCase):
    def test_missing_fields_route_in_order(self):
        self.assertEqual(next_step({},{}),'project')
        self.assertEqual(next_step({'workspace':'  '},{'host':'HOST'}),'project')
        self.assertEqual(next_step({'workspace':'D:/project'},{}),'proxy')
        self.assertEqual(next_step({'workspace':'D:/project'},{'host':'HOST'}),'open')

    def test_compare_applied_not_just_saved_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); project=root/'project';project.mkdir()
            c=Controller(root/'bundle')
            cfg={**DEFAULT,'workspace':str(project)}
            proxy=parse_proxy('socks5h://SERIAL:TOKEN@HOST:1080')
            c.save(cfg,proxy)
            self.assertFalse(runtime_changed(c,cfg,proxy))
            c.file.write_text(json.dumps(make_compose(c.root,c.data,cfg,c.credentials()['project'])))
            active=c.data/'active/upstream.json';active.parent.mkdir(exist_ok=True)
            active.write_text(json.dumps({'proxy':proxy}))
            self.assertFalse(runtime_changed(c,cfg,proxy))
            self.assertTrue(runtime_changed(c,{**cfg,'timezone':'UTC'},proxy))
            self.assertTrue(runtime_changed(c,cfg,{**proxy,'password':'DIFFERENT'}))
            self.assertFalse(runtime_changed(c,{**cfg,'vscode_dir':str(root/'local')},proxy))
            active.write_text('')
            self.assertFalse(runtime_changed(c,{**cfg,'timezone':'UTC'},proxy))

    def test_malformed_runtime_does_not_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Controller(Path(tmp)/'bundle')
            c.file.parent.mkdir(parents=True,exist_ok=True);c.file.write_text('not json')
            active=c.data/'active/upstream.json';active.parent.mkdir();active.write_text('{}')
            with self.assertRaises(ValueError):runtime_changed(c,{}, {})

    def test_sequence_save_event_stop_open(self):
        events=[];c=Mock()
        c.save.side_effect=lambda *_:events.append('save')
        c.stop.side_effect=lambda:events.append('stop')
        c.open_sandbox_vscode.side_effect=lambda **_:events.append('open') or 'OK'
        result=open_workspace(c,{}, {},save=True,restart=True,upgrade=True,on_saved=lambda:events.append('saved'))
        self.assertEqual(result,'OK');self.assertEqual(events,['save','saved','stop','open'])
        c.open_sandbox_vscode.assert_called_once_with(allow_upgrade=True)

    def test_regular_open_does_not_stop_or_save(self):
        c=Mock();saved=Mock()
        open_workspace(c,{}, {},save=False,restart=False,upgrade=False,on_saved=saved)
        c.save.assert_not_called();c.stop.assert_not_called();saved.assert_not_called()

    def test_save_failure_never_stops_existing_work(self):
        c=Mock();c.save.side_effect=RuntimeError('save failed');saved=Mock()
        with self.assertRaises(RuntimeError):
            open_workspace(c,{}, {},save=True,restart=True,upgrade=True,on_saved=saved)
        c.stop.assert_not_called();c.open_sandbox_vscode.assert_not_called();saved.assert_not_called()

    def test_start_failure_still_reports_saved_state(self):
        c=Mock();saved=Mock();c.open_sandbox_vscode.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            open_workspace(c,{}, {},save=True,restart=False,upgrade=False,on_saved=saved)
        saved.assert_called_once()


if __name__=='__main__':unittest.main()
