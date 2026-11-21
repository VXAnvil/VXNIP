import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("ci_policy", Path(__file__).resolve().parents[1] / "scripts/ci-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class FakeAPI:
    def __init__(self, runs, jobs):
        self.run_list = runs
        self.job_list = jobs

    def runs(self, repository, commit):
        return self.run_list

    def jobs(self, repository, run):
        return self.job_list


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.repository = "example/vxanvil"
        self.commit = "a" * 40
        self.run = {
            "id": 10, "run_attempt": 1, "head_sha": self.commit,
            "head_repository": {"full_name": self.repository}, "head_branch": "main",
            "event": "push", "status": "completed", "conclusion": "success",
            "html_url": "https://github.com/example/vxanvil/actions/runs/10",
        }
        self.jobs = [{"id": i, "name": name, "run_attempt": 1,
                      "status": "completed", "conclusion": "success"}
                     for i, name in enumerate(("lint", "test", "build", "gate"), 1)]

    def check(self, runs=None, jobs=None):
        return policy.wait_for_release(FakeAPI([self.run] if runs is None else runs,
                                              self.jobs if jobs is None else jobs),
                                       self.repository, self.commit, timeout=0)

    def test_gate_requires_all_three_successes(self):
        self.assertEqual(len(policy.require_gate({name: {"result": "success"} for name in policy.CI_JOBS})), 3)

    def test_gate_rejects_failure_cancellation_skip_and_missing_job(self):
        for result in ("failure", "cancelled", "skipped", None):
            with self.subTest(result=result), self.assertRaises(policy.PolicyError):
                policy.require_gate({name: {"result": result if name == "test" else "success"} for name in policy.CI_JOBS})
        with self.assertRaises(policy.PolicyError):
            policy.require_gate({"test": {"result": "success"}})

    def test_exact_successful_main_commit_is_approved(self):
        self.assertEqual(self.check()["id"], 10)

    def test_successful_manual_run_is_approved(self):
        self.run.update(event="workflow_dispatch", head_branch="feature")
        self.assertEqual(self.check()["id"], 10)

    def test_another_commit_cannot_approve_release(self):
        self.run["head_sha"] = "b" * 40
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_fork_repository_cannot_approve_release(self):
        self.run["head_repository"]["full_name"] = "fork/vxanvil"
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_pull_request_run_cannot_approve_release(self):
        self.run["event"] = "pull_request"
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_non_main_push_cannot_approve_release(self):
        self.run["head_branch"] = "feature"
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_latest_failed_run_overrides_older_success(self):
        failed = dict(self.run, id=11, conclusion="failure")
        with self.assertRaisesRegex(policy.PolicyError, "11 concluded failure"):
            self.check(runs=[self.run, failed])

    def test_missing_ci_is_not_success(self):
        with self.assertRaises(policy.PolicyError):
            self.check(runs=[])

    def test_failed_cancelled_and_skipped_runs_are_denied(self):
        for result in ("failure", "cancelled", "skipped", "timed_out"):
            with self.subTest(result=result), self.assertRaises(policy.PolicyError):
                self.check(runs=[dict(self.run, conclusion=result)])

    def test_overall_success_without_gate_is_denied(self):
        with self.assertRaisesRegex(policy.PolicyError, "gate"):
            self.check(jobs=[job for job in self.jobs if job["name"] != "gate"])

    def test_overall_success_with_skipped_job_is_denied(self):
        self.jobs[0]["conclusion"] = "skipped"
        with self.assertRaises(policy.PolicyError):
            self.check()

    def test_latest_job_attempt_must_pass(self):
        failed = dict(self.jobs[0], id=20, run_attempt=2, conclusion="failure")
        with self.assertRaises(policy.PolicyError):
            self.check(jobs=self.jobs + [failed])
        retried = dict(failed, id=21, run_attempt=3, conclusion="success")
        self.assertEqual(self.check(jobs=self.jobs + [failed, retried])["id"], 10)

    def test_pending_run_can_complete_within_timeout(self):
        api = FakeAPI([dict(self.run, status="in_progress", conclusion=None)], self.jobs)
        elapsed = [0]

        def sleep(seconds):
            elapsed[0] += seconds
            api.run_list = [copy.deepcopy(self.run)]

        result = policy.wait_for_release(api, self.repository, self.commit, timeout=3,
                                         interval=1, clock=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(result["id"], 10)


if __name__ == "__main__":
    unittest.main()
