import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("verify_go_tests", Path(__file__).resolve().parents[1] / "scripts/verify-go-tests.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class IntegrationResultTests(unittest.TestCase):
    def reports(self, action="pass", package_override=None):
        return [json.dumps({"Package": package_override or package, "Test": name, "Action": action})
                for package, name in verify.REQUIRED]

    def test_all_required_tests_pass(self):
        verify.verify(self.reports())

    def test_skip_is_not_success(self):
        with self.assertRaises(ValueError):
            verify.verify(self.reports("skip"))

    def test_missing_test_is_not_success(self):
        with self.assertRaises(ValueError):
            verify.verify(self.reports()[:-1])

    def test_failed_test_is_not_success(self):
        with self.assertRaises(ValueError):
            verify.verify(self.reports("fail"))

    def test_test_in_another_package_cannot_substitute(self):
        with self.assertRaises(ValueError):
            verify.verify(self.reports(package_override="another/package"))

    def test_invalid_report_is_not_success(self):
        with self.assertRaises(ValueError):
            verify.verify(["not JSON"])


if __name__ == "__main__":
    unittest.main()
