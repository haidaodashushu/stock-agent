import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data.adapters import iwencai_credentials
from data.adapters.iwencai_credentials import IwenCaiKeyring


class IwenCaiKeyringTests(unittest.TestCase):
    def test_candidates_prefer_profile_then_keyring(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            keyring = tmp / "iwencai_keys"
            zshrc = tmp / ".zshrc"
            keyring.write_text(
                "\n".join(
                    [
                        "ACTIVE=current",
                        "IWENCAI_API_KEY_CURRENT=current-key",
                        "IWENCAI_API_KEY_FALLBACK_OLD=old-key",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            zshrc.write_text("export IWENCAI_API_KEY=profile-key\n", encoding="utf-8")

            with patch.object(iwencai_credentials, "KEYRING_PATH", keyring), \
                 patch.object(Path, "home", return_value=tmp), \
                 patch.dict("os.environ", {}, clear=True):
                self.assertEqual(
                    IwenCaiKeyring.candidates(),
                    [
                        ("active", "profile-key"),
                        ("IWENCAI_API_KEY_CURRENT", "current-key"),
                        ("IWENCAI_API_KEY_FALLBACK_OLD", "old-key"),
                    ],
                )

    def test_promote_swaps_current_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            keyring = tmp / "iwencai_keys"
            zshrc = tmp / ".zshrc"
            keyring.write_text(
                "\n".join(
                    [
                        "ACTIVE=current",
                        "IWENCAI_API_KEY_CURRENT=key-a",
                        "IWENCAI_API_KEY_FALLBACK_OLD=key-b",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            zshrc.write_text("export IWENCAI_API_KEY=key-a\n", encoding="utf-8")

            with patch.object(iwencai_credentials, "KEYRING_PATH", keyring), \
                 patch.object(iwencai_credentials, "ZSHRC_PATH", zshrc):
                IwenCaiKeyring.promote("key-b")

            keyring_text = keyring.read_text(encoding="utf-8")
            self.assertIn("IWENCAI_API_KEY_CURRENT=key-b", keyring_text)
            self.assertIn("IWENCAI_API_KEY_FALLBACK_OLD=key-a", keyring_text)
            self.assertEqual(zshrc.read_text(encoding="utf-8").strip(), "export IWENCAI_API_KEY=key-b")


if __name__ == "__main__":
    unittest.main()
