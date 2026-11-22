"""Linux packaging regression tests; Docker and the upstream revision are fixtures."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "linux", "management bundles are built on Linux")
class ManagementBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="vxnip-bundle-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "provider-source"
        shutil.copytree(Path(__file__).resolve().parents[1], self.repo,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.upstream = self.root / "nvke"
        self.upstream.mkdir()
        self.real_git = shutil.which("git")
        for name in ("deploy/fixture", "controller/deploy/fixture",
                     "controller/hack/ci/deploy-stack.sh",
                     "controller/hack/apply-capi-spiffe-exec-auth.sh",
                     "controller/test/fixtures/releases/fixture"):
            file = self.upstream / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("fixture\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "fixture")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("kind", "kubectl", "clusterctl", "helm", "nvkectl", "vxnip-controller"):
            self.executable(self.tools / name, "#!/bin/sh\nexit 0\n")
        self.fakebin = self.root / "fakebin"
        self.fakebin.mkdir()
        # Keep git archive real, but use the tiny fixture checkout for the pinned
        # upstream revision. No NVKE image build or installation happens here.
        self.executable(self.fakebin / "git", """#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]
if args[2:]==['rev-parse','HEAD']:
    print('2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049')
else:
    args=['HEAD' if a=='2f31480b360cbd7fc9d9db7a6ba274a8d0fd5049' else a for a in args]
    os.execv(os.environ['REAL_GIT'], [os.environ['REAL_GIT'], *args])
""")
        self.executable(self.fakebin / "docker", """#!/usr/bin/env python3
import hashlib,os,pathlib,sys
args=sys.argv[1:]
if args[:2]==['image','inspect']:
    if 'helper-sha256' in args[-1]:
        print(os.environ['HELPER_HASH'])
    elif 'kubernetes' in args[-1]:
        print('v1.36.3')
    else:
        print('sha256:'+hashlib.sha256(args[2].encode()).hexdigest())
elif args[:2]==['save','--output']:
    pathlib.Path(args[2]).write_bytes(b'fixture image archive')
else:
    sys.exit('unexpected Docker operation')
""")
        self.env = dict(os.environ, PATH=str(self.fakebin) + os.pathsep + os.environ["PATH"],
                        REAL_GIT=self.real_git,
                        HELPER_HASH=hashlib.sha256((self.repo / "scripts/vxnip-kubeadm").read_bytes()).hexdigest())
        self.env.pop("VXNIP_CONTROL_PLANE_IMAGE", None)
        self.output = self.root / "bundle"

    def git(self, *args):
        subprocess.run([self.real_git, "-C", str(self.upstream), *args],
                       check=True, capture_output=True, text=True)

    @staticmethod
    def executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def package(self, extra_image=False):
        env = dict(self.env)
        if extra_image:
            env["VXNIP_CONTROL_PLANE_IMAGE"] = "vxnip-control-plane:fixture"
        return subprocess.run(
            ["bash", str(self.repo / "scripts/package-nvke-management.sh"),
             str(self.upstream), str(self.tools), "kindest/node:fixture",
             "docker.local/cwe:fixture", "docker.local/cwe-profiles:fixture", str(self.output)],
            env=env, capture_output=True, text=True, timeout=40,
        )

    def check_runtime_dependencies(self, provider):
        # Derive runtime edges from the shipped scripts, not just the manifest.
        for script in (provider / "scripts").iterdir():
            for variable, relative in re.findall(
                    r'\$(repo|scripts|script_dir)/([A-Za-z0-9_./-]+)', script.read_text()):
                base = provider if variable == "repo" else provider / "scripts"
                self.assertTrue((base / relative).exists(), f"{script.name}: missing {relative}")
        init = (provider / "scripts/init-nvke-management.sh").read_text()
        names = re.search(r"for file in ([^\n]+); do", init).group(1).split()
        for name in names:
            self.assertTrue((provider / "scripts" / name).is_file())
            self.assertTrue(os.access(provider / "scripts" / name, os.X_OK))
        dockerfile = (provider / "examples/nvke-docker-control-plane.Dockerfile").read_text()
        for relative in re.findall(r"^COPY (?:--\S+ )?(\S+)", dockerfile, re.MULTILINE):
            self.assertTrue((provider / relative).is_file(), relative)

    def test_three_image_bundle_preserves_install_enrollment_and_recovery(self):
        # Untracked operator files must never leak into the distribution.
        (self.repo / "scripts/local-secret.env").write_text("must-not-ship\n")
        (self.repo / "config/local-secret.env").write_text("must-not-ship\n")
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"],
                       cwd=self.output, check=True, capture_output=True)
        provider = self.output / "provider"
        self.check_runtime_dependencies(provider)
        # Execute the actual packaged enrollment entry point, not its fixture.
        registry = self.root / "enrollment-registry"
        invite = subprocess.run(
            [sys.executable, str(provider / "scripts/nvke-enrollment-server.py"),
             "--state", str(registry), "invite"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        self.assertRegex(invite, r"^[a-f0-9]{64}$")
        invitation = registry / (hashlib.sha256(invite.encode()).hexdigest() + ".invite")
        self.assertTrue(invitation.is_file())
        for file in provider.rglob("*"):
            if file.is_file():
                original = self.repo / file.relative_to(provider)
                self.assertEqual(file.read_bytes(), original.read_bytes())
                self.assertEqual(file.stat().st_mode & 0o777, original.stat().st_mode & 0o777)
        excluded = ("scripts/local-secret.env", "config/local-secret.env",
                    "scripts/generate-crds.sh", "scripts/test_management_tools.py",
                    "scripts/test_vxnip_kubeadm.py", "scripts/test_management_bundle.py",
                    "scripts/build-nvke-control-plane.sh", "scripts/package-nvke-management.sh",
                    "internal/discovery/ssh.go", "hack/lab/create-nvke-lima-vm.py")
        for name in excluded:
            self.assertFalse((provider / name).exists(), name)
        self.assertEqual(len((self.output / "images").read_text().splitlines()), 3)
        self.assertFalse((self.output / "control-plane-image").exists())

    def test_four_image_bundle_keeps_verified_control_plane_image(self):
        result = self.package(extra_image=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(["sha256sum", "--quiet", "-c", "SHA256SUMS"],
                       cwd=self.output, check=True, capture_output=True)
        self.assertEqual((self.output / "control-plane-image").read_text(),
                         "vxnip-control-plane:fixture\n")
        self.assertEqual(len((self.output / "image-ids").read_text().splitlines()), 4)
        self.check_runtime_dependencies(self.output / "provider")

    def test_missing_runtime_dependency_does_not_publish_partial_bundle(self):
        (self.repo / "scripts/configure-nvke-lab-host-recovery.sh").unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob(".nvke-bundle.*")))
        self.assertIn("configure-nvke-lab-host-recovery.sh", result.stderr)


if __name__ == "__main__":
    unittest.main()
