from __future__ import annotations

import unittest


class AgenticResearchConfigTests(unittest.TestCase):
    def test_uses_conservative_defaults_and_validates_bounds(self):
        from backend.agentic_research.config import load_agentic_research_config

        config = load_agentic_research_config({"enabled": True})

        self.assertTrue(config.enabled)
        self.assertEqual(config.max_route_transitions, 3)
        self.assertEqual(config.source_call_limits["personal_knowledge"], 1)
        self.assertGreater(config.deadline_seconds, 0)
        self.assertGreater(config.result_limit, 0)
        self.assertGreater(config.excerpt_char_limit, 0)

    def test_rejects_invalid_limits_and_secret_bearing_fields(self):
        from backend.agentic_research.config import AgenticResearchConfigError, load_agentic_research_config

        with self.assertRaises(AgenticResearchConfigError):
            load_agentic_research_config({"max_route_transitions": 0})
        with self.assertRaises(AgenticResearchConfigError):
            load_agentic_research_config({"source_call_limits": {"web": -1}})
        with self.assertRaises(AgenticResearchConfigError):
            load_agentic_research_config({"provider_token": "not-allowed"})

    def test_disabled_config_has_no_agentic_runtime_side_effect(self):
        from backend.agentic_research.config import load_agentic_research_config

        config = load_agentic_research_config({"enabled": False})

        self.assertFalse(config.enabled)


if __name__ == "__main__":
    unittest.main()
