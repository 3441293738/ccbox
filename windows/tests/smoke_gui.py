"""Widget-level smoke test. No Docker and no saved user configuration needed."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.gui import App
from app.controller import Controller

with tempfile.TemporaryDirectory() as tmp:
    app = App(Controller(Path(tmp) / 'bundle'))
    app.withdraw()
    for size in ('1120x820', '920x700'):
        app.geometry(size)
        for name in app.pages:
            app.show(name)
            app.update_idletasks()
            assert app.pages[name].winfo_reqwidth() > 0
    assert app.proxy_vars['password'].get() == ''
    assert app.report.get('1.0', 'end').startswith('尚未运行')
    assert app.values['vscode_dir'].get() == ''
    assert app.fields['vscode_dir']['page'] == '环境配置'
    with patch('app.gui_studio.filedialog.askdirectory', return_value=str(Path(tmp) / 'editor')):
        app.choose_vscode_folder()
    assert app.values['vscode_dir'].get() == str(Path(tmp) / 'editor')
    assert app.dirty
    with patch('app.gui_studio.filedialog.askdirectory', return_value=''):
        app.choose_vscode_folder()
    assert app.values['vscode_dir'].get() == str(Path(tmp) / 'editor')
    project = Path(tmp) / 'project'
    project.mkdir()
    app.values['workspace'].set(str(project))
    app.values['vscode_dir'].set('relative-path')
    try:
        app.collect_config()
        raise AssertionError('Invalid path accepted')
    except ValueError as error:
        assert error.key == 'vscode_dir'
        app.form_error(error)
    assert app.current_page == '环境配置'
    app.values['vscode_dir'].set('')
    assert app.collect_config()['vscode_dir'] == ''
    assert 'data_dir' in app.fields
    with patch('app.gui_studio.filedialog.askdirectory', return_value=str(Path(tmp) / 'migrated')):
        app.choose_data_folder()
    assert app.data_target.get() == str(Path(tmp) / 'migrated')
    with patch('app.gui_studio.filedialog.askdirectory', return_value=''):
        app.choose_data_folder()
    assert app.data_target.get() == str(Path(tmp) / 'migrated')
    app.dirty = True
    with patch('app.gui_studio.messagebox.askyesno') as dialog:
        app.migrate_storage()
        dialog.assert_not_called()
    app.dirty = False
    with patch('app.gui_studio.messagebox.askyesno', return_value=False), patch.object(app.c, 'migrate_data') as move:
        app.migrate_storage()
        move.assert_not_called()
    detail = '\n'.join(['Image ccbox2-test-gateway Pulling'] * 40 +
                       ['Error response from daemon: No such image: ccbox2-test-gateway:latest'])
    app.after_cancel(app._poll_id)
    app.messages.put(('error', ('启动环境', detail)))
    app.poll()
    app.update_idletasks()
    assert len(app.banner_label.cget('text')) < 125
    assert 'No such image' in app.banner_label.cget('text')
    assert detail in app.log.get('1.0', 'end')
    assert app.log_visible
    assert not app.busy
    app.toggle_log()
    app.reveal_log()
    assert app.log_visible
    app.destroy()
print('GUI smoke PASS: 4 pages, two window sizes, directory picker/cancel/default/validation, compact errors + full log')
