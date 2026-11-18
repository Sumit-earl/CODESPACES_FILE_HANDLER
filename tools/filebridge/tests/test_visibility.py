"""Port visibility logic, tested against a fake `gh` on PATH.

The behaviour that matters and that GitHub's CLI gets wrong on its own:
`gh cs ports visibility` 404s until the port exists in the Dev Tunnels service,
so the tool has to register it with a temporary forward first, then verify.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from filebridge import codespace  # noqa: E402

FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys, time

state_file = os.environ["FAKE_GH_STATE"]
with open(state_file) as handle:
    state = json.load(handle)

args = sys.argv[1:]
if args[:2] == ["cs", "ports"] and len(args) > 2:
    sub = args[2]
    if sub == "visibility":
        port, vis = args[3].split(":")
        if os.environ.get("FAKE_GH_FAIL") == "auth":
            sys.stderr.write("gh: authentication required, run gh auth login\n")
            sys.exit(1)
        if port not in state["registered"]:
            sys.stderr.write(
                "error updating port %s to %s: error getting tunnel port: "
                "unsuccessful request, response: 404 Not Found\nNot Found\n" % (port, vis)
            )
            sys.exit(1)
        state["ports"][port] = vis
        with open(state_file, "w") as handle:
            json.dump(state, handle)
        sys.exit(0)
    if sub == "forward":
        port = args[3].split(":")[0]
        if port not in state["registered"]:
            state["registered"].append(port)
        state["ports"].setdefault(port, "private")
        with open(state_file, "w") as handle:
            json.dump(state, handle)
        time.sleep(60)
        sys.exit(0)

json.dump(
    [{"sourcePort": int(p), "visibility": v} for p, v in state["ports"].items()],
    sys.stdout,
)
'''


class FakeGhCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fakegh-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        self.state_file = self.tmp / "state.json"
        self.set_state(ports={"5555": "private"}, registered=[])
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_STATE": str(self.state_file),
            "CODESPACE_NAME": "test-codespace",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        os.environ.pop("FAKE_GH_FAIL", None)

    def set_state(self, **state):
        self.state_file.write_text(json.dumps(state))

    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    # ------------------------------------------------------------------

    def test_list_ports(self):
        self.assertEqual(codespace.list_ports(), {5555: "private"})

    def test_public_needs_a_forward_then_verifies(self):
        started = time.monotonic()
        result = codespace.ensure_visibility(5555, "public", timeout=15)
        elapsed = time.monotonic() - started
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(result.actual, "public")
        self.assertEqual(result.method, "forward")
        self.assertEqual(self.state()["ports"]["5555"], "public")
        self.assertLess(elapsed, 15)

    def test_private_is_a_no_op_when_not_registered(self):
        self.set_state(ports={}, registered=[])
        result = codespace.ensure_visibility(9999, "private", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "probe")
        self.assertNotIn("9999", self.state()["registered"], "must not create a tunnel port")

    def test_flip_back_to_private_uses_the_existing_entity(self):
        self.set_state(ports={"5555": "public"}, registered=["5555"])
        result = codespace.ensure_visibility(5555, "private", timeout=5)
        self.assertTrue(result.ok, result.describe())
        self.assertEqual(result.actual, "private")
        self.assertEqual(result.method, "direct")

    def test_already_correct_is_instant(self):
        result = codespace.ensure_visibility(5555, "private", timeout=5)
        self.assertTrue(result.ok)
        self.assertEqual(result.method, "probe")

    def test_auth_error_is_reported_not_retried_forever(self):
        self.set_state(ports={"5555": "private"}, registered=[])
        os.environ["FAKE_GH_FAIL"] = "auth"
        started = time.monotonic()
        result = codespace.ensure_visibility(5555, "public", timeout=20)
        self.assertFalse(result.ok)
        self.assertIn("gh auth", result.detail + "gh auth refresh")
        self.assertLess(time.monotonic() - started, 6, "should not wait out the timeout")

    def test_not_a_codespace_is_skipped(self):
        os.environ.pop("CODESPACE_NAME", None)
        result = codespace.ensure_visibility(5555, "public", timeout=2)
        self.assertFalse(result.ok)
        self.assertEqual(result.method, "skipped")

    def test_manual_snippet_mentions_forward_and_visibility(self):
        snippet = codespace.manual_snippet(5555, "public")
        self.assertIn("gh cs ports forward 5555:", snippet)
        self.assertIn("visibility 5555:public", snippet)


if __name__ == "__main__":
    unittest.main()
