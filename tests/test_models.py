"""Роли моделей: чат, «тяжёлая» (качество важнее отклика) и синтез; поведение по умолчанию не меняется.
Запуск из папки code/:  python3 -m unittest tests.test_models -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localcoach.config import Config  # noqa: E402
from localcoach.ollama import Ollama  # noqa: E402
from localcoach import evaluate  # noqa: E402


def spy(cfg: Config) -> tuple[Ollama, list[str]]:
    seen: list[str] = []
    llm = Ollama(cfg)
    llm._json = lambda path, payload, timeout=0: (seen.append(payload["model"]) or
                                                  {"message": {"content": "{}"}, "done_reason": "stop"})
    llm._check_context = lambda data: None
    return llm, seen


class ModelRolesTest(unittest.TestCase):
    def test_default_everything_on_chat_model(self) -> None:
        cfg = Config(model="chat:9b")
        llm, seen = spy(cfg)
        llm.chat_json("s", "u", {}, heavy=True)
        llm.chat_json("s", "u", {})
        self.assertEqual(seen, ["chat:9b", "chat:9b"])
        self.assertEqual((cfg.heavy_name, cfg.synth_name), ("chat:9b", "chat:9b"))

    def test_heavy_only_for_heavy_calls(self) -> None:
        cfg = Config(model="chat:9b", heavy_model="big:27b")
        llm, seen = spy(cfg)
        llm.chat_json("s", "u", {}, heavy=True)
        llm.chat_json("s", "u", {})
        self.assertEqual(seen, ["big:27b", "chat:9b"], "быстрые вызовы остаются на чатовой модели")
        self.assertEqual(cfg.synth_name, "big:27b", "синтез идёт на тяжёлой, пока не задана своя")

    def test_synth_model_overrides_and_explicit_model_wins(self) -> None:
        cfg = Config(model="chat:9b", heavy_model="big:27b", synth_model="think:32b")
        llm, seen = spy(cfg)
        llm.chat_json("s", "u", {}, heavy=True, model=cfg.synth_name)
        self.assertEqual((seen, cfg.heavy_name), (["think:32b"], "big:27b"))

    def test_eval_tag_names_all_models(self) -> None:
        self.assertEqual(evaluate.version_tag(Config(model="a")).split("|")[2], "a")
        self.assertEqual(evaluate.version_tag(Config(model="a", heavy_model="b")).split("|")[2], "a+b")
        self.assertEqual(evaluate.version_tag(Config(model="a", heavy_model="b", synth_model="c")).split("|")[2], "a+b+c")


if __name__ == "__main__":
    unittest.main()
