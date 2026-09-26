"""
multi_agent_graph.py
======================
NEW, ADDITIVE MODULE — does not modify any existing file.

Feature 2: Multi-Agent Collaborative Architecture using LangGraph.

Splits the current single-script flow into 4 specialized, stateful agents,
wired together as a graph with a persisted checkpoint (so a run can pause
for Human-in-the-Loop approval and resume later — exactly like your current
WhatsApp manager-approval flow, but formalized).

Agents:
    1. TriageAgent           -> parses incoming telemetry/error logs, decides
                                 severity (Critical / Warning / Info)
    2. MechanicPartsRAGAgent -> uses HybridSearchEngine (see hybrid_search.py)
                                 to find the exact part number / manual section
    3. LogisticsDispatchAgent-> finds nearest open workshop + coordinates route
                                 (reuses your existing Google Maps logic pattern)
    4. ManagerApprovalAgent  -> Human-in-the-Loop gate. Graph PAUSES here via
                                 LangGraph's checkpointer until a human (manager
                                 on WhatsApp) approves/rejects. This is the
                                 same idea as your current APPROVAL_STATES dict,
                                 but now it's a durable graph checkpoint instead
                                 of an in-memory flag.

This file is self-contained and importable without touching main.py.
To wire it into your FastAPI app, see README_INTEGRATION.md.

Dependencies (see requirements-advanced.txt):
    langgraph
    langchain-core
"""

from typing import TypedDict, List, Dict, Any, Optional, Literal
import time

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver


# ---------------------------------------------------------------------------
# Shared State — this dict travels through every node in the graph. Every
# agent reads what it needs and writes back its own findings.
# ---------------------------------------------------------------------------
class FleetIncidentState(TypedDict, total=False):
    incident_id: str
    vehicle_id: str
    location: str
    destination: str
    issue_type: str
    cargo_type: str
    raw_error_log: str

    # Triage output
    severity: str                 # "Critical" | "Warning" | "Info"
    triage_notes: str

    # Mechanic/Parts RAG output
    matched_part_number: Optional[str]
    matched_manual_section: Optional[str]
    part_confidence: float

    # Logistics output
    nearest_workshop: Optional[Dict[str, Any]]
    eta_minutes: Optional[float]

    # HITL / Manager Approval
    approval_status: Literal["pending", "approved", "rejected"]
    manager_notes: Optional[str]

    # Final outcome (set by dispatch_confirmed / dispatch_cancelled nodes)
    final_status: Optional[str]

    # Trace log (kept compatible with your existing AgentTrace shape)
    traces: List[Dict[str, Any]]


def _trace(state: FleetIncidentState, step_name: str, agent_role: str, output: dict) -> dict:
    entry = {
        "step_name": step_name,
        "agent_role": agent_role,
        "status": "completed",
        "timestamp": time.time(),
        "output_payload": output,
    }
    return entry


# ---------------------------------------------------------------------------
# Agent 1: Triage Agent
# ---------------------------------------------------------------------------
def triage_agent(state: FleetIncidentState) -> FleetIncidentState:
    log = (state.get("raw_error_log") or state.get("issue_type") or "").lower()

    critical_signals = ["engine seizure", "brake failure", "fire", "smoke",
                         "cargo spill", "collision", "no oil pressure"]
    warning_signals = ["overheat", "leak", "warning light", "vibration", "noise"]

    if any(s in log for s in critical_signals):
        severity = "Critical"
    elif any(s in log for s in warning_signals):
        severity = "Warning"
    else:
        severity = "Info"

    state["severity"] = severity
    state["triage_notes"] = f"Classified as {severity} based on reported symptoms."
    state.setdefault("traces", []).append(
        _trace(state, "triage", "TriageAgent", {"severity": severity})
    )
    return state


# ---------------------------------------------------------------------------
# Agent 2: Mechanic / Parts RAG Agent
# ---------------------------------------------------------------------------
def make_mechanic_parts_agent(hybrid_search_engine=None):
    """Factory so you can inject your real HybridSearchEngine (from
    hybrid_search.py) loaded with your actual workshop manuals / inventory."""

    def mechanic_parts_agent(state: FleetIncidentState) -> FleetIncidentState:
        query = state.get("raw_error_log") or state.get("issue_type", "")

        if hybrid_search_engine is not None:
            results = hybrid_search_engine.search(query, top_k=1)
            top = results[0] if results else None
            part_number = top.get("part_number") if top else None
            manual_section = top.get("id") if top else None
            confidence = top.get("rerank_score", 0.0) if top else 0.0
        else:
            # No engine injected yet -> safe placeholder, never invents a
            # part number (guards against hallucination, see self_rag.py)
            part_number, manual_section, confidence = None, None, 0.0

        state["matched_part_number"] = part_number
        state["matched_manual_section"] = manual_section
        state["part_confidence"] = confidence
        state.setdefault("traces", []).append(
            _trace(state, "mechanic_parts_rag", "MechanicPartsRAGAgent",
                   {"part_number": part_number, "confidence": confidence})
        )
        return state

    return mechanic_parts_agent


# ---------------------------------------------------------------------------
# Agent 3: Logistics & Dispatch Agent
# ---------------------------------------------------------------------------
def make_logistics_agent(maps_lookup_fn=None):
    """Inject your existing `_get_heavy_service_center_intelligence` /
    `_fetch_google_maps_route` logic here so this agent reuses it instead of
    duplicating Google Maps calls."""

    def logistics_agent(state: FleetIncidentState) -> FleetIncidentState:
        if maps_lookup_fn is not None:
            workshop_info = maps_lookup_fn(state.get("location"), state.get("destination"))
        else:
            workshop_info = {"name": "Nearest Authorized Workshop (placeholder)", "eta_minutes": None}

        state["nearest_workshop"] = workshop_info
        state["eta_minutes"] = workshop_info.get("eta_minutes")
        state.setdefault("traces", []).append(
            _trace(state, "logistics_dispatch", "LogisticsDispatchAgent", workshop_info)
        )
        return state

    return logistics_agent


# ---------------------------------------------------------------------------
# Agent 4: Manager Approval Agent (Human-in-the-Loop gate)
# ---------------------------------------------------------------------------
def manager_approval_agent(state: FleetIncidentState) -> FleetIncidentState:
    """This node sets status to 'pending' and the graph run STOPS here
    (see `interrupt_before` in build_fleet_graph). Your WhatsApp webhook
    handler resumes the graph later by calling:
        graph.update_state(config, {"approval_status": "approved"/"rejected"})
        graph.invoke(None, config)   # resumes from checkpoint
    """
    state["approval_status"] = state.get("approval_status", "pending")
    state.setdefault("traces", []).append(
        _trace(state, "manager_approval", "ManagerApprovalAgent",
               {"approval_status": state["approval_status"]})
    )
    return state


def _route_after_approval(state: FleetIncidentState) -> str:
    if state.get("approval_status") == "approved":
        return "dispatch_confirmed"
    if state.get("approval_status") == "rejected":
        return "dispatch_cancelled"
    return END  # still pending -> graph pauses (interrupt) here


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------
def build_fleet_graph(hybrid_search_engine=None, maps_lookup_fn=None):
    graph = StateGraph(FleetIncidentState)

    graph.add_node("triage", triage_agent)
    graph.add_node("mechanic_parts_rag", make_mechanic_parts_agent(hybrid_search_engine))
    graph.add_node("logistics_dispatch", make_logistics_agent(maps_lookup_fn))
    graph.add_node("manager_approval", manager_approval_agent)
    graph.add_node("dispatch_confirmed", lambda s: {**s, "final_status": "DISPATCH_CONFIRMED"})
    graph.add_node("dispatch_cancelled", lambda s: {**s, "final_status": "DISPATCH_CANCELLED"})

    graph.set_entry_point("triage")
    graph.add_edge("triage", "mechanic_parts_rag")
    graph.add_edge("mechanic_parts_rag", "logistics_dispatch")
    graph.add_edge("logistics_dispatch", "manager_approval")
    graph.add_conditional_edges(
        "manager_approval",
        _route_after_approval,
        {
            "dispatch_confirmed": "dispatch_confirmed",
            "dispatch_cancelled": "dispatch_cancelled",
            END: END,
        },
    )
    graph.add_edge("dispatch_confirmed", END)
    graph.add_edge("dispatch_cancelled", END)

    # Checkpointer = durable state, so the graph can pause at manager_approval
    # and resume minutes/hours later when the WhatsApp reply comes in,
    # without losing any of the earlier agents' work.
    checkpointer = MemorySaver()  # swap for SqliteSaver/PostgresSaver in production
    compiled = graph.compile(checkpointer=checkpointer, interrupt_before=["manager_approval"])
    return compiled


# ---------------------------------------------------------------------------
# Example usage (does not run automatically on import)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    fleet_graph = build_fleet_graph()
    config = {"configurable": {"thread_id": "incident-demo-1"}}

    initial_state: FleetIncidentState = {
        "incident_id": "demo-1",
        "vehicle_id": "BR01GP9621",
        "location": "NH19, Barh, Bihar",
        "destination": "Authorized Heavy Workshop Hub",
        "issue_type": "engine overheat warning light",
        "raw_error_log": "Coolant temp warning light + overheat",
        "cargo_type": "General",
    }

    result = fleet_graph.invoke(initial_state, config)
    print("Graph paused for approval. Current state:", result)

    # Simulate manager approving via WhatsApp later:
    fleet_graph.update_state(config, {"approval_status": "approved"})
    final_result = fleet_graph.invoke(None, config)
    print("Final result after approval:", final_result)
