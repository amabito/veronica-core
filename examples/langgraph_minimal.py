"""LangGraph minimal — Node-level enforcement via veronica_node_wrapper.

Wrap any LangGraph node function with veronica_node_wrapper to enforce
budget and step count limits before the node executes.

Install:
    pip install veronica-core langgraph langchain-core

This example builds a minimal two-node graph and shows VeronicaHalt being
raised when the step limit is exceeded. No real API key required.
"""

from __future__ import annotations

from veronica_core import GuardConfig
from veronica_core.adapters.langgraph import veronica_node_wrapper
from veronica_core.inject import VeronicaHalt


# ---------------------------------------------------------------------------
# Shared config — one container per node for independent limit tracking,
# or pass container=... to share limits across nodes.
# ---------------------------------------------------------------------------

config = GuardConfig(max_cost_usd=1.0, max_steps=2)


# ---------------------------------------------------------------------------
# Node definitions
# ---------------------------------------------------------------------------


@veronica_node_wrapper(config)
def call_model(state: dict) -> dict:
    """Invoke the LLM and append its reply to state["messages"].

    In production, replace the stub with a real LLM call:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "sk-..."))
        response = llm.invoke(state["messages"])
        return {"messages": state["messages"] + [response]}
    """
    stub_reply = f"[stub] step {len(state['messages']) + 1}"
    return {"messages": state["messages"] + [stub_reply]}


@veronica_node_wrapper(config)
def summarize(state: dict) -> dict:
    """Summarize the conversation accumulated in state["messages"]."""
    summary = "Summary: " + " | ".join(state["messages"])
    return {"messages": state["messages"], "summary": summary}


# ---------------------------------------------------------------------------
# Minimal graph runner (no real LangGraph compile needed for the demo)
# ---------------------------------------------------------------------------


def run_graph(state: dict) -> dict:
    """Execute call_model -> summarize in sequence."""
    state = call_model(state)
    state = summarize(state)
    return state


def main() -> None:
    # Initial graph state
    state: dict = {"messages": []}

    # Run the graph twice — second run should hit the step limit
    for run in range(1, 4):
        print(f"Run {run}:")
        try:
            state = run_graph(state)
            steps = call_model.container.step_guard  # type: ignore[attr-defined]
            used = steps.steps if steps else 0
            print(f"  OK: summary={state.get('summary', '(none)')}")
            print(f"  steps={used}/{config.max_steps}")
        except VeronicaHalt as exc:
            print(f"  HALTED: {exc}")
            break


if __name__ == "__main__":
    main()
