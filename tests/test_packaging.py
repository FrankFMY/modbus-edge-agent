import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTest(unittest.TestCase):
    def test_example_config_is_synthetic_and_cli_validates_it(self):
        config_path = ROOT / "examples" / "config.example.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["version"], "modbus-edge-agent.config.v1")
        self.assertIs(config["gateway"]["synthetic_source"], True)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "modbus_edge_agent", "--config", str(config_path), "--validate-config"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["version"], "modbus-edge-agent.config.v1")

    def test_systemd_service_is_hardened_and_never_enables_dry_run(self):
        service = (ROOT / "packaging" / "modbus-edge-agent.service").read_text(encoding="utf-8")
        self.assertIn("User=modbus-edge", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=/var/lib/modbus-edge-agent", service)
        self.assertNotIn("--dry-run", service)
        self.assertNotIn("--no-mqtt", service)

    def test_installer_preserves_config_validates_as_service_user_and_does_not_auto_enable(self):
        installer = (ROOT / "packaging" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("PRESERVE_CONFIG=1", installer)
        self.assertIn("runuser -u modbus-edge --", installer)
        self.assertIn("--validate-config", installer)
        self.assertNotIn("enable --now", installer)
        self.assertIn("systemctl restart modbus-edge-agent", installer)


if __name__ == "__main__":
    unittest.main()
