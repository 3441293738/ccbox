"""Small, testable decisions for the beginner entry point. No network probes."""
import json


def next_step(config, proxy):
    if not str(config.get('workspace', '')).strip():
        return 'project'
    if not str(proxy.get('host', '')).strip():
        return 'proxy'
    return 'open'


def runtime_changed(controller, config, proxy):
    """Compare desired settings with the last prepared runtime, not credentials on UI.

    A stopped environment has no active secret and will be prepared on opening.
    Inconsistent/malformed runtime data is an error, never a reason to guess.
    """
    active = controller.data / 'active/upstream.json'
    if not controller.file.exists() or not active.exists() or not active.stat().st_size:
        return False
    from .controller import make_compose
    current = json.loads(controller.file.read_text(encoding='utf-8'))
    expected = make_compose(controller.root, controller.data, config, controller.credentials()['project'])
    upstream = json.loads(active.read_text(encoding='utf-8'))
    return current != expected or upstream.get('proxy') != proxy


def open_workspace(controller, config, proxy, *, save, restart, upgrade, on_saved, before_launch=None, selected_workspace=None):
    """Called only after UI confirmation; stops only the selected environment."""
    if save:
        controller.save(config, proxy)
        on_saved()
    if restart:
        controller.log('正在应用新设置：短暂停止当前环境，项目和登录资料保留。')
        controller.stop()
    if before_launch or selected_workspace:
        from .sandbox_editor import open_editor
        return open_editor(controller, upgrade, before_launch=before_launch, selected_workspace=selected_workspace)
    return controller.open_sandbox_vscode(allow_upgrade=upgrade)
