"""Widget-level behavior with blank fixtures, never user account data."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.controller import Controller, DEFAULT, parse_proxy
from app.gui import App
from app.environments import Environments

with tempfile.TemporaryDirectory() as tmp:
    base=Path(tmp)
    project=base/'project'
    project.mkdir()
    extra=base/'extra'
    extra.mkdir()
    c=Controller(base/'bundle')
    c.save({**DEFAULT,'workspace':str(project)},parse_proxy('socks5h://SERIAL:TOKEN@HOST:1080'))
    app=App(c, monitor_spaces=False)
    errors=[]
    app.report_callback_exception=lambda *args:errors.append(args)
    app.withdraw()
    try:
        with patch('app.gui_studio.filedialog.askdirectory',return_value=str(extra)):
            app.add_project()
        assert len(app.collect_config()['projects'])==1
        assert app.dirty
        with patch('app.gui_studio.messagebox.askyesno',return_value=False):
            app.remove_project(app.project_items[0]['id'])
        assert len(app.project_items)==1
        app.set_busy(True)
        app.add_project()
        assert len(app.project_items)==1
        app.set_busy(False)
        assert not errors, errors
        with patch('app.gui_studio.messagebox.askyesno',return_value=False),patch.object(app,'task') as task:
            app.dirty=False
            app.open_sandbox_editor()
            task.assert_not_called()
        with patch('app.gui_studio.messagebox.askyesno',return_value=True),patch.object(app,'task') as task,patch.object(app.c,'open_sandbox_vscode',return_value='OK') as launch:
            app.open_sandbox_editor()
            assert task.call_args.args[0]=='打开沙箱 VS Code'
            task.call_args.args[1]()
            launch.assert_called_once_with(allow_upgrade=True)
        app.dirty=True
        key=Environments(c.root).create('账号 B')
        app.refresh_environments()
        with patch('app.gui_studio.messagebox.askyesno',return_value=False):
            app.switch_environment('账号 B')
        assert app.c.environment_id=='default'
        with patch('app.gui_studio.messagebox.askyesno',return_value=True):
            app.switch_environment('账号 B')
        assert app.c.environment_id==key
        assert app.proxy_vars['password'].get()==''
        assert app.values['workspace'].get()==''
        assert app.project_items==[]
        assert not app.dirty
        assert app.stats['proxy'].get()=='未测试'
        app.load_environment('default')
        assert app.proxy_vars['password'].get()=='TOKEN'
        assert app.values['workspace'].get()==str(project)
        assert app.project_items==[]  # discarded edits were not silently saved
        # Account cards use captured IDs and never stop a space just to switch.
        from app.spaces import catalog_rows
        rows=catalog_rows(app.c.root)
        for row in rows:
            row.update(state='running', status='运行中', detail='测试数据')
        app.render_spaces(rows)
        assert len(app.space_cards)==2
        app.show('账号空间')
        assert not app.space_switcher.winfo_manager()
        with patch.object(Controller,'stop') as stop, patch.object(app,'start_development') as open_editor:
            app.space_action(key,'open')
            assert app.c.environment_id==key
            open_editor.assert_called_once()
            stop.assert_not_called()
        app.show('账号空间')
        with patch('app.gui_studio.messagebox.askyesno',return_value=True),patch.object(app,'task') as task:
            app.space_action('default','stop')
            assert app.c.environment_id=='default'
            assert task.call_args.args[1].__self__.environment_id=='default'
        app.dirty=True
        with patch('app.gui_studio.messagebox.askyesno',return_value=False),patch.object(app,'start_development') as launch:
            app.space_action(key,'open')
            assert app.c.environment_id=='default'
            launch.assert_not_called()
        app.dirty=False
        app.set_busy(True)
        app.space_action(key,'settings')
        assert app.c.environment_id=='default'
        assert app.space_cards[key]['open'].cget('state')=='disabled'
        app.set_busy(False)
        assert app.space_cards[key]['open'].cget('state')=='normal'
        before=app.space_cards[key]['name'].cget('text')
        app.spaces_received(('different root','wrong data',[],False))
        assert app.space_cards[key]['name'].cget('text')==before
        for scale in (1.0,1.5):
            import customtkinter as ctk
            ctk.set_widget_scaling(scale)
            app.geometry('980x700')
            for page in app.pages:
                app.show(page)
                app.update_idletasks()
            assert app.environment_menu.winfo_exists()
        app.set_busy(True)
        app.set_busy(False)
        assert not errors, errors
    finally:
        app.destroy()
print('Multi GUI PASS: add/remove/cancel, busy guard, dirty switch, blank account, restored account, reset checks, 100/150% scaling')
