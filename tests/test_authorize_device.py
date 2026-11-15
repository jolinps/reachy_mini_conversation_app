"""Local checks for the device grant command. No request is sent."""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "authorize_device.py"


def _load():
    spec = importlib.util.spec_from_file_location("authorize_device", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuthorizeDeviceTests(unittest.TestCase):
    def setUp(self):
        self.module = _load()

    def test_verified_profile_named_default_uses_the_listener_without_a_profile_path(self):
        values = self._values("default", "private_endpoint", "http://10.1.2.3:8642", "http://10.1.2.3:8642")
        self.assertEqual(values["observed_profile"], "default")
        self.assertEqual(values["route_path"], "/")
        self.assertNotIn("/p/default", values["private_endpoint"])

    def test_missing_route_evidence_does_not_invent_a_profile_path(self):
        with self.assertRaises(ValueError) as caught:
            self._values("default", "profile_prefix", "http://10.1.2.3:8642", "")
        self.assertIn("not inferred", str(caught.exception))
        self.assertNotIn("/p/default", str(caught.exception))

    def test_profile_prefix_requires_the_observed_path(self):
        values = self._values(
            "classroom",
            "profile_prefix",
            "http://10.1.2.3:8642",
            "http://10.1.2.3:8642/p/classroom",
        )
        self.assertEqual(values["route_path"], "/p/classroom")
        with self.assertRaises(ValueError):
            self._values("default", "profile_prefix", "http://10.1.2.3:8642", "http://10.1.2.3:8642")

    def test_a_path_is_not_accepted_as_the_profile_name(self):
        with self.assertRaises(ValueError):
            self._values("/p/default", "private_endpoint", "http://10.1.2.3:8642", "http://10.1.2.3:8642")

    def test_dry_run_prints_no_listener_or_secret(self):
        env = os.environ.copy()
        env.update(self._env("default", "private_endpoint", "http://10.1.2.3:8642", "http://10.1.2.3:8642"))
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_profile: default", completed.stdout)
        self.assertIn("observed_route_path: /", completed.stdout)
        self.assertIn("profile path inferred: no", completed.stdout)
        self.assertNotIn("10.1.2.3", completed.stdout)
        self.assertNotIn("local-secret", completed.stdout)
        self.assertNotIn("local-admin", completed.stdout)
        self.assertIn("dry run: no request was sent", completed.stdout)

    def _values(self, profile, routing, endpoint, evidence):
        previous = os.environ.copy()
        os.environ.update(self._env(profile, routing, endpoint, evidence))
        try:
            return self.module._values()
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def _env(self, profile, routing, endpoint, evidence):
        return {
            "GATEWAY_ADMIN_TOKEN": "local-admin",
            "HERMES_AGENT_ID": "inspected-agent",
            "HERMES_ROUTING": routing,
            "HERMES_PRIVATE_ENDPOINT": endpoint,
            "HERMES_ROUTE_EVIDENCE": evidence,
            "HERMES_API_KEY": "local-secret",
            "OBSERVED_PROFILE": profile,
            "OBSERVED_INSTANCE": "inspected-process",
            "INSPECTION_REF": "host-note",
        }


if __name__ == "__main__":
    unittest.main()
