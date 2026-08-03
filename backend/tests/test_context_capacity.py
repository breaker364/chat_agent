import json
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.agent import (
    check_context_capacity,
    get_model_context_window,
    estimate_tokens_from_text,
)


class TestContextCapacityCheck(unittest.TestCase):
    """Test context window capacity checking logic."""

    @patch("backend.agent.get_model_context_window")
    def test_check_context_capacity_returns_remaining_tokens(self, mock_get_window):
        """Test that check_context_capacity returns correct remaining token count."""
        mock_get_window.return_value = 128000
        result = check_context_capacity(
            context_token_estimate=50000,
            model_name="deepseek-chat",
        )
        self.assertEqual(result["model_context_window"], 128000)
        self.assertEqual(result["context_token_estimate"], 50000)
        self.assertEqual(result["remaining_tokens"], 78000)
        self.assertFalse(result["is_exceeded"])

    @patch("backend.agent.get_model_context_window")
    def test_check_context_capacity_exceeded(self, mock_get_window):
        """Test that check_context_capacity detects when limit is exceeded."""
        mock_get_window.return_value = 128000
        result = check_context_capacity(
            context_token_estimate=130000,
            model_name="deepseek-chat",
        )
        self.assertEqual(result["model_context_window"], 128000)
        self.assertEqual(result["context_token_estimate"], 130000)
        self.assertEqual(result["remaining_tokens"], 0)
        self.assertTrue(result["is_exceeded"])

    @patch("backend.agent.get_model_context_window")
    def test_check_context_capacity_exact_limit(self, mock_get_window):
        """Test that check_context_capacity handles exact limit case."""
        mock_get_window.return_value = 128000
        result = check_context_capacity(
            context_token_estimate=128000,
            model_name="deepseek-chat",
        )
        self.assertEqual(result["remaining_tokens"], 0)
        self.assertFalse(result["is_exceeded"])

    @patch("backend.agent.get_model_context_window")
    def test_check_context_capacity_unknown_model(self, mock_get_window):
        """Test that check_context_capacity handles unknown model gracefully."""
        mock_get_window.return_value = None
        result = check_context_capacity(
            context_token_estimate=50000,
            model_name="unknown-model",
        )
        self.assertIsNone(result["model_context_window"])
        self.assertEqual(result["context_token_estimate"], 50000)
        self.assertIsNone(result["remaining_tokens"])
        self.assertFalse(result["is_exceeded"])

    @patch("backend.agent.get_model_context_window")
    def test_check_context_capacity_zero_estimate(self, mock_get_window):
        """Test that check_context_capacity handles zero token estimate."""
        mock_get_window.return_value = 128000
        result = check_context_capacity(
            context_token_estimate=0,
            model_name="deepseek-chat",
        )
        self.assertEqual(result["remaining_tokens"], 128000)
        self.assertFalse(result["is_exceeded"])


class TestGetModelContextWindow(unittest.TestCase):
    """Test model context window configuration retrieval."""

    @patch("backend.config.load_app_config")
    def test_get_model_context_window_from_config(self, mock_load_config):
        """Test that get_model_context_window reads from config."""
        mock_load_config.return_value = {
            "model": "deepseek-chat",
            "model_context_window": 128000,
        }
        result = get_model_context_window("deepseek-chat")
        self.assertEqual(result, 128000)

    @patch("backend.config.load_app_config")
    def test_get_model_context_window_default_value(self, mock_load_config):
        """Test that get_model_context_window returns default when not configured."""
        mock_load_config.return_value = {
            "model": "deepseek-chat",
        }
        result = get_model_context_window("deepseek-chat")
        self.assertIsNone(result)

    @patch("backend.config.load_app_config")
    def test_get_model_context_window_different_model(self, mock_load_config):
        """Test that get_model_context_window returns None for non-matching model."""
        mock_load_config.return_value = {
            "model": "gpt-4",
            "model_context_window": 128000,
        }
        result = get_model_context_window("deepseek-chat")
        self.assertIsNone(result)

    @patch("backend.config.load_app_config")
    def test_get_model_context_window_invalid_config(self, mock_load_config):
        """Test that get_model_context_window handles invalid config gracefully."""
        mock_load_config.return_value = {
            "model": "deepseek-chat",
            "model_context_window": "invalid",
        }
        result = get_model_context_window("deepseek-chat")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
