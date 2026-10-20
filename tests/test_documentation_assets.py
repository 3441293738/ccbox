import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('hygiene', ROOT/'tools/check_repository.py')
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


class DocumentationAssetsTests(unittest.TestCase):
    def test_documentation_jpeg_is_allowed(self):
        self.assertTrue(hygiene.is_documentation_image('docs/images/demo.jpg', b'\xff\xd8\xfffixture\xff\xd9'))

    def test_binary_outside_documentation_remains_rejected(self):
        for path in ('windows/data/demo.jpg', 'docs/demo.jpg', 'docs/images/nested/demo.jpg', 'tools/demo.jpg'):
            self.assertFalse(hygiene.is_documentation_image(path, b'\xff\xd8\xfffixture\xff\xd9'))

    def test_wrong_format_and_oversize_remain_rejected(self):
        for body in (b'not an image', b'\xff\xd8\xfftruncated', b'\xff\xd8\xff' + b'x' * 1_000_000 + b'\xff\xd9'):
            self.assertFalse(hygiene.is_documentation_image('docs/images/demo.jpg', body))
        self.assertFalse(hygiene.is_documentation_image('docs/images/demo.exe', b'\xff\xd8\xfffixture\xff\xd9'))

    def test_screenshots_are_bounded_and_typed(self):
        for name in ('home.jpg', 'projects.jpg', 'proxy.jpg'):
            file = ROOT/'docs/images'/name
            self.assertTrue(hygiene.is_documentation_image(file.relative_to(ROOT), file.read_bytes()))


if __name__ == '__main__':
    unittest.main()
