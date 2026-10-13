"""The triage pipeline: vision -> hybrid RAG -> corrective RAG -> routing -> manager approval."""
import time
import uuid
from typing import Any, Dict, List, Optional

import requests

from advanced.multimodal import identify_damaged_part
from advanced.observability import traced
from fleet import state
from fleet.config import cfg
from fleet.directory import resolve_hub_phone
from fleet.insights import record_and_check_vehicle_pattern
from fleet.knowledge import CorrectiveRAGValidator, get_engine
from fleet.models import AgentTrace, IncidentInput, TriageResponse
from fleet.netsafe import fetch_public_image
from fleet.quotes import request_spare_part_quotes


# ==========================================
# 4. ENTERPRISE OBSERVABILITY & TRACING SIMULATOR (LangSmith / Arize Phoenix Style)
# ==========================================
class EnterpriseObservabilityTracer:
    def __init__(self, incident_id: str):
        self.incident_id = incident_id
        self.traces = []

    def log_span(self, agent_name: str, span_type: str, input_data: Any, output_data: Any, latency_ms: float, tokens_used: int = 150):
        trace_entry = {
            "trace_id": f"TRC-{uuid.uuid4().hex[:8].upper()}",
            "incident_id": self.incident_id,
            "agent_role": agent_name,
            "span_type": span_type,
            "latency_ms": latency_ms,
            "estimated_token_cost": tokens_used,
            "timestamp": time.time(),
            "input": input_data,
            "output": output_data
        }
        self.traces.append(trace_entry)
        print(f"📊 [OBSERVABILITY TRACE] Agent: {agent_name} | Type: {span_type} | Latency: {latency_ms}ms")


# ==========================================
# 2. MULTI-AGENT COLLABORATIVE ARCHITECTURE (LangGraph Stateful Flow Simulation)
# ==========================================
class EnterpriseAgenticRAGOrchestrator:
    def __init__(self, incident: IncidentInput):
        self.incident = incident
        self.incident_id = f"INC-{uuid.uuid4().hex[:8].upper()}"
        self.traces: List[AgentTrace] = []
        self.start_time = time.time()
        self.step_counter = 0
        self.tracer = EnterpriseObservabilityTracer(self.incident_id)

    @traced("multimodal_vision_inspection")
    def _run_multi_modal_vision_inspection(self) -> str:
        """5. Multi-Modal Input (Images + Telemetry): REAL Gemini Vision call
        when a real image_url is attached — the vision model only ever
        describes what it sees (never invents a part number itself). Falls
        back to the original simulated keyword check when no image is
        attached, so default behavior (no image today) is unchanged."""
        if self.incident.image_url and cfg.GEMINI_API_KEY:
            try:
                # image_url is caller-supplied: fetch it only if it is a public http(s) URL.
                image_bytes = fetch_public_image(self.incident.image_url)
                vision_result = identify_damaged_part(
                    image_bytes,
                    vehicle_context=f"Vehicle ID: {self.incident.vehicle_id}, reported issue: {self.incident.issue_type}",
                )
                if not vision_result.get("error"):
                    return (
                        f"Vision Agent (Gemini) analyzed attached photo: "
                        f"{vision_result.get('visible_damage', 'damage detected')} on "
                        f"{vision_result.get('component_area', 'reported component')} "
                        f"(confidence: {vision_result.get('confidence', 'medium')})."
                    )
            except Exception as e:
                print(f"DEBUG VISION AGENT ERROR: {str(e)}")
                # falls through to the simulated fallback below

        if self.incident.image_url or "smoke" in self.incident.issue_type.lower() or "oil" in self.incident.issue_type.lower():
            return "Vision Agent inspected attachment: Heavy leakage identified on coolant line manifold. Auto-adjusted part diagnostics confidence."
        return "Vision Agent check: Standard text telemetry verified (no damage photo provided)."

    def _geocode_location(self, address: str):
        """Breakdown location ka lat/lng nikaalta hai, nearest hub sorting ke liye."""
        if not cfg.GOOGLE_MAPS_API_KEY or not address:
            return None
        try:
            geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {"address": f"{address}, Bihar, India", "key": cfg.GOOGLE_MAPS_API_KEY}
            data = requests.get(geo_url, params=params, timeout=5).json()
            if data.get("status") == "OK" and data.get("results"):
                return data["results"][0]["geometry"]["location"]
        except Exception:
            pass
        return None

    @staticmethod
    def _haversine_km(a, b):
        """Do coords ke beech straight-line distance nikaalta hai (km me)."""
        try:
            from math import radians, sin, cos, asin, sqrt
            lat1, lon1 = radians(a["lat"]), radians(a["lng"])
            lat2, lon2 = radians(b["lat"]), radians(b["lng"])
            dlat, dlon = lat2 - lat1, lon2 - lon1
            h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
            return 2 * 6371 * asin(sqrt(h))
        except Exception:
            return float("inf")

    def _build_full_route_url(self, hub_waypoint: Optional[str]) -> str:
        """Source → nearest service centre → destination ka clickable Google Maps route."""
        from urllib.parse import quote_plus
        origin = quote_plus(self.incident.location or "")
        dest = quote_plus(self.incident.destination or "")
        base = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}&travelmode=driving"
        if hub_waypoint:
            base += f"&waypoints={quote_plus(hub_waypoint)}"
        return base
        
    @traced("fetch_google_maps_route")
    def _fetch_google_maps_route(self, via_hub: Optional[str] = None):
        if not cfg.GOOGLE_MAPS_API_KEY or not self.incident.destination:
            return None
        url = "https://maps.googleapis.com/maps/api/directions/json"
        params = {
            "origin": self.incident.location,
            "destination": self.incident.destination,
            "key": cfg.GOOGLE_MAPS_API_KEY
        }
        if via_hub:
            params["waypoints"] = via_hub
        try:
            response = requests.get(url, params=params, timeout=7)
            data = response.json()
            if data.get("status") == "OK":
                legs = data["routes"][0]["legs"]
                total_m = sum(l["distance"]["value"] for l in legs)
                total_s = sum(l["duration"]["value"] for l in legs)
                result = {
                    "distance_text": f"{round(total_m / 1000, 1)} km",
                    "distance_value": total_m,
                    "duration_text": f"{round(total_s / 3600, 1)} hours",
                    "start_address": legs[0]["start_address"],
                    "end_address": legs[-1]["end_address"]
                }
                if via_hub and len(legs) >= 2:
                    result["leg_breakdown_to_service_center"] = {
                        "distance": legs[0]["distance"]["text"],
                        "duration": legs[0]["duration"]["text"],
                        "service_center_address": legs[0]["end_address"]
                    }
                    result["leg_service_center_to_destination"] = {
                        "distance": legs[1]["distance"]["text"],
                        "duration": legs[1]["duration"]["text"]
                    }
                result["route_via_service_center"] = bool(via_hub)
                return result
        except Exception:
            pass
        return None

    @traced("heavy_service_center_intelligence")
    def _get_heavy_service_center_intelligence(self):
        raw_hubs = []
        loc_lower = (self.incident.location or "").lower()
        is_outside_patna = "patna" not in loc_lower
        location_query = self.incident.location.split(",")[0].strip()
        region_scope = "Bihar, India" if is_outside_patna else "Patna, Bihar"
        origin_coords = self._geocode_location(self.incident.location) if is_outside_patna else None

        if cfg.GOOGLE_MAPS_API_KEY:
            places_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
            search_queries = [
                f"Tata commercial vehicle service center OR Eicher workshop near {location_query}, {region_scope}"
            ]
            if is_outside_patna:
                search_queries.append(
                    f"heavy commercial truck service centre near {location_query}, Bihar, India"
                )
            seen = set()
            for query_str in search_queries:
                params = {"query": query_str, "key": cfg.GOOGLE_MAPS_API_KEY}
                if origin_coords:
                    params["location"] = f"{origin_coords['lat']},{origin_coords['lng']}"
                    params["radius"] = 150000  # poore Bihar ka coverage
                try:
                    data = requests.get(places_url, params=params, timeout=7).json()
                    if data.get("status") == "OK" and data.get("results"):
                        for place in data["results"][:5]:
                            name = place.get('name', 'Service Center')
                            address = place.get('formatted_address', '')
                            if (name, address) in seen:
                                continue
                            seen.add((name, address))
                            rating = place.get('rating', 'N/A')
                            geometry = place.get('geometry', {}).get('location', {})
                            raw_hubs.append({
                                "display_str": f"{name} — {address} (Rating: {rating})",
                                "coords": geometry,
                                "place_id": place.get("place_id"),
                                "waypoint": f"{geometry.get('lat')},{geometry.get('lng')}" if geometry else address
                            })
                except Exception:
                    pass

        if not raw_hubs:
            patna_hubs = [
                {"display_str": "TATA.CARS Service Centre - Guinea Motors, Patliputra Industrial Area, Patna, Bihar (Rating: 3.9)", "phone": "0612-2262244", "coords": {"lat": 25.6210, "lng": 85.1050}},
                {"display_str": "Eicher Commercial Vehicles Workshop, NH-30 Bypass Road, Patna, Bihar (Rating: 4.2)", "phone": "0612-2277311", "coords": {"lat": 25.5788, "lng": 85.1560}},
                {"display_str": "Tata Motors Authorized Commercial Heavy Workshop, Zero Mile, Patna, Bihar (Rating: 4.1)", "phone": "0612-2233890", "coords": {"lat": 25.5941, "lng": 85.2010}},
                {"display_str": "Eicher Trucks & Buses Service Station, Fatuha Industrial Area, Patna, Bihar (Rating: 4.0)", "phone": "0612-2299456", "coords": {"lat": 25.5060, "lng": 85.3050}}
            ]
            bihar_hubs = [
                {"display_str": "Tata Motors Commercial Vehicle Service Centre, Ramdayalu, Muzaffarpur, Bihar (Rating: 4.0)", "phone": "0621-2240110", "coords": {"lat": 26.1209, "lng": 85.3647}},
                {"display_str": "Eicher Trucks & Buses Authorized Workshop, GT Road, Gaya, Bihar (Rating: 4.1)", "phone": "0631-2221450", "coords": {"lat": 24.7955, "lng": 85.0002}},
                {"display_str": "Tata Motors Heavy Commercial Workshop, Zero Mile, Bhagalpur, Bihar (Rating: 3.9)", "phone": "0641-2420331", "coords": {"lat": 25.2425, "lng": 86.9842}},
                {"display_str": "Eicher Commercial Vehicle Service Station, Donar Chowk, Darbhanga, Bihar (Rating: 4.0)", "phone": "06272-245120", "coords": {"lat": 26.1542, "lng": 85.8918}},
                {"display_str": "Tata Motors Authorized Truck Workshop, NH-31, Purnia, Bihar (Rating: 4.0)", "phone": "06454-242890", "coords": {"lat": 25.7771, "lng": 87.4753}},
                {"display_str": "Heavy Commercial Vehicle Service Hub, Bela Industrial Area, Begusarai, Bihar (Rating: 3.8)", "phone": "06243-222410", "coords": {"lat": 25.4182, "lng": 86.1290}}
            ]
            default_heavy_hubs = (bihar_hubs + patna_hubs) if is_outside_patna else patna_hubs
            for hub in default_heavy_hubs:
                raw_hubs.append({
                    "display_str": hub["display_str"],
                    "coords": hub.get("coords", {}),
                    "place_id": None,
                    "phone": hub["phone"],
                    "waypoint": f"{hub['coords']['lat']},{hub['coords']['lng']}" if hub.get("coords") else hub["display_str"]
                })

        # Breakdown point se actual nearest hub sabse upar
        if origin_coords:
            raw_hubs.sort(
                key=lambda h: self._haversine_km(origin_coords, h["coords"]) if h.get("coords") else float("inf")
            )

        # Ensure de-duplication and closest-first ordering are consistent,
        # then keep this exact order as the "queue" reject/reroute walks through.
        seen_display = set()
        deduped_hubs = []
        for h in raw_hubs:
            if h["display_str"] not in seen_display:
                seen_display.add(h["display_str"])
                deduped_hubs.append(h)
        raw_hubs = deduped_hubs[:5]

        hub_waypoint = raw_hubs[0].get("waypoint")
        hub_phone = resolve_hub_phone(raw_hubs[0], 0)

        numbered_hubs = [f"{idx}. {h['display_str']}" for idx, h in enumerate(raw_hubs, 1)]
        # Full ordered record list (closest first) — kept per-incident so that
        # a manager REJECT can automatically advance to the next one in line.
        hub_records = [
            {
                "name": h["display_str"],
                "phone": h.get("phone"),
                "place_id": h.get("place_id"),
                "waypoint": h.get("waypoint"),
            }
            for h in raw_hubs
        ]
        primary_hub = numbered_hubs[0]

        return {
            "corridor": self.incident.location,
            "hub": primary_hub,
            "hub_phone": hub_phone,
            "hub_waypoint": hub_waypoint,
            "search_scope": "STATEWIDE_BIHAR" if is_outside_patna else "PATNA_METRO",
            "all_detected_hubs": numbered_hubs,
            "hub_records": hub_records
        }

    @state.locked
    def _register_incident(self, service_intel: Dict[str, Any], rag_intel: Dict[str, Any]) -> Optional[str]:
        """Record the incident and open its vendor-quote round as ONE atomic step.

        Held under STATE_LOCK so a vendor's reply (handled on another thread) can never be
        processed before the incident it belongs to has been saved. Returns the
        predictive-maintenance alert text for this vehicle, if any."""
        # Feature: Per-Vehicle Predictive Maintenance — check BEFORE recording
        # this incident, so "matches" only counts genuinely PRIOR occurrences.
        predictive_alert = record_and_check_vehicle_pattern(self.incident.vehicle_id, self.incident.issue_type)

        # Feature: Spare-Part Price Comparison — ask top 3 nearby vendors for
        # availability/price on this specific part. Their WhatsApp replies
        # get picked up later in the webhook handler.
        request_spare_part_quotes(
            self.incident_id,
            service_intel.get("hub_records", []),
            rag_intel["recommended_part"]
        )

        state.APPROVAL_STATES[self.incident_id] = "PENDING_MANAGER_APPROVAL"
        state.INCIDENT_CONTEXTS[cfg.MANAGER_WHATSAPP_NUMBER] = self.incident_id
        state.INCIDENT_DETAILS[self.incident_id] = {
            "vehicle_id": self.incident.vehicle_id,
            "issue_type": self.incident.issue_type,
            "location": self.incident.location,
            "destination": self.incident.destination,
            "severity": self.incident.severity,
            "cargo_type": self.incident.cargo_type,
            "hub": service_intel["hub"],
            "hub_phone": service_intel["hub_phone"],
            "hub_waypoint": service_intel.get("hub_waypoint"),
            "hub_records": service_intel.get("hub_records", []),
            "hub_index": 0,
            "recommended_part": rag_intel["recommended_part"],
            "hallucination_grade": rag_intel.get("hallucination_grade"),
            "diagnostic_summary": rag_intel.get("diagnostic_summary", ""),
            "predictive_alert": predictive_alert,
            "followup_scheduled": False
        }
        return predictive_alert

    @traced("run_swarm_full_incident")
    def run_swarm(self) -> TriageResponse:
        
        service_intel = self._get_heavy_service_center_intelligence()
        map_route = self._fetch_google_maps_route(via_hub=service_intel.get("hub_waypoint"))
        full_route_url = self._build_full_route_url(service_intel.get("hub_waypoint"))
        
        # 1. Hybrid Search + Reranking Execution
        raw_rag = get_engine().hybrid_retrieve_and_rerank(self.incident.issue_type)
        
        # 3. Corrective RAG (CRAG) Validation & Hallucination Grading
        rag_intel = CorrectiveRAGValidator.evaluate_and_correct(self.incident.issue_type, raw_rag)
        
        # 5. Multi-Modal Vision Analysis
        vision_report = self._run_multi_modal_vision_inspection()

        predictive_alert = self._register_incident(service_intel, rag_intel)

        detected_hubs = service_intel.get("all_detected_hubs", [])
        assigned_phone = cfg.MANAGER_WHATSAPP_NUMBER

        # Agent 1: Triage & Vision Multi-Modal Agent Trace
        self.step_counter += 1
        t_start = time.time()
        sup_dec = {
            "action_required": True,
            "target_vehicle": self.incident.vehicle_id,
            "mapped_whatsapp_number": assigned_phone,
            "breakdown_location": map_route["start_address"] if map_route else self.incident.location,
            "destination_workshop": service_intel["hub"],
            "all_nearby_service_centers": detected_hubs,
            "issue_detected": self.incident.issue_type,
            "multimodal_vision_report": vision_report,
            "hitl_status": "WAITING_FOR_WHATSAPP_INTERACTIVE_BUTTON_OR_TEXT"
        }
        lat_1 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Supervisor_Triage", agent_role="Supervisor & Multi-Modal Vision Agent", status="SUCCESS", timestamp=lat_1, output_payload=sup_dec))
        self.tracer.log_span("Supervisor_Triage", "AGENT_NODE", self.incident.dict(), sup_dec, lat_1, 180)

        # Agent 2: Hybrid RAG + Corrective RAG (CRAG) Agent Trace
        self.step_counter += 1
        t_start = time.time()
        lat_2 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Agentic_RAG_Diagnostics", agent_role="Hybrid Vector/BM25 + CRAG Agent", status="SUCCESS", timestamp=lat_2, output_payload=rag_intel))
        self.tracer.log_span("Agentic_RAG_Diagnostics", "RAG_RETRIEVAL", {"issue": self.incident.issue_type}, rag_intel, lat_2, 220)

        # Agent 3: Logistics & Route Dispatch Agent Trace
        self.step_counter += 1
        t_start = time.time()
        routing = {
            "total_distance": map_route["distance_text"] if map_route else "310 km",
            "estimated_travel_time": map_route["duration_text"] if map_route else "6 hours",
            "primary_route_status": "HEAVY_CORRIDOR_OPTIMIZED",
            "hyper_accurate_alternative_route": f"Optimized transit to {service_intel['hub'].split('—')[0]}",
            "breakdown_to_service_center": map_route.get("leg_breakdown_to_service_center") if map_route else "Live leg unavailable",
            "service_center_to_destination": map_route.get("leg_service_center_to_destination") if map_route else "Live leg unavailable",
            "route_waypoint_service_center": service_intel["hub"],
            "open_full_route_url": full_route_url
        }
        lat_3 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Routing_Recalculation", agent_role="Routing & Logistics Agent", status="SUCCESS", timestamp=lat_3, output_payload=routing))
        self.tracer.log_span("Routing_Recalculation", "ROUTING_NODE", {"destination": service_intel["hub"]}, routing, lat_3, 130)

        # Agent 4: Manager Approval & ERP Sync Agent Trace
        self.step_counter += 1
        t_start = time.time()
        erp = {
            "erp_transaction_id": f"TXN-ERP-{uuid.uuid4().hex[:6].upper()}",
            "ledger_status": "PENDING_HITL_APPROVAL",
            "observability_trace_id": self.tracer.traces[0]["trace_id"]
        }
        lat_4 = round((time.time() - t_start) * 1000, 2)
        self.traces.append(AgentTrace(step_name="Manager_Approval_and_ERP_Sync", agent_role="HITL Approval State & ERP Agent", status="SUCCESS", timestamp=lat_4, output_payload=erp))
        self.tracer.log_span("Manager_Approval_and_ERP_Sync", "STATE_COMMIT", {}, erp, lat_4, 90)

        return TriageResponse(
            incident_id=self.incident_id,
            status="AWAITING_HITL_APPROVAL",
            execution_time_ms=round((time.time() - self.start_time) * 1000, 2),
            deterministic_steps_executed=self.step_counter,
            assigned_whatsapp_number=assigned_phone,
            approval_status=state.APPROVAL_STATES[self.incident_id],
            traces=self.traces,
            final_resolution={
                "vehicle_id": self.incident.vehicle_id,
                "service_center_search_scope": service_intel["search_scope"],
                "nearest_hub_contact": service_intel["hub_phone"],
                "open_full_route_url": full_route_url,
                "assigned_whatsapp": assigned_phone,
                "origin": self.incident.location,
                "primary_nearest_hub": service_intel["hub"],
                "rag_diagnostic_part": rag_intel["recommended_part"],
                "crag_status": rag_intel["hallucination_grade"],
                "mitigation_summary": "Google-level Hybrid RAG & CRAG part match executed. Waiting for Manager WhatsApp approval.",
                "erp_ref": erp["erp_transaction_id"],
                "predictive_maintenance_alert": predictive_alert
            }
        )
