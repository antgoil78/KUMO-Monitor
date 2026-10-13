import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import kumo_repository as repo


class FailedTaskRecoveryTests(unittest.TestCase):
    def setUp(self):
        repo._last_run_lock_cleanup_monotonic = 0
        self.addCleanup(setattr, repo, "_last_run_lock_cleanup_monotonic", 0)

    def test_timeout_after_weekend_repairs_all_statuses_without_run_locks(self):
        failure = {
            "RUN_ID": "timed-out-run",
            "WORKFLOW_ID": "post-workflow",
            "END_TIME": "2026-10-02T20:17:15.622000",
            "ERROR_MESSAGE": "Statement timed out after 21,600 seconds",
        }
        with patch.object(repo, "_query", return_value=[failure]) as query, \
             patch.object(repo, "_execute") as execute, \
             patch.object(repo, "object_exists", return_value=True), \
             patch.object(repo, "_run_lock_table_available", return_value=False):
            repo.load_active_run_locks()

        # Recovery must include failures older than the previous 24-hour window,
        # and filter failures before Snowflake applies its result limit.
        lookup = query.call_args.args[0]
        self.assertIn("DATEADD('day', -7", lookup)
        self.assertIn("ERROR_ONLY => TRUE", lookup)
        self.assertIn("SCHEDULED_TIME_RANGE_END => CURRENT_TIMESTAMP()", lookup)
        self.assertEqual(execute.call_count, 3)
        history, queue, status = execute.call_args_list
        self.assertIn(config.T_HISTORY, history.args[0])
        self.assertEqual(history.args[1]["end_time"], failure["END_TIME"])
        self.assertEqual(history.args[1]["error_message"], failure["ERROR_MESSAGE"])
        self.assertIn(config.T_QUEUE, queue.args[0])
        self.assertIn("IN (", queue.args[0])
        self.assertIn(config.WORKFLOW_STATUS_TABLE, status.args[0])
        self.assertEqual(status.args[1]["run_id"], failure["RUN_ID"])
        self.assertIn("RUN_ID = %(run_id)s", status.args[0])
        self.assertIn("= 'PROCESSING'", status.args[0])

    def test_no_authoritative_failure_does_not_change_workflows(self):
        with patch.object(repo, "_query", return_value=[]), \
             patch.object(repo, "_execute") as execute, \
             patch.object(repo, "_run_lock_table_available", return_value=False):
            repo.cleanup_workflow_run_locks()
        execute.assert_not_called()

    def test_unavailable_task_history_does_not_prevent_lock_cleanup(self):
        with patch.object(repo, "_query", side_effect=RuntimeError("No task history access")), \
             patch.object(repo, "_execute") as execute, \
             patch.object(repo, "_run_lock_table_available", return_value=True):
            repo.cleanup_workflow_run_locks()
        self.assertEqual(execute.call_count, 2)
        self.assertTrue(all(config.T_APP_WORKFLOW_RUN_LOCKS in c.args[0]
                            for c in execute.call_args_list))

    def test_optional_workflow_status_table_does_not_block_history_recovery(self):
        with patch.object(repo, "_query", return_value=[{"RUN_ID": "failed-run"}]), \
             patch.object(repo, "_execute") as execute, \
             patch.object(repo, "object_exists", return_value=False), \
             patch.object(repo, "_run_lock_table_available", return_value=False):
            repo.cleanup_workflow_run_locks()
        self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
