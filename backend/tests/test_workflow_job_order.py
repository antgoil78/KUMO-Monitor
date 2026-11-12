import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestration_configuration as model


def dependency(workflow, job=None, parent=None, **rules):
    return dict(WORKFLOW_ID=workflow, JOB_ID=job, PARENT_ID=parent, TRIGGER_TYPE='DEP' if parent else None,
                SUCCESS=True, WARNING=None, ERROR=False, SQL='SELECT TRUE', ALLOW=True, **rules)


def catalog():
    data = dict(workflows=[dict(WORKFLOW_ID='w1', WORKFLOW_NAME='One'), dict(WORKFLOW_ID='w2', WORKFLOW_NAME='Two')],
                jobs=[dict(JOB_ID=job, JOB_NAME=job, IS_ENABLED=True) for job in ('j1', 'j2', 'j3')],
                raw_records=[dependency('w1'), dependency('w1', 'j3', 'j2'), dependency('w1', 'j1', 'w1'),
                             dependency('w1', 'j2', 'j1'), dependency('w2'), dependency('w2', 'j1', 'w2'),
                             dependency('sample', 'missing', 'sample')])
    data.update(model.normalize_dependencies(data))
    return data


class WorkflowOrderTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def test_order_follows_parent_chain_and_reused_jobs_have_scoped_identities(self):
        assignments = model.assignments(self.catalog)
        self.assertEqual([row['JOB_ID'] for row in assignments if row['WORKFLOW_ID'] == 'w1'], ['j1', 'j2', 'j3'])
        first, second = [row for row in assignments if row['JOB_ID'] == 'j1']
        self.assertNotEqual(first['ASSIGNMENT_ID'], second['ASSIGNMENT_ID'])
        rows = model.configuration_rows(self.catalog)
        self.assertEqual(next(row['parentId'] for row in rows if row['jobId'] == 'j2'), first['ASSIGNMENT_ID'])
        self.assertEqual(len(self.catalog['warnings']), 1)

    def test_reorder_changes_parents_and_preserves_rules_and_other_workflows(self):
        with patch.object(model.sf, 'execute_service') as execute:
            model.apply_orders(self.catalog, [{'workflowId': 'w1', 'jobIds': ['j3', 'j1', 'j2']}])
        writes = [call.args for call in execute.call_args_list]
        # j2 still follows j1, so only the first two changed parents are written.
        self.assertEqual(len(writes), 2)
        parents = {values['JOB_ID']: values['PARENT_ID'] for _, values in writes}
        self.assertEqual(parents, {'j3': 'w1', 'j1': 'j3'})
        for sql, values in writes:
            self.assertEqual(values['WORKFLOW_ID'], 'w1')
            self.assertEqual(values['SQL'], 'SELECT TRUE')
            self.assertIs(values['ERROR'], False)
            self.assertIsNone(values['WARNING'])
            self.assertIn('WORKFLOW_ID = %(WORKFLOW_ID)s AND JOB_ID = %(JOB_ID)s', sql)
            self.assertNotIn('WORKFLOW_DEPENDENCY', sql)
            self.assertNotIn('WORKFLOW_JOB', sql)

    def test_removing_first_job_reparents_next_job_without_deleting_job_definition(self):
        with patch.object(model.sf, 'execute_service') as execute:
            model.apply_orders(self.catalog, [{'workflowId': 'w1', 'jobIds': ['j2', 'j3']}])
        deletes = [call for call in execute.call_args_list if call.args[0].startswith('DELETE')]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0].args[1]['JOB_ID'], 'j1')
        update = next(call for call in execute.call_args_list if call.args[0].startswith('UPDATE'))
        self.assertEqual(update.args[1]['PARENT_ID'], 'w1')

    def test_batch_validation_rejects_unknown_jobs_duplicates_and_invalid_workflows_before_writes(self):
        for invalid in ([{'workflowId': 'w1', 'jobIds': ['missing']}],
                        [{'workflowId': 'w1', 'jobIds': ['j1', 'j1']}],
                        [{'workflowId': 'unknown', 'jobIds': []}],
                        [{'workflowId': 'w1', 'jobIds': ['j2']}, {'workflowId': 'w2', 'jobIds': ['missing']}],
                        [{'workflowId': 'w1', 'jobIds': []}, {'workflowId': 'w1', 'jobIds': []}]):
            with self.subTest(invalid=invalid), patch.object(model.sf, 'execute_service') as execute:
                with self.assertRaises(ValueError):
                    model.apply_orders(self.catalog, invalid)
                execute.assert_not_called()

    def test_duplicate_and_unknown_job_rows_are_preserved_and_block_edits_to_affected_workflow(self):
        for bad in (dependency('w1', 'j1', 'w1'), dependency('w1', 'missing', 'j1')):
            data = copy.deepcopy(self.catalog)
            data['raw_records'].append(bad)
            data.update(model.normalize_dependencies(data))
            self.assertIn('w1', data['invalidWorkflows'])
            with patch.object(model.sf, 'execute_service') as execute:
                with self.assertRaises(ValueError):
                    model.apply_orders(data, [{'workflowId': 'w1', 'jobIds': ['j1']}])
                execute.assert_not_called()

    def test_add_job_to_empty_workflow_uses_workflow_parent(self):
        with patch.object(model.sf, 'execute_service') as execute:
            model.apply_orders(self.catalog, [{'workflowId': 'w2', 'jobIds': ['j1', 'j2']}])
        insert = next(call for call in execute.call_args_list if call.args[0].startswith('INSERT'))
        self.assertEqual(insert.args[1]['JOB_ID'], 'j2')
        self.assertEqual(insert.args[1]['PARENT_ID'], 'j1')
        with patch.object(model.sf, 'execute_service') as execute:
            model.add_workflow('new', 'j3')
        values = [call.args[1] for call in execute.call_args_list]
        self.assertEqual(next(row['PARENT_ID'] for row in values if row['JOB_ID'] == 'j3'), 'new')

    def test_stale_revision_and_write_failure_rollback(self):
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            with self.assertRaises(model.ConfigurationConflict):
                model.save_orders([{'workflowId': 'w1', 'jobIds': []}], 'stale')
            self.assertEqual(execute.call_args_list[-1].args[0], 'ROLLBACK')
            self.assertFalse(any(call.args[0].startswith('DELETE') for call in execute.call_args_list))
        def fail(sql, params=None):
            if 'SET "PARENT_ID"' in sql:
                raise RuntimeError('write failed')
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service', side_effect=fail) as execute:
            with self.assertRaises(RuntimeError):
                model.save_orders([{'workflowId': 'w1', 'jobIds': ['j3', 'j1', 'j2']}], model.revision(self.catalog))
            self.assertEqual(execute.call_args_list[-1].args[0], 'ROLLBACK')
            self.assertFalse(any(call.args[0] == 'COMMIT' for call in execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
