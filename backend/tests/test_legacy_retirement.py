import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as application
import config
import kumo_repository as repo
import snowflake_client as sf


class LegacyRetirementTests(unittest.TestCase):
    def test_retired_dev_blocks_old_writes_and_get_fallback_before_repository_calls(self):
        client=application.app.test_client()
        with patch.object(config,'LEGACY_ENGINE_RETIRED',True),patch.object(application,'_renew_activity_lease'),patch.object(sf,'execute_service') as execute:
            for method,path in [('post','/api/workflows/old/run'),('get','/api/workflows/old/run-fallback'),('patch','/api/settings/engine'),('post','/api/dependencies/verify'),('patch','/api/workflows/old')]:
                response=getattr(client,method)(path,json={'enabled':True})
                self.assertEqual(response.status_code,410,(path,response.get_json()))
            execute.assert_not_called()

    def test_retired_dispatcher_cannot_be_offered_for_resume(self):
        with patch.object(config,'LEGACY_ENGINE_RETIRED',True),patch.object(repo,'_query') as query:
            engine=repo.get_engine_state(refresh=True)
        self.assertEqual(engine['status'],'RETIRED')
        self.assertTrue(engine['retired']);query.assert_not_called()

    def test_retirement_routes_archive_queue_only_in_selected_environment(self):
        old_flag,old_queue=config.LEGACY_ENGINE_RETIRED,config.T_QUEUE
        try:
            with patch.object(config,'METADATA_NAMESPACE','KUMO_DEV.MONITOR_META'):
                config.apply_orchestration_settings([dict(PARAMETER_KEY='LEGACY_ENGINE_RETIRED',PARAMETER_VALUE='true',VALUE_TYPE='BOOLEAN',ACTIVE_FL=True)])
                self.assertTrue(config.LEGACY_ENGINE_RETIRED)
                self.assertEqual(config.T_QUEUE,'KUMO_DEV.MONITOR_META.X_WORKFLOW_RUN_QUEUE')
            with patch.object(config,'METADATA_NAMESPACE','KUMO_TST.MONITOR_META'):
                config.apply_orchestration_settings([])
                self.assertFalse(config.LEGACY_ENGINE_RETIRED)
                self.assertEqual(config.T_QUEUE,'KUMO_TST.MONITOR_META.WORKFLOW_RUN_QUEUE')
        finally:config.LEGACY_ENGINE_RETIRED,config.T_QUEUE=old_flag,old_queue

    def test_retired_status_coordinator_never_starts_old_recovery_worker(self):
        coordinator=application.StatusCoordinator()
        with patch.object(config,'LEGACY_ENGINE_RETIRED',True),patch.object(application,'Thread') as thread:
            coordinator.set_client_count(1)
        thread.assert_not_called()


if __name__=='__main__':unittest.main()
