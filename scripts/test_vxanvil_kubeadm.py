import json
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class PreflightTests(unittest.TestCase):
    def test_only_empty_package_placeholders_are_ignored(self):
        helper = runpy.run_path(str(Path(__file__).with_name("vxanvil-kubeadm")))
        has_files = helper["has_files"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / ".kubelet-keep"
            with patch.dict(has_files.__globals__, PACKAGE_MARKERS={marker}):
                marker.touch()
                self.assertFalse(has_files(root))
                marker.write_text("not a package placeholder")
                self.assertTrue(has_files(root))
                marker.unlink()
                marker.symlink_to(root / "missing")
                self.assertTrue(has_files(root))
                marker.unlink()
                (root / "kubelet.conf").touch()
                self.assertTrue(has_files(root))

    def test_nvke_reset_removes_generated_files_and_preserves_unknown_files(self):
        helper = runpy.run_path(str(Path(__file__).with_name("vxanvil-kubeadm")))
        reset_kubernetes = helper["reset_kubernetes"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oidc = root / "oidc"
            oidc.mkdir()
            auth_config = oidc / "auth-config.yaml"
            auth_config.touch()
            unknown = oidc / "local-config.yaml"
            unknown.touch()
            with patch.dict(reset_kubernetes.__globals__, {
                "PKI": root / "pki", "ETCD": root / "etcd", "CNI": root / "cni",
                "kubeadm": lambda *args: None, "NVKE_WORKER_RUNTIME_UNIT": root / "no-unit",
            }):
                with self.assertRaises(helper["HelperError"]):
                    reset_kubernetes("nvke")
            self.assertFalse(auth_config.exists())
            self.assertTrue(unknown.is_file())

    def test_nvke_reset_uses_the_independent_cri_and_refuses_to_fall_back(self):
        helper = runpy.run_path(str(Path(__file__).with_name("vxanvil-kubeadm")))
        reset_kubernetes = helper["reset_kubernetes"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unit = root / "worker.service"
            endpoint = root / "cri.sock"
            operations = []
            with patch.dict(reset_kubernetes.__globals__, {
                "PKI": root / "kubernetes" / "pki", "ETCD": root / "etcd", "CNI": root / "cni",
                "CONFIG": root / "config", "LOG": root / "log",
                "NVKE_WORKER_RUNTIME_UNIT": unit, "NVKE_WORKER_RUNTIME_SOCKET": endpoint,
                "kubeadm": lambda *args: operations.append(args),
            }):
                reset_kubernetes("nvke")
                unit.touch()
                with self.assertRaises(helper["HelperError"]):
                    reset_kubernetes("nvke")
                with patch.object(Path, "is_socket", return_value=True):
                    reset_kubernetes("nvke")
            self.assertEqual(operations, [
                ("reset", "--force"),
                ("reset", "--force", "--cri-socket", f"unix://{endpoint}"),
            ])

    def test_nvke_plan_sets_identity_and_releases_owned_files(self):
        helper = runpy.run_path(str(Path(__file__).with_name("vxanvil-kubeadm")))
        apply, reset = helper["apply_nvke"], helper["reset"]
        uid = "fbeceea4-23c8-45cf-9972-c377d2f2a9b0"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "nke-agent"
            kubelet_env = root / "kubelet.env"
            dropin = root / "kubelet.conf"
            state = root / "current.json"
            recovery_service = root / "recovery.service"
            recovery_timer = root / "recovery.timer"
            spire = root / "spire"
            operations = []
            with patch.dict(apply.__globals__, {
                "ROOT": root, "STATE": state, "KUBELET_ENV": kubelet_env,
                "KUBELET_DROPIN": dropin, "SPIRE_STATE": root / "spire-state",
                "SPIRE_CONFIG": spire / "spire.conf", "SPIRE_BUNDLE": spire / "bundle.pem",
                "NVKE_STATE": root / "agent-state", "NVKE_TEMP": (),
                "RECOVERY_SERVICE": recovery_service, "RECOVERY_TIMER": recovery_timer,
                "NVKE_WORKER_RUNTIME_UNIT": root / "vmetal-nvke-containerd.service",
                "AGENT_ENV": {"nke-agent.service": env},
                "AGENT_CA": {"nke-agent.service": root / "controller-ca.crt"},
                "systemctl": lambda *args: operations.append(args),
                "assert_clean_node": lambda: None,
                "reset_kubernetes": lambda mode: operations.append(("reset",)),
                "pwd": SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_gid=123)),
                "os": SimpleNamespace(chown=lambda *args: None),
            }):
                plan = {
                    "unit": "nke-agent.service", "providerID": f"vmetal://{uid}", "nodeIP": "10.0.0.54",
                    "files": [{
                        "path": str(env), "mode": "0600",
                        "content": 'AGENT_ARGS="--secure-server-port 15005"\n',
                    }, {
                        "path": str(spire / "spire.conf"), "mode": "0640",
                        "content": (
                            'agent { data_dir = "/var/lib/spire-agent" log_level = "DEBUG" '
                            'server_address = "192.0.2.10" server_port = "30441" trust_domain = "test.example" '
                            'trust_bundle_path = "/etc/spire-agent/initial-trust-bundle.pem" join_token = "fake-test" } '
                            'plugins { KeyManager "disk" { plugin_data { directory = "/var/lib/spire-agent" } } '
                            'NodeAttestor "join_token" { plugin_data {} } WorkloadAttestor "unix" { plugin_data {} } }'
                        ),
                    }, {
                        "path": str(spire / "bundle.pem"), "mode": "0644",
                        "content": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
                    }],
                }
                previous_umask = os.umask(0o077)
                try:
                    apply(uid, None, plan)
                finally:
                    os.umask(previous_umask)
                self.assertEqual(spire.stat().st_mode & 0o777, 0o750)
                self.assertIn(f"--provider-id=vmetal://{uid}", kubelet_env.read_text())
                self.assertIn("--node-ip=10.0.0.54", kubelet_env.read_text())
                self.assertIn(str(kubelet_env), dropin.read_text())
                current = json.loads(state.read_text())
                self.assertIn("OnBootSec=10s", recovery_timer.read_text())
                self.assertEqual([line for line in recovery_service.read_text().splitlines()
                                  if line.startswith("ExecStart=")], [
                    "ExecStart=/usr/bin/systemctl start containerd.service",
                    "ExecStart=/usr/bin/systemctl start spire-agent.service",
                    "ExecStart=/usr/bin/systemctl start nke-agent.service",
                    "ExecStart=/bin/sh -c 'if [ -s /etc/kubernetes/kubelet.conf ] && "
                    "[ -s /var/lib/kubelet/config.yaml ]; then exec /usr/bin/systemctl start kubelet.service; fi'",
                ])
                recovery_timer.unlink()
                apply(uid, current, plan)
                self.assertTrue(recovery_timer.is_file())
                self.assertEqual(operations, [
                    ("daemon-reload",), ("restart", "spire-agent.service"), ("restart", "nke-agent.service"),
                    ("daemon-reload",), ("enable", "recovery.timer"),
                    ("daemon-reload",), ("enable", "recovery.timer"),
                ])
                reset(uid, current, "nvke")
                self.assertEqual(operations[7:], [
                    ("disable", "--now", "recovery.timer"), ("stop", "recovery.service"),
                    ("stop", "nke-agent.service"), ("stop", "spire-agent.service"), ("reset",), ("daemon-reload",),
                ])
                for path in (env, kubelet_env, dropin, state, recovery_service, recovery_timer):
                    self.assertFalse(path.exists())
                self.assertTrue((root / ("retired-" + uid)).exists())


if __name__ == "__main__":
    unittest.main()
