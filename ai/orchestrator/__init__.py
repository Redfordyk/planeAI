"""planeAI Multi-Agent Orchestrator (phases 7-12).

This subpackage layers a coordinator + 7 specialised agents on top of
the base AI features (RAG, single-issue agent worker, token guards):

    decision   — Decision Layer matrix (AUTO/NOTIFY/CONFIRM/ESCALATE)
    breaker    — circuit breaker + kill-switch (TZ 11.2)
    events     — typed events flowing from Plane into the orchestrator
    velocity   — TeamVelocity recording from issue completion
    planner    — goal -> issue tree
    monitor    — heuristic risk detection
    executor   — assignment by load
    escalator  — critical risk -> alert task with options
    analyst    — velocity / bottleneck insights
    communicator — weekly status report
    router     — ORCHESTRATOR: event -> agent dispatch
    api        — DRF views for goals / risks / kill-switch

Every agent funnels its decisions through ``log_action`` (writes
``AgentAction`` row) so the activity feed is the single source of
truth for "what did the system do, and why".
"""
