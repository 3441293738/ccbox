import sys
from app.gui import main

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
        # Headless widget initialization for testing the packaged Tcl/Tk runtime.
        import json
        import tempfile
        from pathlib import Path
        from app.gui import App
        from app.controller import Controller, ROOT
        from transport import tls_diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Controller(tmp))
            app.withdraw()
            for name in app.pages:
                app.show(name)
                app.update_idletasks()
            assert 'lan_address' in app.fields
            assert app.values['lan_enabled'].get() is False
            assert callable(app.apply_lan) and callable(app.show_lan_connection)
            assert 'vscode_dir' in app.fields
            assert 'data_dir' in app.fields
            assert app.values['vscode_dir'].get() == ''
            assert callable(app.c.open_vscode)
            assert callable(app.c.open_sandbox_vscode)
            assert app.sandbox_editor_button.winfo_exists()
            assert callable(app.start_development)
            assert callable(app.quick_help)
            assert not app.disclosures['home_tools']['open']
            assert not app.disclosures['storage']['open']
            app.start_development()
            assert app.current_page == '环境配置'
            app.toggle_section('resources', True)
            assert app.disclosures['resources']['open']
            app.toggle_section('resources', False)
            assert callable(app.create_environment)
            assert callable(app.add_project)
            assert app.development_mode.get() == '沙箱开发'
            assert callable(app.disconnect_host)
            assert app.development_menu.winfo_exists()
            assert app.c.environment_id == 'default'
            from app.environments import Environments
            key = Environments(app.c.root).create('EXE 测试环境')
            app.load_environment(key)
            assert app.c.environment_id == key and app.proxy_vars['password'].get() == ''
            app.load_environment('default')
            assert len(app.environment_options) == 2
            app.destroy()
        result = {'gui_init': 'PASS', 'pages': 4, 'frozen': bool(getattr(sys, 'frozen', False)),
                  'editor_directory_field': True,
                  'data_migration_field': True, 'one_click_editor': True,
                  'multi_project_ui': True, 'isolated_environments_ui': True, 'sandbox_editor_ui': True,
                  'lan_access_ui': True, 'host_development_ui': True, 'guided_start_ui': True, 'collapsible_settings': True, 'version': '0.3.3-macos-arm64',
                  'tls': tls_diagnostics(),
                  'restore_toolbar': (ROOT / 'runtime/window_restore.py').is_file() and 'id="restore"' in (ROOT / 'runtime/desktop.html').read_text(encoding='utf-8'),
                  'clipboard_support': (ROOT / 'runtime/clipboard_transfer.py').is_file() and "uploadClipboard('text',value)" in (ROOT / 'runtime/desktop.html').read_text(encoding='utf-8'),
                  'runtime_sidecar': (ROOT / 'runtime/Dockerfile.sandbox').is_file()}
        Path(sys.argv[2]).write_text(json.dumps(result, indent=2), encoding='utf-8')
    else:
        main()
