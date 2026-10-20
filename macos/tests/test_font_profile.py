"""The browser font profile is a compatibility choice, not a location score."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FontProfileTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / 'runtime/Dockerfile.sandbox').read_text(encoding='utf-8')

    def test_generic_cjk_fallback_is_installed(self):
        self.assertIn('fonts-droid-fallback', self.dockerfile)
        self.assertNotIn('fonts-noto-cjk', self.dockerfile)

    def test_color_emoji_font_is_installed(self):
        self.assertIn('fonts-noto-color-emoji', self.dockerfile)

    def test_no_recommended_all_font_collection(self):
        self.assertIn('apt-get install -y --no-install-recommends', self.dockerfile)
        self.assertNotIn('fonts-noto ', self.dockerfile)

    def test_runtime_is_still_nonroot_and_proxy_configured(self):
        self.assertIn('USER 1000:1000', self.dockerfile)
        self.assertIn('HTTP_PROXY=http://127.0.0.1:18080', self.dockerfile)
        self.assertIn('HTTPS_PROXY=http://127.0.0.1:18080', self.dockerfile)

    def test_browser_identity_and_timezone_are_not_overridden(self):
        policies = json.loads((ROOT / 'runtime/firefox-policies.json').read_text(encoding='utf-8'))
        prefs = policies['policies']['Preferences']
        self.assertNotIn('general.useragent.override', prefs)
        self.assertNotIn('general.platform.override', prefs)
        self.assertNotIn('privacy.resistFingerprinting', prefs)
        self.assertFalse(prefs['media.peerconnection.enabled']['Value'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
