"""Mac local build tests. Never write real Keychain entries during fixtures."""
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def get_password(self, service, identity):
        return self.values.get((service, identity))

    def set_password(self, service, identity, value):
        self.values[service, identity] = value

    def delete_password(self, service, identity):
        self.values.pop((service, identity), None)


if __name__ == '__main__':
    with ExitStack() as stack:
        if sys.platform == 'darwin':
            stack.enter_context(patch('app.mac_vault.backend', return_value=MemoryKeychain()))
            stack.enter_context(patch('app.controller.docker_environment',return_value=dict(os.environ)))
            stack.enter_context(patch('app.sandbox_editor.docker_environment',return_value=dict(os.environ)))
        suite = unittest.defaultTestLoader.discover(str(root/'tests'), pattern='test_*.py')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
