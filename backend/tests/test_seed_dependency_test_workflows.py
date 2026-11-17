import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'migrations'))
import seed_dependency_test_workflows as seed


class FixturePlanTests(unittest.TestCase):
    def test_complete_graph_is_idempotent_with_staggered_delays(self):
        inserts, updates = seed.plan([], [], [], 'TEST_WH')
        self.assertEqual({key: len(rows) for key, rows in inserts.items()}, {'WORKFLOW': 10, 'JOB': 10, 'DEPENDENCIES': 23})
        self.assertTrue(all(row['TIMEOUT_SECONDS'] > 60 for row in inserts['JOB']))
        self.assertEqual({seed.DELAYS[name] for name in seed.NAMES}, {20, 30, 60})
        self.assertEqual(seed.PARENTS['WF10'], ['WF8', 'WF9'])
        self.assertTrue(all(not row['IS_SCHEDULE_ENABLED'] for row in inserts['WORKFLOW']))
        new, changed = seed.plan(inserts['WORKFLOW'], inserts['JOB'], inserts['DEPENDENCIES'], 'TEST_WH')
        self.assertTrue(all(not rows for rows in new.values()))
        self.assertTrue(all(not rows for rows in changed.values()))

    def test_extending_old_four_preserves_all_existing_rule_settings(self):
        all_rows, _ = seed.plan([], [], [], 'TEST_WH')
        old_ids = {seed.IDS[name] for name in seed.NAMES[:4]}
        workflows = [row for row in all_rows['WORKFLOW'] if row['WORKFLOW_ID'] in old_ids]
        jobs = [row for row in all_rows['JOB'] if row['JOB_ID'] in {seed.JOBS[name] for name in seed.NAMES[:4]}]
        rules = [row for row in all_rows['DEPENDENCIES'] if row['WORKFLOW_ID'] in old_ids]
        for row in workflows + jobs:
            row['DESCRIPTION'] = seed.LEGACY_MARKER
        for row in jobs:
            row['SQL_COMMAND'] = row['SQL_COMMAND'].split(', SYSTEM$WAIT')[0]
            row['TIMEOUT_SECONDS'] = 60
        rules[-2].update(ALLOW=False, SUCCESS=False, SQL='SELECT FALSE', PARENT_MUST_BE_NEWER=False)
        previous = copy.deepcopy(rules)
        inserts, updates = seed.plan(workflows, jobs, rules, 'TEST_WH')
        self.assertEqual({key: len(rows) for key, rows in inserts.items()}, {'WORKFLOW': 6, 'JOB': 6, 'DEPENDENCIES': 14})
        self.assertEqual({key: len(rows) for key, rows in updates.items()}, {'WORKFLOW': 4, 'JOB': 4})
        self.assertEqual(rules, previous)
        self.assertTrue(all(row['WORKFLOW_ID'] not in old_ids for row in inserts['DEPENDENCIES']))

    def test_conflicts_edited_sql_and_external_links_are_rejected(self):
        original, _ = seed.plan([], [], [], 'TEST_WH')
        for kind in ('identity', 'command', 'link', 'duplicate', 'scheduled'):
            rows = copy.deepcopy(original)
            if kind == 'identity': rows['WORKFLOW'][0]['DESCRIPTION'] = 'Unrelated workflow'
            if kind == 'command': rows['JOB'][0]['SQL_COMMAND'] = 'SELECT custom_work()'
            if kind == 'link': rows['DEPENDENCIES'].append(dict(rows['DEPENDENCIES'][0], WORKFLOW_ID='external'))
            if kind == 'duplicate': rows['DEPENDENCIES'].append(rows['DEPENDENCIES'][0])
            if kind == 'scheduled': rows['WORKFLOW'][0]['IS_SCHEDULE_ENABLED'] = True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                seed.plan(rows['WORKFLOW'], rows['JOB'], rows['DEPENDENCIES'], 'TEST_WH')
