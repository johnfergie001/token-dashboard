import json
import os
import tempfile
import unittest

from token_dashboard.db import init_db, connect
from token_dashboard.subprojects import load_map, project_cost_rows

PRICING = {
    "models": {},
    "tier_fallback": {
        "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_create_5m": 3.75, "cache_create_1h": 6.0},
        "haiku":  {"input": 1.0, "output": 5.0,  "cache_read": 0.1, "cache_create_5m": 1.25, "cache_create_1h": 2.0},
    },
}


class LoadMapTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(load_map("/no/such/file.json"), [])

    def test_invalid_json_returns_empty(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("not json")
            path = f.name
        try:
            self.assertEqual(load_map(path), [])
        finally:
            os.unlink(path)

    def test_loads_groups_and_compiles_patterns(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"groups": [{"name": "Proj A", "patterns": [r"[\\/]ProjA[\\/]"]}]}, f)
            path = f.name
        try:
            groups = load_map(path)
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["name"], "Proj A")
            self.assertTrue(groups[0]["patterns"][0].search(r"C:\ProjA\file.py"))
        finally:
            os.unlink(path)

    def test_skips_bad_regex_but_keeps_valid_siblings(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"groups": [{"name": "P", "patterns": ["[unclosed", "ok"]}]}, f)
            path = f.name
        try:
            groups = load_map(path)
            self.assertEqual(len(groups[0]["patterns"]), 1)
        finally:
            os.unlink(path)


class ProjectCostRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "sp.db")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, model,
              input_tokens, output_tokens, cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens)
            VALUES
              ('u1','s1','hub','user','2026-04-10T00:00:00Z',NULL,0,0,0,0,0),
              ('a1','s1','hub','assistant','2026-04-10T00:00:01Z','claude-sonnet-4-6',100,200,0,0,0),
              ('u2','s2','hub','user','2026-04-11T00:00:00Z',NULL,0,0,0,0,0),
              ('a2','s2','hub','assistant','2026-04-11T00:00:01Z','claude-sonnet-4-6',50,50,0,0,0);

            INSERT INTO tool_calls (message_uuid, session_id, project_slug, tool_name, target, timestamp, is_error)
            VALUES
              ('a1','s1','hub','Bash','ssh host "cd /var/www/widget && deploy.sh"','2026-04-10T00:00:01Z',0),
              ('a1','s1','hub','Bash','ls','2026-04-10T00:00:02Z',0),
              ('a1','s1','hub','Read','/repo/AIS-OS/notes.md','2026-04-10T00:00:03Z',0),
              ('a2','s2','hub','Bash','git status','2026-04-11T00:00:01Z',0);
            """)
            c.commit()
        self.map_path = os.path.join(self.tmp, "map.json")
        with open(self.map_path, "w") as f:
            json.dump({"groups": [{"name": "Widget", "patterns": [r"[\\/]var[\\/]www[\\/]widget(?=[\\/\s\"'&;)]|$)"]}]}, f)

    def test_no_map_falls_back_to_slug_grouping(self):
        rows = project_cost_rows(self.db, PRICING, map_path="/no/such/file.json")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project_slug"], "hub")
        self.assertFalse(rows[0]["subproject"])
        self.assertEqual(rows[0]["sessions"], 2)

    def test_cost_matches_manual_tier_calc(self):
        rows = project_cost_rows(self.db, PRICING, map_path="/no/such/file.json")
        # s1: 100*3 + 200*15 = 3300; s2: 50*3+50*15 = 900 -> 4200/1e6
        self.assertAlmostEqual(rows[0]["cost_usd"], 4200 / 1_000_000, places=6)

    def test_map_splits_session_by_bash_call_share(self):
        rows = project_cost_rows(self.db, PRICING, map_path=self.map_path)
        by_name = {r["project_name"]: r for r in rows}
        self.assertIn("Widget", by_name)
        self.assertIn("hub", by_name)
        self.assertTrue(by_name["Widget"]["subproject"])
        # s1 has 3 real tool calls (Bash x2, Read x1), 1 matches Widget -> weight 1/3
        s1_cost = (100 * 3 + 200 * 15) / 1_000_000
        self.assertAlmostEqual(by_name["Widget"]["cost_usd"], s1_cost / 3, places=8)
        # remainder of s1 (2/3) plus all of s2 (no calls matched) land on the slug bucket
        s2_cost = (50 * 3 + 50 * 15) / 1_000_000
        self.assertAlmostEqual(by_name["hub"]["cost_usd"], s1_cost * 2 / 3 + s2_cost, places=8)

    def test_rows_sorted_by_cost_desc(self):
        rows = project_cost_rows(self.db, PRICING, map_path=self.map_path)
        costs = [r["cost_usd"] for r in rows]
        self.assertEqual(costs, sorted(costs, reverse=True))

    def test_since_until_filters_sessions(self):
        rows = project_cost_rows(self.db, PRICING, since="2026-04-11T00:00:00Z", map_path="/no/such/file.json")
        self.assertEqual(rows[0]["sessions"], 1)


if __name__ == "__main__":
    unittest.main()
