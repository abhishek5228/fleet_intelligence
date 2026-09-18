# 🚛 Hyper-Local Fleet Swarm Intelligence

**A deterministic multi-agent "self-healing" logistics engine that automatically re-plans a truck's journey the moment it breaks down — with live Google Maps rerouting, procurement, legal compliance, and ERP sync all handled by a coordinated swarm of agents.**


---

## 📖 Table of Contents

- [The Idea](#-the-idea)
- [How It Works](#-how-it-works)
- [The Agent Swarm Pipeline](#-the-agent-swarm-pipeline)
- [Handling Every Case](#-handling-every-case)
- [Tech Stack](#-tech-stack)
- [Project Structure](#-project-structure)
- [Getting Started](#-getting-started)
- [Environment Variables](#-environment-variables)
- [API Reference](#-api-reference)
- [Example Request & Response](#-example-request--response)
- [Roadmap](#-roadmap)
- [Contributing](#-contributing)
- [License](#-license)

---

## 💡 The Idea

Fleet operators (logistics, construction, freight) lose hours every time a **heavy vehicle breaks down mid-route**. Today that recovery is manual and slow:

1. A driver calls a dispatcher.
2. The dispatcher manually checks traffic/road conditions.
3. Someone manually finds a replacement part or vendor.
4. Someone else drafts the compliance/insurance paperwork.
5. Finally, someone updates the ERP/fleet ledger — often a day later.

**Fleet Swarm Intelligence** compresses this entire chain into a **single API call**, executed in milliseconds by a deterministic pipeline of specialized "agents." Instead of one monolithic function doing everything, the problem is broken into the same roles a real operations team would have — a **Supervisor**, a **Routing** specialist, a **Procurement** specialist, a **Legal/Compliance** specialist, and an **ERP** specialist — each contributing its own piece of the resolution, with every step timed and traced for auditability.

This is a **hyper-local** system: it ships with real corridor/hub knowledge for the **Patna, Bihar region** (NH-31, Gandhi Setu, Danapur, Bihta, Zero Mile) as a first-class fallback, while remaining fully generic for any location once a Google Maps API key is supplied.

---

## ⚙️ How It Works

At a high level:

```
Driver/Dispatcher reports breakdown
            │
            ▼
   React Frontend (incident form)
            │  POST /api/triage
            ▼
     FastAPI Backend
            │
            ▼
 HyperLocalSwarmOrchestrator
   runs 5 agents in sequence
            │
            ▼
 Structured TriageResponse
 (every agent's trace + final plan)
            │
            ▼
   Frontend renders the full
   multi-agent execution trace
```

The backend is intentionally **deterministic** (not an LLM-driven agent loop) — every "agent" is a well-defined function that always produces a structured, predictable output. This makes the system fast (milliseconds, not seconds), auditable, and safe to run in production without worrying about hallucinated actions.

---

## 🤖 The Agent Swarm Pipeline

Every incident is processed by five agents, run in order, each one logged as a trace with its own execution time:

| # | Agent | Responsibility |
|---|-------|-----------------|
| 1 | **Supervisor Agent** | Triages the incident, calculates a risk score based on severity, and assigns the rest of the swarm to the job. |
| 2 | **Routing Agent** | Computes the real alternate route using the **Google Maps Directions API** (if configured), or falls back to a curated **Bihar corridor database** with pre-mapped bypass routes and estimated delay savings. |
| 3 | **Procurement Agent** | Identifies the nearest operational hub/depot and confirms replacement stock is locked and ready for dispatch. |
| 4 | **Legal Agent** | Auto-generates an emergency transit/towing permit reference and invokes the relevant SLA/force-majeure clause. |
| 5 | **ERP Sync Agent** | Commits the resolution to the fleet ledger with a transaction ID, trip distance, and updated fleet status. |

The API response includes **every agent's payload, status, and timestamp**, so the frontend can render a full execution trace — not just a final answer.

---

## 🧩 Handling Every Case

The engine is built to degrade gracefully instead of failing:

- **✅ Google Maps API key configured** → Routing Agent uses live Directions API data (real distance, duration, start/end coordinates, addresses).
- **⚠️ No API key / API call fails / no destination provided** → Routing Agent automatically falls back to the built-in **Bihar corridor knowledge base** (`LOCATION_MAP`), matching against known highways/landmarks (`NH-31`, `Gandhi Setu`, `Danapur`, `Bihta`, `Zero Mile`).
- **🌍 Unknown/unlisted location** → A generic peripheral-bypass route and regional hub are synthesized on the fly from the cargo type and location, so the pipeline **never dead-ends**.
- **🚨 Severity = `CRITICAL`** → Risk score is set high (`0.94`) to reflect urgency in the Supervisor's decision payload.
- **🟡 Severity = `MODERATE`** → Risk score defaults to `0.70`.
- **📦 Any cargo type** → Procurement and ERP payloads dynamically reference the submitted cargo type, so the response is never generic boilerplate.
- **🧾 Optional destination field** → If left blank, it defaults to `"Hajipur Industrial Area Workshop"`, preventing `422` validation errors from the API.

---

## 🛠 Tech Stack

**Backend**
- [FastAPI](https://fastapi.tiangolo.com/) — API framework
- [Pydantic](https://docs.pydantic.dev/) — request/response schema validation
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [Requests](https://requests.readthedocs.io/) — Google Maps Directions API calls

**Frontend**
- [React 19](https://react.dev/)
- [Tailwind CSS 3](https://tailwindcss.com/)
- [Axios](https://axios-http.com/)
- [Lucide React](https://lucide.dev/) — icons
- Create React App (`react-scripts`)

---

## 📁 Project Structure

```
fleet-swarm-project/
├── backend/
│   ├── main.py              # FastAPI app + HyperLocalSwarmOrchestrator (the swarm logic)
│   └── requirements.txt     # fastapi, uvicorn, pydantic, requests
│
└── frontend/
    ├── public/
    ├── src/
    │   ├── App.js            # Incident form + live trace visualization
    │   ├── App.css
    │   └── index.js
    ├── package.json
    └── tailwind.config.js
```

---

## 🚀 Getting Started

### Prerequisites
- Python 3.9+
- Node.js 18+ and npm
- (Optional) A [Google Maps Directions API key](https://developers.google.com/maps/documentation/directions/get-api-key)

### 1. Clone the repo

```bash
git clone https://github.com/projects2829/fleet-swarm-project.git
cd fleet-swarm-project
```

### 2. Run the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

# optional, enables live routing:
export GOOGLE_MAPS_API_KEY=your_api_key_here

uvicorn main:app --reload --port 8000
```

The API will be live at `http://localhost:8000`, with interactive docs at `http://localhost:8000/docs`.

### 3. Run the frontend

```bash
cd frontend
npm install

# point the frontend at your local backend:
echo "REACT_APP_API_URL=http://localhost:8000" > .env

npm start
```

The app opens at `http://localhost:3000`.

---

## 🔑 Environment Variables

| Variable | Where | Description |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Backend | Enables live routing via Google Maps Directions API. If omitted, the system automatically uses the built-in Bihar corridor fallback. |
| `REACT_APP_API_URL` | Frontend | Base URL of the backend API. Defaults to the deployed Render URL if not set. |

---

## 📡 API Reference

### `POST /api/triage`

Triggers the full agent swarm for a breakdown incident.

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `vehicle_id` | string | ✅ | Fleet unit identifier |
| `location` | string | ✅ | Breakdown location (origin) |
| `destination` | string | ❌ | Target workshop/hub (defaults to Hajipur Workshop) |
| `issue_type` | string | ✅ | Description of the mechanical/operational issue |
| `severity` | string | ✅ | `CRITICAL` or `MODERATE` |
| `cargo_type` | string | ✅ | Type of cargo being carried |

**Response**: `TriageResponse` — includes `incident_id`, total `execution_time_ms`, number of `deterministic_steps_executed`, the full list of agent `traces`, and a `final_resolution` summary.

### `GET /api/health`

Simple health-check endpoint, returns engine status.

---

## 🧪 Example Request & Response

**Request**

```json
POST /api/triage
{
  "vehicle_id": "TRUCK-BR-01-9922",
  "location": "Zero Mile, Patna",
  "destination": "Hajipur Industrial Area Workshop",
  "issue_type": "Engine Overheat & Transmission Breakdown",
  "severity": "CRITICAL",
  "cargo_type": "Heavy Construction Steel Rods"
}
```

**Response (shape)**

```json
{
  "incident_id": "INC-A1B2C3D4",
  "status": "RESOLVED_VIA_DYNAMIC_MAPS_SWARM",
  "execution_time_ms": 812.45,
  "deterministic_steps_executed": 5,
  "traces": [
    { "step_name": "Supervisor_Triage", "agent_role": "Supervisor Agent", "status": "SUCCESS", "...": "..." },
    { "step_name": "Routing_Recalculation", "agent_role": "Routing Agent", "status": "SUCCESS", "...": "..." },
    { "step_name": "Procurement_Vendor_Negotiation", "agent_role": "Procurement Agent", "status": "SUCCESS", "...": "..." },
    { "step_name": "Legal_Compliance_Generation", "agent_role": "Legal Agent", "status": "SUCCESS", "...": "..." },
    { "step_name": "ERP_State_Commit", "agent_role": "ERP Sync Agent", "status": "SUCCESS", "...": "..." }
  ],
  "final_resolution": {
    "vehicle_id": "TRUCK-BR-01-9922",
    "origin": "Zero Mile, Patna",
    "destination": "Hajipur Industrial Area Workshop",
    "mitigation_summary": "Swarm rerouted unit TRUCK-BR-01-9922 ...",
    "erp_ref": "TXN-ERP-4F9C2A"
  }
}
```

---

## 🗺 Roadmap

- [ ] Persist incidents to a real database instead of in-memory response
- [ ] Auth + multi-tenant fleet accounts
- [ ] Expand the corridor knowledge base beyond Bihar to other regions
- [ ] Real ERP/vendor API integrations (currently simulated)
- [ ] WebSocket-based live status updates instead of a single request/response

---

## 🤝 Contributing

Contributions are welcome! Feel free to open an issue or submit a PR for bug fixes, new corridor data, or additional agents in the swarm.

## 📄 License

This project is licensed under the [MIT License](LICENSE).
