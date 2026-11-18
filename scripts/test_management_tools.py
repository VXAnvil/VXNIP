import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tool = load("migrate-vmetal-hosts")
        self.host = {
            "metadata": {"name": "host-old", "uid": "old-uid", "resourceVersion": "9"},
            "spec": {"machineID": "a" * 64, "sshTarget": "ubuntu@worker", "allowBootstrap": True},
        }
        self.entry = {"oldName": "host-old", "oldUID": "old-uid", "oldSpec": self.host["spec"],
                      "newName": "host-" + "b" * 32,
                      "newSpec": dict(self.host["spec"], machineID="b" * 64),
                      "deleted": False, "created": False}
        self.plan = {"version": 1, "sourceUID": "source", "targetUID": "target",
                     "hosts": [copy.deepcopy(self.entry)]}
        self.old = copy.deepcopy(self.host)
        self.new = None
        self.fail_create = False
        self.deletes = 0

    def kube(self, config, *args, body=None):
        if args[:2] == ("get", self.tool.OLD_HOSTS):
            return self.old or ""
        if args[:2] == ("get", self.tool.NEW_HOSTS):
            return self.new or ""
        if args[0] == "delete":
            self.assertEqual(body["preconditions"], {"uid": "old-uid", "resourceVersion": "9"})
            self.deletes += 1
            self.old = None
            return {}
        if args[0] == "wait":
            return ""
        if args[0] == "create":
            if self.fail_create:
                raise OSError("interrupted API write")
            self.new = body
            return body
        self.fail(f"unexpected kubectl call {args}")

    def apply(self, path, plan):
        with (
            patch.object(self.tool, "kubectl", self.kube),
            patch.object(self.tool, "management_uid", lambda config: config),
            patch.object(self.tool, "require_released"),
            patch.object(self.tool, "probe", return_value="b" * 64),
        ):
            self.tool.apply_migration("source", "target", path, plan)

    def test_interrupted_create_resumes_without_duplicate_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            self.fail_create = True
            with self.assertRaises(OSError):
                self.apply(path, self.plan)
            saved = json.loads(path.read_text())
            self.assertTrue(saved["hosts"][0]["deleted"])
            self.assertFalse(saved["hosts"][0]["created"])
            self.fail_create = False
            self.apply(path, saved)
            self.apply(path, json.loads(path.read_text()))
            self.assertEqual(self.deletes, 1)
            self.assertEqual(self.new["spec"]["machineID"], "b" * 64)

    def test_new_claim_prevents_source_delete(self):
        self.old["status"] = {"claim": {"uid": "allocation"}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "claimed"):
                self.apply(Path(directory) / "plan.json", self.plan)
        self.assertEqual(self.deletes, 0)

    def test_changed_mapping_is_rejected_before_source_delete(self):
        self.plan["hosts"][0]["newSpec"]["sshTarget"] = "intruder@other"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "mapping"):
                self.apply(Path(directory) / "plan.json", self.plan)
        self.assertEqual(self.deletes, 0)


class NetworkTests(unittest.TestCase):
    def test_tailscale_route_drives_mtu(self):
        network = load("vmetal-network")
        responses = [
            b'[{"dev":"tailscale0","prefsrc":"100.100.0.1"}]',
            b'[{"mtu":1280}]',
        ]
        with patch.object(network.subprocess, "check_output", side_effect=responses):
            result = network.path_mtu("100.100.0.2")
        self.assertEqual(result["podMTU"], 1180)
        self.assertEqual(result["source"], "100.100.0.1")


if __name__ == "__main__":
    unittest.main()
