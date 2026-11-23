import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import orchestration_configuration as model


def record(record_id, workflow, job=None, parent_workflow=None, parent_job=None, sequence=None):
    return dict(WORKFLOW_DEPENDENCY_ID=record_id, CHILD_WORKFLOW_ID=workflow,
                JOB_ID=job, PARENT_WORKFLOW_ID=parent_workflow, PARENT_JOB_ID=parent_job,
                JOB_SEQUENCE=sequence, TRIGGER_TYPE='DEP' if job or parent_workflow else None,
                SUCCESS=None, WARNING=None, ERROR=None, SQL_CONDITION=None, ALLOW=True,
                IS_ENABLED=True, CONFIG_VERSION=1, MIN_STATUS_LEVEL='SUCCESS',
                DEPENDENCY_TYPE='SEQUENCE' if job else 'TRIGGER')


def catalog():
    return dict(workflows=[dict(WORKFLOW_ID='w1', WORKFLOW_NAME='WF1'), dict(WORKFLOW_ID='w2', WORKFLOW_NAME='WF2')],
                jobs=[dict(JOB_ID='j1', JOB_NAME='JOB1', IS_ENABLED=True), dict(JOB_ID='j2', JOB_NAME='JOB2', IS_ENABLED=True)],
                records=[record('h1', 'w1'), record('a1', 'w1', 'j1', 'w1', sequence=1),
                         record('h2', 'w2', parent_workflow='w1'), record('a2', 'w2', 'j1', 'w2', sequence=1),
                         record('b2', 'w2', 'j2', parent_job='j1', sequence=2)])


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()
        self.rows = model.configuration_rows(self.catalog)

    def test_shared_jobs_have_distinct_assignment_and_parent_identities(self):
        self.assertEqual([row['id'] for row in self.rows], ['w1', 'a1', 'w2', 'a2', 'b2'])
        self.assertEqual(self.rows[-1]['parentId'], 'a2')
        self.assertEqual(len(model.validate_rows(self.rows, self.catalog)), 5)

    def test_unknown_definition_duplicate_assignment_cross_workflow_parent_and_cycle_rejected(self):
        for mutate in (
            lambda rows: rows[-1].update(jobId='unknown'),
            lambda rows: rows[-1].update(jobId='j1'),
            lambda rows: rows[-1].update(parentId='a1'),
            lambda rows: rows[1].update(parentId='a1'),
            lambda rows: rows[-1].update(parentId='w2'),
            lambda rows: rows[0].update(parentId='b2'),
        ):
            with self.subTest(mutate=mutate):
                rows = copy.deepcopy(self.rows)
                mutate(rows)
                with self.assertRaises(ValueError):
                    model.validate_rows(rows, self.catalog)

    def test_boolean_rules_are_strict_and_false_is_not_coerced_to_null(self):
        rows = copy.deepcopy(self.rows)
        rows[-1].update(success=True, warning=None, error=False, allow=False)
        validated = model.validate_rows(rows, self.catalog)
        self.assertIs(validated[-1]['error'], False)
        for field in ('success', 'warning', 'error', 'allow', 'parentMustBeNewer'):
            bad = copy.deepcopy(rows)
            bad[-1][field] = 'false'
            with self.assertRaises(ValueError):
                model.validate_rows(bad, self.catalog)

    def test_multiple_parent_rules_keep_independent_freshness_and_targeted_writes(self):
        self.catalog['workflows'].append(dict(WORKFLOW_ID='w3', WORKFLOW_NAME='WF3'))
        self.catalog['records'].extend([record('h3', 'w3'), record('extra', 'w2', parent_workflow='w3')])
        rows = model.configuration_rows(self.catalog)
        links = [row for row in rows if row['workflowId'] == 'w2' and not row['jobId']]
        self.assertEqual([row['parentId'] for row in links], ['w1', 'w3'])
        links[1]['parentMustBeNewer'] = True
        model.validate_rows(rows, self.catalog)
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            model.save(rows, model.revision(self.catalog))
        writes = [call for call in execute.call_args_list if call.args[0].startswith('UPDATE') and 'SET "PARENT_ID"' in call.args[0]]
        self.assertEqual(len(writes), 1)
        self.assertIn('AND PARENT_ID = %(PARENT_ID)s', writes[0].args[0])
        self.assertEqual(writes[0].args[1]['PARENT_ID'], 'w3')
        self.assertTrue(writes[0].args[1]['PARENT_MUST_BE_NEWER'])
        links[1]['parentId'] = 'w1'
        with self.assertRaisesRegex(ValueError, 'unique dependency'):
            model.validate_rows(rows, self.catalog)
        links[1]['parentId'] = 'w3'
        next(row for row in rows if row['id'] == 'w3').update(parentId='w2', trigger='DEP')
        with self.assertRaisesRegex(ValueError, 'cycles'):
            model.validate_rows(rows, self.catalog)

    def test_native_multiple_parents_are_valid_but_duplicate_edges_are_not(self):
        source = dict(workflows=self.catalog['workflows'], jobs=self.catalog['jobs'], raw_records=[
            dict(WORKFLOW_ID='w2', JOB_ID=None, PARENT_ID='w1', PARENT_MUST_BE_NEWER=True),
            dict(WORKFLOW_ID='w2', JOB_ID=None, PARENT_ID='w2', PARENT_MUST_BE_NEWER=False)])
        normalized = model.normalize_dependencies(source)
        self.assertEqual(normalized['invalidWorkflows'], [])
        self.assertEqual(len(normalized['records']), 2)
        self.assertTrue(normalized['records'][0]['PARENT_MUST_BE_NEWER'])
        source['raw_records'].append(source['raw_records'][0])
        self.assertEqual(model.normalize_dependencies(source)['invalidWorkflows'], ['w2'])

    def test_additional_parent_can_be_created_with_a_new_uuid_and_removed(self):
        self.catalog['workflows'].append(dict(WORKFLOW_ID='w3', WORKFLOW_NAME='WF3'))
        self.catalog['records'].append(record('h3', 'w3'))
        rows = model.configuration_rows(self.catalog)
        extra = dict(next(row for row in rows if row['id'] == 'w2'),
                     id=str(model.uuid.uuid4()), parentId='w3', success=True,
                     warning=None, error=False, sql='SELECT TRUE', parentMustBeNewer=True)
        rows.append(extra)
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            model.save(rows, model.revision(self.catalog))
        writes = [call for call in execute.call_args_list if call.args[0].startswith(('INSERT', 'DELETE'))]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].args[0].startswith('INSERT'))
        self.assertEqual(writes[0].args[1]['WORKFLOW_ID'], 'w2')
        self.assertEqual(writes[0].args[1]['PARENT_ID'], 'w3')
        self.assertIs(writes[0].args[1]['ERROR'], False)
        self.assertEqual(writes[0].args[1]['SQL'], 'SELECT TRUE')
        self.assertTrue(writes[0].args[1]['PARENT_MUST_BE_NEWER'])

        self.catalog['records'].append(record(extra['id'], 'w2', parent_workflow='w3'))
        rows = model.configuration_rows(self.catalog)
        rows = [row for row in rows if row['id'] != 'w2']
        next(row for row in rows if row['id'] == extra['id'])['id'] = 'w2'
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            model.save(rows, model.revision(self.catalog))
        writes = [call for call in execute.call_args_list if call.args[0].startswith(('INSERT', 'DELETE'))]
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].args[0].startswith('DELETE'))
        self.assertEqual(writes[0].args[1]['PARENT_ID'], 'w1')
        self.assertIn('AND PARENT_ID = %(PARENT_ID)s', writes[0].args[0])

    def test_new_parent_rules_cannot_reuse_assignment_ids_or_move_existing_rules(self):
        for identity in ('a2', 'a1', 'not-a-uuid'):
            with self.subTest(identity=identity):
                rows = copy.deepcopy(self.rows)
                rows.append(dict(rows[2], id=identity, parentId=None, trigger=None))
                # Remove the assignment to ensure the identity check handles it.
                rows = [row for row in rows if row['id'] != identity or not row['jobId']]
                with self.assertRaisesRegex(ValueError, 'identity'):
                    model.validate_rows(rows, self.catalog)

    def test_last_parent_removal_saves_an_external_rule_and_preserves_jobs(self):
        rows = copy.deepcopy(self.rows)
        next(row for row in rows if row['id'] == 'w2').update(
            parentId=None, trigger=None, success=None, warning=None, error=None,
            parentMustBeNewer=False, sql='', allow=True)
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            model.save(rows, model.revision(self.catalog))
        writes = [call for call in execute.call_args_list if call.args[0].startswith(('INSERT', 'DELETE'))]
        self.assertEqual(len(writes), 2)
        self.assertEqual({call.args[1]['PARENT_ID'] for call in writes}, {'w1', None})
        self.assertTrue(all(call.args[1]['JOB_ID'] is None for call in writes))

    def test_definition_names_cannot_be_overwritten_through_the_grid(self):
        self.rows[0]['name'] = 'Spoofed'
        self.rows[-1]['name'] = 'Spoofed'
        validated = model.validate_rows(self.rows, self.catalog)
        self.assertEqual(validated[0]['name'], 'WF1')
        self.assertEqual(validated[-1]['name'], 'JOB2')

    def test_revision_includes_other_parent_links_and_definition_edits(self):
        previous = model.revision(self.catalog)
        self.catalog['workflows'][0]['UPDATED_AT'] = '2026-10-27T12:00:00'
        self.assertNotEqual(previous, model.revision(self.catalog))

    def test_stale_save_is_rejected_and_rolled_back(self):
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            with self.assertRaises(model.ConfigurationConflict):
                model.save(self.rows, 'stale-revision')
            self.assertEqual(execute.call_args_list[-1].args[0], 'ROLLBACK')
            self.assertFalse(any(call.args[0].startswith('INSERT') for call in execute.call_args_list))

    def test_failed_write_rolls_back_the_entire_grid(self):
        self.rows[-1]['sql'] = "SELECT 'not executed'"
        statements = []
        def execute(sql, params=None):
            statements.append(sql)
            if 'SET "PARENT_ID"' in sql:
                raise RuntimeError('write failure')
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service', side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, 'write failure'):
                model.save(self.rows, model.revision(self.catalog))
        self.assertEqual(statements[-1], 'ROLLBACK')
        self.assertNotIn('COMMIT', statements)
        self.assertFalse(any(sql.startswith("SELECT 'not executed'") for sql in statements))

    def test_save_binds_sql_as_configuration_and_commits_with_no_definition_writes(self):
        self.rows[-1]['sql'] = 'SELECT TRUE; -- stored text'
        self.rows[-1]['error'] = False
        with patch.object(model, 'read_catalog', return_value=self.catalog), patch.object(model.sf, 'execute_service') as execute:
            model.save(self.rows, model.revision(self.catalog))
        self.assertEqual(execute.call_args_list[-1].args[0], 'COMMIT')
        writes = [call for call in execute.call_args_list if 'SET "PARENT_ID"' in call.args[0]]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[1]['SQL'], 'SELECT TRUE; -- stored text')
        self.assertIs(writes[0].args[1]['ERROR'], False)
        self.assertTrue(all('WORKFLOW_JOB' not in call.args[0] for call in execute.call_args_list))


if __name__ == '__main__':
    unittest.main()
