"""Official tau2 HalfDuplexAgent adapter, with Xana owning conversation state."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from tau2.agent.base_agent import HalfDuplexAgent, is_valid_agent_history_message
from tau2.agent.llm_agent import AGENT_INSTRUCTION, SYSTEM_PROMPT
from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall, ToolMessage, UserMessage

from xana.benchmarks.process import TrialProcess
from xana.benchmarks.workspace import prepare_home, remove_credentials


class XanaAgent(HalfDuplexAgent[TrialProcess]):
    def __init__(self, tools, domain_policy: str, *, llm: str, xana_home: str,
                 artifacts: str, timeout: float = 600, max_turns: int = 200) -> None:
        super().__init__(tools, domain_policy)
        self.model = llm
        self.source_home = Path(xana_home).expanduser().resolve()
        self.artifacts = Path(artifacts).expanduser().resolve()
        self.timeout = timeout
        self.max_turns = max_turns
        self.active: TrialProcess | None = None

    def get_init_state(self, message_history=None) -> TrialProcess:
        self.stop()
        history = message_history or []
        if not all(is_valid_agent_history_message(message) for message in history):
            raise ValueError("Invalid tau2 initial history")
        self.artifacts.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="trial-", dir=self.artifacts))
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "home"
        schemas = [tool.openai_schema for tool in self.tools]
        # Official benchmark input, unchanged; no invented scheduling or policy prompt.
        policy = SYSTEM_PROMPT.format(agent_instruction=AGENT_INSTRUCTION,
                                      domain_policy=self.domain_policy)
        prepare_home(self.source_home, home, model=self.model,
                     tools=[schema["function"]["name"] for schema in schemas],
                     system_prompt=policy, max_turns=self.max_turns)
        try:
            self.active = TrialProcess(home, workspace, root, timeout=self.timeout,
                init={"tools": schemas, "history": [m.model_dump() for m in history]})
        except BaseException:
            remove_credentials(home)
            raise
        return self.active

    def generate_next_message(self, message, state: TrialProcess):
        if state is not self.active:
            raise ValueError("Stale tau2 trial state")
        if isinstance(message, UserMessage):
            if message.is_tool_call() or message.content is None:
                raise ValueError("Xana tau2 adapter supports text user messages")
            state.send({"kind": "user", "text": message.content})
        else:
            results = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
            for result in results:
                if not isinstance(result, ToolMessage) or result.requestor != "assistant":
                    raise ValueError("Unexpected tau2 tool message")
                state.send({"kind": "tool_result", "id": result.id,
                            "content": result.content, "error": result.error})
        response = state.receive()
        if response["kind"] == "tool":
            answer = AssistantMessage(role="assistant", tool_calls=[ToolCall(
                id=response["id"], name=response["name"], arguments=response["arguments"])])
        elif response["kind"] == "final":
            usage = response["usage"]
            answer = AssistantMessage(role="assistant", content=response["text"], usage={
                "prompt_tokens": usage.get("total_input_tokens", 0),
                "completion_tokens": usage.get("total_output_tokens", 0),
                "total_tokens": usage.get("total_input_tokens", 0) + usage.get("total_output_tokens", 0),
            })
        else:
            raise RuntimeError(f"Unexpected Xana message: {response}")
        return answer, state

    def stop(self, message=None, state=None) -> None:
        if self.active is not None:
            self.active.close()
            self.active = None


def create_agent(tools, domain_policy, *, llm, llm_args=None, **kwargs):
    if (kwargs.get("solo_mode") or kwargs.get("audio_native_config")
            or kwargs.get("additional_stop_tool_names") or kwargs.get("agent_instruction_override")):
        raise ValueError("Xana adapter requires the tau2 text conversation protocol")
    return XanaAgent(tools, domain_policy, llm=llm, **(llm_args or {}))


def main() -> None:
    from tau2.data_model.simulation import TextRunConfig
    from tau2.registry import registry
    from tau2.runner import run_domain

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Official tau2 TextRunConfig JSON")
    parser.add_argument("--xana-home", type=Path, required=True, help="Dedicated benchmark model/login home")
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    config["agent"] = "xana"
    config["llm_args_agent"] = {**config.get("llm_args_agent", {}),
        "xana_home": str(args.xana_home.resolve()), "artifacts": str(args.artifacts.resolve())}
    registry.register_agent_factory(create_agent, "xana")
    run_domain(TextRunConfig.model_validate(config))


if __name__ == "__main__":
    main()
