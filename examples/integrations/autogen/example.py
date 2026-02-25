"""VERONICA Core + AG2 integration examples.



Three self-contained demos showing how to wrap AG2 agents with VERONICA's

circuit-breaker and SAFE_MODE. No API key required — stub agents only.



Demos:

    1. demo_circuit_breaker  — agent fails repeatedly, cooldown activates

    2. demo_safe_mode        — orchestrator triggers system-wide halt

    3. demo_per_agent_tracking — healthy vs broken agent tracked independently

"""



from __future__ import annotations



from typing import Optional



from veronica_core import VeronicaIntegration

from veronica_core.backends import MemoryBackend

from veronica_core.state import VeronicaState





# ---------------------------------------------------------------------------

# Stub agent (mimics ag2.ConversableAgent for demo purposes)

# ---------------------------------------------------------------------------



class StubAgent:

    """Minimal stand-in for ag2.ConversableAgent.



    Real AG2 equivalent:

        import ag2

        agent = ag2.ConversableAgent(

            name="planner",

            llm_config={"model": "gpt-4o-mini"},

        )

    """



    def __init__(self, name: str, fail_after: Optional[int] = None) -> None:

        self.name = name

        self._fail_after = fail_after

        self._call_count = 0



    def generate_reply(

        self,

        messages: list[dict],

        sender: Optional["StubAgent"] = None,

    ) -> Optional[str]:

        """Return a stub reply or None to simulate an agent failure."""

        self._call_count += 1

        if self._fail_after is not None and self._call_count > self._fail_after:

            return None  # simulate LLM returning nothing / refusing

        return f"[{self.name}] reply #{self._call_count}"





# ---------------------------------------------------------------------------

# guarded_reply factory

# ---------------------------------------------------------------------------



def make_guarded_reply(veronica: VeronicaIntegration):

    """Return a guarded_reply function bound to the given VeronicaIntegration.



    The inner function is the exact pattern from the VERONICA docs:



        def guarded_reply(agent, messages):

            if veronica.state.current_state == VeronicaState.SAFE_MODE:

                return None

            if veronica.is_in_cooldown(agent.name):

                return None

            reply = agent.generate_reply(messages)

            if reply is None:

                veronica.record_fail(agent.name)

            else:

                veronica.record_pass(agent.name)

            return reply

    """



    def guarded_reply(agent: StubAgent, messages: list[dict]) -> Optional[str]:

        # --- system-wide halt check ---

        if veronica.state.current_state == VeronicaState.SAFE_MODE:

            print(f"  [VERONICA] SAFE_MODE active — {agent.name} blocked (system halt)")

            return None



        # --- per-agent cooldown check ---

        if veronica.is_in_cooldown(agent.name):

            remaining = veronica.get_cooldown_remaining(agent.name)

            print(

                f"  [VERONICA] {agent.name} is cooling down "

                f"({remaining:.1f}s remaining) — skipped"

            )

            return None



        # --- call the agent ---

        reply = agent.generate_reply(messages)



        # --- record result ---

        if reply is None:

            cooldown_activated = veronica.record_fail(agent.name)

            fail_count = veronica.get_fail_count(agent.name)

            if cooldown_activated:

                print(

                    f"  [VERONICA] {agent.name} failed ({fail_count} times) "

                    f"— cooldown ACTIVATED"

                )

            else:

                print(

                    f"  [VERONICA] {agent.name} failed ({fail_count} times) "

                    f"— threshold not yet reached"

                )

        else:

            veronica.record_pass(agent.name)

            print(f"  [VERONICA] {agent.name} passed — reply recorded")



        return reply



    return guarded_reply





# ---------------------------------------------------------------------------

# Demo 1: circuit breaker

# ---------------------------------------------------------------------------



def demo_circuit_breaker() -> None:

    """Show fail counter rising then cooldown activating.



    Agent breaks after 2 successful calls.  VERONICA threshold = 3 fails.

    Runs 7 rounds to demonstrate the full progression:

      rounds 1-2  → normal replies

      rounds 3-5  → failures counted (1/3, 2/3, 3/3 → cooldown)

      rounds 6-7  → cooldown blocks without calling the agent

    """

    print("\n" + "=" * 60)

    print("Demo 1: Circuit Breaker")

    print("=" * 60)



    veronica = VeronicaIntegration(

        cooldown_fails=3,

        cooldown_seconds=60,

        backend=MemoryBackend(),

    )

    guarded_reply = make_guarded_reply(veronica)



    agent = StubAgent(name="researcher", fail_after=2)

    messages: list[dict] = [{"role": "user", "content": "Summarise the paper."}]



    for round_num in range(1, 8):

        print(f"\nRound {round_num}:")

        reply = guarded_reply(agent, messages)

        print(f"  reply -> {reply!r}")





# ---------------------------------------------------------------------------

# Demo 2: SAFE_MODE

# ---------------------------------------------------------------------------



def demo_safe_mode() -> None:

    """Orchestrator triggers SAFE_MODE; all agents are blocked.



    Both agents run fine for 2 rounds, then an orchestrator detects an

    anomaly and transitions VERONICA to SAFE_MODE.  Subsequent calls to

    either agent are blocked regardless of their individual fail counts.

    """

    print("\n" + "=" * 60)

    print("Demo 2: SAFE_MODE (system-wide halt)")

    print("=" * 60)



    veronica = VeronicaIntegration(

        cooldown_fails=5,

        cooldown_seconds=60,

        backend=MemoryBackend(),

    )

    guarded_reply = make_guarded_reply(veronica)



    planner = StubAgent(name="planner")

    executor = StubAgent(name="executor")

    messages: list[dict] = [{"role": "user", "content": "Plan and execute the task."}]



    for round_num in range(1, 4):

        print(f"\nRound {round_num}:")



        if round_num == 3:

            print("  [orchestrator] Anomaly detected — triggering SAFE_MODE")

            veronica.state.transition(

                VeronicaState.SAFE_MODE,

                reason="Orchestrator detected runaway cost spike",

            )



        print(f"  planner:")

        guarded_reply(planner, messages)

        print(f"  executor:")

        guarded_reply(executor, messages)



    print("\n  [orchestrator] Anomaly resolved — clearing SAFE_MODE")

    veronica.state.transition(VeronicaState.IDLE, reason="Manual review passed")
    veronica.state.transition(VeronicaState.SCREENING, reason="Resuming after review")



    print("\nRound 4 (after SAFE_MODE cleared):")

    print("  planner:")

    guarded_reply(planner, messages)

    print("  executor:")

    guarded_reply(executor, messages)





# ---------------------------------------------------------------------------

# Demo 3: per-agent tracking

# ---------------------------------------------------------------------------



def demo_per_agent_tracking() -> None:

    """Healthy and broken agents are tracked independently.



    healthy_agent never returns None.

    broken_agent fails immediately (fail_after=0), threshold=2 fails.

    After 2 rounds broken_agent enters cooldown; healthy_agent keeps running.

    """

    print("\n" + "=" * 60)

    print("Demo 3: Per-agent Tracking")

    print("=" * 60)



    veronica = VeronicaIntegration(

        cooldown_fails=2,

        cooldown_seconds=60,

        backend=MemoryBackend(),

    )

    guarded_reply = make_guarded_reply(veronica)



    healthy_agent = StubAgent(name="healthy_agent")

    broken_agent = StubAgent(name="broken_agent", fail_after=0)

    messages: list[dict] = [{"role": "user", "content": "Do the task."}]



    for round_num in range(1, 6):

        print(f"\nRound {round_num}:")

        print(f"  healthy_agent:")

        guarded_reply(healthy_agent, messages)

        print(f"  broken_agent:")

        guarded_reply(broken_agent, messages)





# ---------------------------------------------------------------------------

# main

# ---------------------------------------------------------------------------



def main() -> None:

    demo_circuit_breaker()

    demo_safe_mode()

    demo_per_agent_tracking()



    print("\n" + "=" * 60)

    print("Core pattern (5 lines):")

    print("=" * 60)

    print("""

    def guarded_reply(agent, messages):

        if veronica.state.current_state == VeronicaState.SAFE_MODE:

            return None

        if veronica.is_in_cooldown(agent.name):

            return None

        reply = agent.generate_reply(messages)

        if reply is None:

            veronica.record_fail(agent.name)

        else:

            veronica.record_pass(agent.name)

        return reply

    """)





if __name__ == "__main__":

    main()

