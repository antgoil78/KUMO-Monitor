import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import batch_control as batches


class BatchControlTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(batches.batch_control_bp)
        self.client = app.test_client()
        self.cursor = MagicMock()
        self.cursor.fetchone.return_value = {"TOTAL": 205}
        self.cursor.fetchall.return_value = []
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor
        connection = patch.object(batches.sf, "connection")
        self.connection = connection.start()
        self.connection.return_value.__enter__.return_value = self.conn
        self.addCleanup(connection.stop)
        for patcher in (
            patch.object(batches.config, "USE_MOCK", False),
            patch.object(batches.sf, "is_configured", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_page_is_bounded_and_serializes_snowflake_values(self):
        self.cursor.fetchall.return_value = [{
            "READY_TO_LOAD_DTTM": datetime(2026, 10, 16, 8, 30),
            "NO_OF_RECS": Decimal("1234"), "PROCESSED_FL": False,
            "PROCESSED_DTTM": None,
        }]
        response = self.client.get("/api/lim/batch-control")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["total"], 205)
        self.assertEqual(len(data["columns"]), 11)
        self.assertEqual(data["rows"][0]["NO_OF_RECS"], 1234)
        self.assertIs(data["rows"][0]["PROCESSED_FL"], False)
        self.assertIsNone(data["rows"][0]["PROCESSED_DTTM"])
        self.assertIn("2026-10-16T08:30", data["rows"][0]["READY_TO_LOAD_DTTM"])
        self.connection.assert_called_once_with(use_warehouse=True, include_context=True, force_service=True)
        select = self.cursor.execute.call_args
        self.assertIn('ORDER BY "READY_TO_LOAD_DTTM" DESC NULLS LAST', select.args[0])
        self.assertEqual(select.args[1], {"limit": 100, "offset": 0})
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in self.cursor.execute.call_args_list))

    def test_search_is_bound_and_applied_to_count_and_rows(self):
        search = "CUAR_100%' OR 1=1 --"
        response = self.client.get("/api/lim/batch-control", query_string={"search": search, "page": 2, "sortBy": "NO_OF_RECS", "sortDirection": "asc"})
        self.assertEqual(response.status_code, 200)
        count, select = self.cursor.execute.call_args_list
        for call in (count, select):
            self.assertNotIn(search, call.args[0])
            self.assertEqual(call.args[1]["search"], search)
            self.assertEqual(call.args[0].count("CONTAINS("), len(batches.COLUMN_KEYS))
        self.assertEqual(select.args[1]["offset"], 100)
        self.assertIn('ORDER BY "NO_OF_RECS" ASC NULLS LAST', select.args[0])

    def test_every_column_can_be_sorted_in_both_directions(self):
        for column in batches.COLUMN_KEYS:
            for direction in ("asc", "desc"):
                with self.subTest(column=column, direction=direction):
                    response = self.client.get("/api/lim/batch-control", query_string={"sortBy": column, "sortDirection": direction})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(f'ORDER BY "{column}" {direction.upper()} NULLS LAST', self.cursor.execute.call_args.args[0])

    def test_invalid_sort_or_page_never_reaches_snowflake(self):
        for params in (
            {"sortBy": "DW_TABLE_NM; DROP TABLE X"}, {"sortDirection": "desc;--"},
            {"page": "0"}, {"page": "abc"}, {"pageSize": "0"},
            {"pageSize": "501"}, {"search": "x" * 501},
        ):
            with self.subTest(params=params):
                response = self.client.get("/api/lim/batch-control", query_string=params)
                self.assertEqual(response.status_code, 400)
        self.connection.assert_not_called()

    def test_out_of_range_page_clamps_after_filtering(self):
        self.cursor.fetchone.return_value = {"TOTAL": 3}
        response = self.client.get("/api/lim/batch-control?page=99")
        self.assertEqual(response.get_json()["page"], 1)
        self.assertEqual(self.cursor.execute.call_args.args[1]["offset"], 0)
        self.cursor.fetchone.return_value = {"TOTAL": 0}
        response = self.client.get("/api/lim/batch-control")
        self.assertEqual(response.get_json()["total"], 0)
        self.assertEqual(response.get_json()["rows"], [])

    def test_database_failure_is_reported_and_cursor_closed(self):
        self.cursor.execute.side_effect = RuntimeError("Batch control unavailable")
        response = self.client.get("/api/lim/batch-control")
        self.assertEqual(response.status_code, 500)
        self.assertIn("Batch control unavailable", response.get_json()["error"])
        self.cursor.close.assert_called_once()

    def test_mock_mode_does_not_query_snowflake(self):
        with patch.object(batches.config, "USE_MOCK", True):
            response = self.client.get("/api/lim/batch-control")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["source"], "mock")
        self.connection.assert_not_called()

    def test_summary_uses_requested_grouping_and_stable_default_order(self):
        self.cursor.fetchall.return_value = [{
            "LIM": "CUAR000", "FILES": 3, "RECS": Decimal("1450"),
            "GROUP_KEY": "a" * 64, "PROCESSED_FL": True, "DELETED_FL": False,
        }]
        response = self.client.get("/api/lim/batch-control/groups")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["rows"][0]["RECS"], 1450)
        self.assertEqual(len(response.get_json()["columns"]), 8)
        for call in self.cursor.execute.call_args_list:
            self.assertIn("SUBSTR(DW_FILE_NM, 4, 7) AS LIM", call.args[0])
            self.assertIn("COUNT(*) AS FILES, SUM(NO_OF_RECS) AS RECS", call.args[0])
            self.assertIn("GROUP BY ALL", call.args[0])
        self.assertIn('ORDER BY "READY_TO_LOAD_DTTM" DESC NULLS LAST, "LIM" ASC NULLS LAST, "PROCESSED_DTTM" DESC NULLS LAST', self.cursor.execute.call_args.args[0])

    def test_detail_filters_both_count_and_rows_by_exact_group_identity(self):
        key = "a" * 64
        response = self.client.get("/api/lim/batch-control", query_string={"groupKey": key, "search": "CUAR"})
        self.assertEqual(response.status_code, 200)
        for call in self.cursor.execute.call_args_list:
            self.assertEqual(call.args[1]["group_key"], key)
            self.assertIn(batches.GROUP_KEY_SQL + " = %(group_key)s", call.args[0])
            self.assertNotIn(key, call.args[0])
        # Raw nanoseconds and all nullable grouping fields participate in the
        # shared identity, so sibling groups cannot leak into expanded files.
        for field in ("READY_TO_LOAD_DTTM", "DW_VER_CTRL", "PROCESSED_DTTM", "PROCESSED_FL", "DELETED_FL"):
            self.assertIn(field, batches.GROUP_KEY_SQL)
        self.assertIn("FF9", batches.GROUP_KEY_SQL)

    def test_summary_sort_and_search_are_validated_independently_of_file_columns(self):
        for column in ("FILES", "RECS", "LIM"):
            response = self.client.get("/api/lim/batch-control/groups", query_string={"sortBy": column, "sortDirection": "desc", "search": "Yes"})
            self.assertEqual(response.status_code, 200)
            sql, params = self.cursor.execute.call_args.args
            self.assertIn(f'ORDER BY "{column}" DESC NULLS LAST', sql)
            self.assertEqual(params["search"], "Yes")
            self.assertIn('IFF("PROCESSED_FL", \'Yes\', \'No\')', sql)
        self.connection.reset_mock()
        for path in (
            "/api/lim/batch-control/groups?sortBy=DW_FILE_NM",
            "/api/lim/batch-control/groups?sortBy=FILES;DROP",
            "/api/lim/batch-control?groupKey=invalid",
            "/api/lim/batch-control/groups?groupKey=" + "a" * 64,
        ):
            self.assertEqual(self.client.get(path).status_code, 400)
        self.connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
