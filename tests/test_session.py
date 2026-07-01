import unittest
from pathlib import Path
from types import SimpleNamespace

from minicode.agent import AgentConfig, CodingAgent, CodingSession, SessionEvent
from minicode.llm import LLMResponse, LLMStreamDelta, LLMStreamDone
from minicode.observability import TokenUsage
from minicode.skills import SkillCatalog


class FakeSandbox:
    def __init__(self):
        self.workspace = Path(".").resolve()

    def run(self, command: str):
        return SimpleNamespace(exit_code=0, stdout="/workspace\n./README.md\n", stderr="")


class StreamingFinishLlm:
    def chat_response_stream(self, model, messages):
        text = '{"thought":"done","action":"finish","args":{"answer":"ok"}}'
        yield LLMStreamDelta(content=text[:10], raw={})
        yield LLMStreamDelta(content=text[10:], raw={})
        yield LLMStreamDone(
            LLMResponse(
                content=text,
                token_usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
                duration_ms=1,
                raw={"stream": True},
            )
        )

    def chat_response(self, model, messages):
        raise AssertionError("non-streaming fallback should not be used")


class StreamingFailureLlm:
    def chat_response_stream(self, model, messages):
        raise RuntimeError("stream failed")

    def chat_response(self, model, messages):
        return LLMResponse(
            content='{"thought":"done","action":"finish","args":{"answer":"fallback ok"}}',
            token_usage=TokenUsage(prompt_tokens=4, completion_tokens=5, total_tokens=9),
            duration_ms=1,
            raw={},
        )


class SessionTests(unittest.TestCase):
    def build_agent(self, llm):
        return CodingAgent(
            llm=llm,
            sandbox=FakeSandbox(),
            config=AgentConfig(model="fake", skills_enabled=False),
            skill_catalog=SkillCatalog.empty(),
        )

    def test_agent_module_reexports_session_types(self):
        self.assertEqual(CodingSession.__name__, "CodingSession")
        self.assertEqual(SessionEvent.__name__, "SessionEvent")

    def test_iter_turn_emits_streaming_delta_and_finish(self):
        session = self.build_agent(StreamingFinishLlm()).start_session()

        events = list(session.iter_turn("say ok"))

        self.assertIn("model_delta", [event.kind for event in events])
        self.assertEqual(events[-1].kind, "turn_finish")
        self.assertEqual(events[-1].data["answer"], "ok")

    def test_stream_failure_falls_back_to_non_streaming_response(self):
        session = self.build_agent(StreamingFailureLlm()).start_session()

        events = list(session.iter_turn("say ok"))

        self.assertIn("model_stream_fallback", [event.kind for event in events])
        self.assertEqual(events[-1].kind, "turn_finish")
        self.assertEqual(events[-1].data["answer"], "fallback ok")


if __name__ == "__main__":
    unittest.main()
