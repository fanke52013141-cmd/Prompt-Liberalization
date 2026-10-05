"""Real pinned SDK smoke test, entirely offline; run in the optional venv."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prompt_gepa.adapter import PromptAdapter
from prompt_gepa.search import optimize


class SDKTests(unittest.TestCase):
    def test_official_search_returns_complementary_frontier_and_lineage(self):
        train = [{"id": f"d{i}", "purpose": "dev", "source_group": f"d{i}",
                  "input": str(i), "index": i} for i in range(2)]
        selection = [{"id": f"s{i}", "purpose": "select", "source_group": f"s{i}",
                      "input": f"SELECTION_SECRET_{i}", "index": i} for i in range(2)]
        reflected = []
        def reflect(messages):
            reflected.append(messages)
            return "candidate A" if len(reflected) == 1 else "candidate B"
        def evaluate(body, item):
            score = {"baseline": [0.2, 0.2], "candidate A": [1.0, 0.3],
                     "candidate B": [0.7, 1.0]}[body][item["index"]]
            return {"score": score, "output": body, "status": "ok", "feedback": "case-specific quality"}
        adapter = PromptAdapter(train, selection, evaluate, lambda: False)
        result = optimize("baseline", adapter, reflect, max_metric_calls=30, seed=4)
        bodies = [candidate["body"] for candidate in result["candidates"]]
        self.assertIn("candidate A", bodies)
        self.assertIn("candidate B", bodies)
        ai, bi = bodies.index("candidate A"), bodies.index("candidate B")
        self.assertIn(ai, result["frontier"]["s0"])
        self.assertIn(bi, result["frontier"]["s1"])
        self.assertTrue(result["parents"][ai])
        self.assertTrue(result["parents"][bi])
        self.assertGreater(result["metric_calls"], 0)
        self.assertTrue(result["evaluations"])
        self.assertNotIn("SELECTION_SECRET", str(reflected))


if __name__ == "__main__":
    unittest.main()
