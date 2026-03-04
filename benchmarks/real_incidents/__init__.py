"""Real incident benchmarks for veronica-core.

Each module simulates a documented real-world LLM runaway incident,
comparing uncontained baseline behavior against veronica containment.

Incidents:
    incident_01_openai_loop   -- GPT-4 infinite self-correction loop
    incident_02_cost_spike    -- $552 runaway API bill from recursive agent
    incident_03_websocket_ddos -- 47k tokens/sec WebSocket flood
    incident_04_semantic_echo -- Semantic loop echo chamber (same answer repeated)
    incident_05_multi_tool    -- Tool cascade: one tool spawns N more tools
"""
