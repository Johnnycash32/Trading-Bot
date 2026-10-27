import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from trading_bot.gemini_agent import DEFAULT_MODEL, GeminiAnalysisAgent, gemini_config
from trading_bot.market_data import Candle, Ticker


class GeminiAgentTests(unittest.TestCase):
    def test_reads_gemini_key_from_env_file_without_exposing_it(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('GEMINI_API_KEY="test-secret" # local only\nGEMINI_MODEL=gemini-test\n', encoding="utf-8")
            api_key, model = gemini_config(env={}, dotenv_path=path)

        self.assertEqual(api_key, "test-secret")
        self.assertEqual(model, "gemini-test")

    def test_process_environment_takes_precedence_over_env_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("GEMINI_API_KEY=file-key\n", encoding="utf-8")
            api_key, _ = gemini_config(env={"GEMINI_API_KEY": "process-key"}, dotenv_path=path)

        self.assertEqual(api_key, "process-key")

    def test_analysis_uses_api_key_header_and_public_candle_context(self) -> None:
        requests = []

        def fake_request(request, timeout):
            requests.append((request, timeout))
            return {"candidates": [{"content": {"parts": [{"text": "Trend is mixed."}]}}]}

        agent = GeminiAnalysisAgent("test-secret", requester=fake_request)
        result = agent.analyze(
            exchange="bybit",
            symbol="SOLUSDT",
            interval="1",
            ticker=Ticker("bybit", "SOLUSDT", 100.0, 99.9, 100.1, 1.2, 1),
            candles=[Candle(1, 99, 101, 98, 100, 10)],
        )

        request, timeout = requests[0]
        self.assertEqual(result, "Trend is mixed.")
        self.assertEqual(timeout, 30)
        self.assertNotIn("test-secret", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "test-secret")
        sent = json.loads(request.data)
        self.assertIn("SOLUSDT", sent["contents"][0]["parts"][0]["text"])
        self.assertIn("100.0", sent["contents"][0]["parts"][0]["text"])

    def test_empty_api_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GeminiAnalysisAgent(" ")

    def test_default_model_is_explicit(self) -> None:
        self.assertEqual(DEFAULT_MODEL, "gemini-2.5-flash")


if __name__ == "__main__":
    unittest.main()
