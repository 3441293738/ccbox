"""Display backend contract; browser integration lives in tools/test_desktop_display.cjs."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('display_supervisor', ROOT/'runtime/supervisor.py')
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class DisplayTests(unittest.TestCase):
    def test_native_resize_server_stays_private_and_clipboard_explicit(self):
        with patch.object(supervisor, 'launch') as launch, \
                patch.object(supervisor.signal, 'signal'), patch.object(Path, 'mkdir'), \
                patch.object(Path, 'write_text'), patch.object(supervisor, 'stopping', True):
            supervisor.main()
        commands = [c.args[0] for c in launch.call_args_list]
        vnc = next(c for c in commands if c[0] == 'Xtigervnc')
        for flag in ('-localhost', '-AcceptSetDesktopSize=1', '-AcceptCutText=0', '-SendCutText=0'):
            self.assertIn(flag, vnc)
        self.assertEqual(vnc[vnc.index('-rfbport')+1], '5900')
        self.assertEqual(vnc[vnc.index('-nolisten')+1], 'tcp')
        self.assertFalse(any(c[0] in ('Xvfb', 'x11vnc') for c in commands))
        ws = next(c for c in commands if c[0] == 'websockify')
        self.assertEqual(ws[-2:], ['127.0.0.1:6081', '127.0.0.1:5900'])

    def test_image_installs_resizable_backend(self):
        file = (ROOT/'runtime/Dockerfile.sandbox').read_text(encoding='utf-8')
        self.assertIn('tigervnc-standalone-server', file)
        self.assertNotIn('xvfb x11vnc', file)

    def test_existing_environments_require_explicit_upgrade(self):
        from app.sandbox_editor import REVISION, ready
        self.assertEqual(REVISION, '3')
        class Controller:
            data = Path('/fixture')
        with patch.object(Path, 'read_text', return_value='{"revision":"2","restore_toolbar":1}'):
            self.assertFalse(ready(Controller()))

    def test_viewer_uses_native_resize_and_bounded_viewport(self):
        html = (ROOT/'runtime/desktop.html').read_text(encoding='utf-8')
        for item in ('ResizeObserver', 'rfb.resizeSession=resize', 'max-width:3840px',
                     'max-height:2160px', 'height:100dvh', 'windowActive=false',
                     "mode==='auto'&&!document.hidden", '已适应窗口', '准备 / 修复',
                     'value="auto">自动铺满', 'value="follow">跟随画面',
                     'value="native">原始大小', 'value="9">高清', 'value="6">流畅'):
            self.assertIn(item, html)
        for private in ('rfb._', 'window.screen', 'window.devicePixelRatio', 'navigator.userAgent'):
            self.assertNotIn(private, html)
        self.assertIn('rfb.clipViewport=false', html)
        self.assertNotIn('object-fit:fill', html)


if __name__ == '__main__':
    unittest.main()
