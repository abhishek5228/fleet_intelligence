# 🚛 Autonomous Fleet Intelligence Engine

**A WhatsApp-first AI system that handles truck/bus breakdowns for a logistics company — end to end, without anyone needing to install an app.**

🔗 **Live App:** [fleet-swarm-project.vercel.app](https://fleet-swarm-project.vercel.app/)

> Think of it as an AI operations manager that never sleeps: the moment a truck breaks down, it diagnoses the problem, finds the nearest repair shop, gets spare-part prices from vendors, negotiates the best deal, and keeps the driver, the fleet manager, and the vendor all talking to each other — over plain WhatsApp.

---

## 📖 What problem does this solve?

If you run a fleet of trucks or buses (this project is built around a real logistics company, **Beekay Infra & Logistics**, operating in Bihar, India), a breakdown on the highway today usually looks like this:

- Driver calls the manager → manager is busy or driving → call goes unanswered
- Manager has no idea what's actually wrong with the vehicle
- Someone has to manually search for a nearby mechanic/workshop
- Someone has to call 2-3 spare-part vendors, ask prices, and negotiate
- Nobody tracks whether the truck ever actually got fixed
- If the same truck breaks down for the same reason every month, nobody notices the pattern

This project replaces that entire chaotic phone-call process with **one WhatsApp conversation per person** — the driver chats with the bot, the manager chats with the bot, and the vendor chats with the bot. The AI in the middle does the diagnosis, the routing, the price comparison, and the follow-up.

**No app to install. No training needed. If someone can use WhatsApp, they can use this system.**

---

## 🎯 Real, practical use cases

1. **A driver's truck breaks down on a highway at 2 AM.** He opens WhatsApp and either types the problem, sends a **voice note** in Hindi/Hinglish, or just **sends a photo** of the damaged part. The AI understands it, figures out the likely fault, and finds the nearest authorized service center automatically (using live Google Maps data).
2. **The fleet manager gets an instant WhatsApp alert** with the vehicle, issue, nearest workshop, and AI-suggested spare part — with **Approve ✅ / Reject ❌** buttons. No app, no dashboard login needed, no typing.
3. **The manager rejects a workshop** (too far / bad reviews) → the system **automatically reroutes to the next nearest one** and re-asks for approval — instead of the manager having to search again.
4. **Before any part is bought, 2–3 nearby vendors are contacted automatically** on WhatsApp, asked for price & availability, and the AI even **negotiates one round** if a vendor's price is higher than a competitor's (without ever leaking the competitor's exact price). The manager just picks the best price with one tap.
5. **The driver sends a photo of a damaged part any time during the repair.** If it's something small and safe (a loose wire, low coolant, a blown fuse), the AI gives the driver **step-by-step DIY instructions** right there in the chat — saving a mechanic visit entirely. If it's serious, it escalates to the manager immediately, flagged by severity (LOW/MODERATE/HIGH/CRITICAL).
6. **If a driver says "accident" or "aag lag gayi" (fire) or similar,** the AI detects the emergency instantly and fires a separate 🆘 CRITICAL alert to the manager — it doesn't wait in the normal message queue.
7. **If a driver goes quiet after a repair is dispatched,** the AI proactively checks in on its own ("kaisi chal rahi hai repair?") instead of the manager having to remember to follow up.
8. **If the same vehicle keeps breaking down with a similar issue** (e.g. radiator problems 3 times in 90 days), the manager sees a 🔁 **Predictive Maintenance Alert** right on the very first approval message — before approving yet another one-off repair, they're nudged to book a full inspection instead.
9. Every diagnosis the AI makes and a human later verifies is **permanently learned** — so the next time a similar issue comes up, the system answers with confidence from its own growing knowledge base instead of guessing again.

---

## 🧠 How it works (the actual flow)

```
   DRIVER                    AI ENGINE (this project)                 MANAGER              VENDOR
     │                                │                                  │                    │
     │──(text / voice / photo)───────▶│                                  │                    │
     │                                │  1. Understands the problem       │                    │
     │                                │     (Gemini + Hybrid RAG search   │                    │
     │                                │      over real repair manuals)    │                    │
     │                                │  2. Finds nearest workshop        │                    │
     │                                │     (Google Maps + Places)        │                    │
     │                                │  3. Sends Approve/Reject alert ──▶│                    │
     │                                │                                  │──Reject──▶ auto-reroute to next hub
     │                                │                                  │──Approve──┐          │
     │                                │  4. Asks 2-3 vendors for price ─────────────┼─────────▶│
     │                                │                                  │          │◀─price────│
     │                                │  5. Negotiates if needed ───────────────────┼─────────▶│
     │                                │  6. Shows manager best price ◀──────────────┘          │
     │                                │                                  │──picks vendor──▶     │
     │◀──"Accept Repair" button───────│                                  │                    │
     │──taps Accept──────────────────▶│  Chat is now LIVE with AI agent  │                    │
     │──(sends photo mid-repair)─────▶│  7. Vision AI diagnoses photo    │                    │
     │◀──DIY steps OR "help coming"───│     (self-fix if safe, escalate  │                    │
     │                                │      to manager if serious) ────▶│                    │
     │                                │  8. If driver goes silent,       │                    │
     │◀──proactive check-in───────────│     follows up automatically     │                    │
```

Everything above happens through **one Meta WhatsApp Business number** — the "AI Engine" box is a FastAPI backend that receives every WhatsApp message as a webhook, decides who sent it (driver / manager / vendor) and what they mean, and replies instantly.

---

## 🔄 Does it "auto-update" itself?

**Yes — the system has a Self-Improving Knowledge Base, but it only learns from human-verified answers, never silently on its own.** Here's exactly how it works:

1. A new/unusual breakdown comes in that doesn't match anything in the existing repair-manual knowledge base confidently.
2. The system falls back to a **live Gemini AI diagnosis** for that one incident — and it's honestly labeled *"AI-Generated Diagnosis — not from a certified manual, verify before dispatch"*, never shown with fake manual-level confidence.
3. If the **manager taps Approve** on that diagnosis, that's treated as human verification — and **only then** does the system permanently save it into its own knowledge base (it also writes it to disk so it survives redeploys).
4. The **very next time** a similar issue comes in — for any vehicle, not just this one — it's matched instantly with high confidence, straight from the learned manual, no Gemini guessing needed.

So it's "auto-update, but with a human in the loop" by design: an AI guess only becomes permanent knowledge after a real person has actually signed off on it. This is a safety choice — it stops one bad AI guess from quietly poisoning the knowledge base for every future incident.

*(Separately, and fully automatically with no approval needed: the system also tracks each vehicle's breakdown history on its own, and flags the manager if the same vehicle keeps having similar problems — see "Per-vehicle predictive maintenance" below.)*

---

## ✨ Complete feature list (every functionality, big and small)

### 🧠 Core AI / Agentic pipeline
- **Multi-Agent Swarm architecture** — Triage & Vision Agent → Hybrid RAG Diagnostics Agent → Routing & Logistics Agent → Manager-Approval/ERP Agent, each one logged and traced independently.
- **Hybrid Search RAG** — combines real BM25 keyword search with vector similarity + cross-encoder reranking over an expandable knowledge base of vehicle repair manuals (drop in more `.json` manual files any time, no code change needed).
- **Corrective RAG (CRAG) with a hallucination grader** — before recommending a spare part, the system checks the part is actually grounded in the retrieved manual text, not just a plausible-looking guess.
- **Self-Improving Knowledge Base** — see the "auto-update" section above.
- **Multi-Modal Vision (Gemini Vision)** — real photo analysis, both at the initial report stage (`image_url`) and any time later in the WhatsApp chat.
- **Full observability tracing** — every agent step is logged with its own trace ID, latency, and payload, in a LangSmith/Arize-style format, viewable per-incident.

### 📍 Finding the nearest workshop (Routing & Logistics Agent)
- **Live Google Places search** for authorized Tata/Eicher heavy-commercial workshops near the breakdown point, with a built-in fallback list of real Bihar hubs (Patna, Muzaffarpur, Gaya, Bhagalpur, Darbhanga, Purnia, Begusarai) if the API has no results.
- **Statewide vs. metro search scope** — automatically searches all of Bihar if the breakdown is outside Patna, or narrows to Patna metro if it's inside the city.
- **Distance-based sorting** — hubs are ranked by real straight-line (haversine) distance from the breakdown point, closest first.
- **Automatic de-duplication** — the same workshop showing up from multiple search queries is only listed once, capped at the 5 closest.
- **One-tap full route on Google Maps** — origin → workshop → final destination, as a single clickable link with the workshop as a waypoint.
- **Live distance & ETA** pulled from the Google Directions API, broken down leg-by-leg (breakdown→workshop, workshop→destination).
- **Automatic hub phone lookup** via Google Place Details, so the manager always gets a real contact number, not just a name.

### ✅ Manager approval flow
- **Approve / Reject buttons** on WhatsApp — no typing needed for the main decision.
- **Natural-language understanding too** — a manager can just type instructions in plain Hindi/Hinglish/English instead of tapping a button, and the AI classifies it into the right action (approve, reroute to a local mechanic, reject, answer an info request, log a custom instruction, etc.).
- **Automatic hub re-routing on Reject** — instantly re-offers the next-nearest workshop with a fresh Approve/Reject card, cycling through up to 5 hubs before giving up and asking for manual input.
- **Direct, specific answers to questions** — if the manager asks for a phone number or a detail that's already known, the AI gives the real value immediately instead of a vague "will share shortly."

### 💰 Spare-Part Price Comparison & Negotiation
- **Multiple vendors contacted in parallel** automatically (real nearby vendors from Google Places, or a configurable test list while demoing).
- **Yes/No availability buttons** sent to each vendor — no free-text parsing needed for that step.
- **Automatic one-round negotiation** — if a vendor's price is higher than a competitor's, the AI asks them to reconsider, without ever revealing the competitor's exact number.
- **40-second safety-net timer** — even if not every vendor has replied yet, the manager still gets whatever quotes are ready instead of waiting indefinitely for a vendor who never responds.
- **Side-by-side price comparison with pick-one buttons** for the manager, sorted cheapest-first with a "✅ Best" tag.
- **Automatic plain-text fallback** if the interactive buttons fail to deliver for any reason — the manager never gets left with nothing.
- **Vendor confirmation instantly triggers the driver's dispatch message**, with the vendor name, price, and hub contact included.

### 📲 WhatsApp-native driver experience
- **Text, voice note, or photo — driver's choice.** Voice notes are transcribed and understood directly in Hindi/Hinglish/regional languages, and can even auto-create a full incident report on their own.
- **"Accept Repair" button** — the AI chat only activates for a driver once they've confirmed, so there's no ambiguity about who's actively being helped.
- **In-chat Photo Diagnosis, any time** — not just at the initial report. Send a photo of any part, any time during the repair, and get an instant AI assessment.
- **Self-Service Repair Guidance (DIY steps)** — for genuinely minor, safe issues (a loose connector, low coolant, a blown fuse), the driver gets step-by-step fix instructions instead of an automatic mechanic dispatch. The AI is deliberately restricted from ever suggesting self-fixes for brakes, steering, fuel systems, or anything involving a running engine.
- **Automatic severity grading from photos** (LOW/MODERATE/HIGH/CRITICAL) — serious damage is escalated to the manager immediately, with the photo forwarded where possible.
- **Live location forwarding** — a driver's shared WhatsApp location goes straight to the manager with a clickable map pin.
- **Nudge to share live location** whenever a driver gives a status update, since WhatsApp only allows the driver to share their own location — the system can't pull it automatically.

### 🌐 Language & urgency intelligence
- **Automatic language/script matching** — the AI always replies in whatever script the sender just used (Devanagari Hindi, Hinglish, or English), never mixing or switching on its own.
- **Urgency detection on every message** — silently scored NORMAL / URGENT / CRITICAL. A message suggesting an accident, fire, or injury fires an instant, separate 🆘 alert to the manager rather than waiting in the normal reply.
- **Keyword-based safety net** — even if the AI service is temporarily unavailable, a lightweight keyword check still catches genuinely critical situations (accident, fire, injury) and escalates them.

### 🔁 Follow-up & predictive intelligence
- **Proactive driver follow-up** — if a driver goes silent for a configurable amount of time after a repair is dispatched, the AI checks in on its own ("kaisi chal rahi hai repair?") instead of the manager having to remember.
- **Per-vehicle predictive maintenance** — every incident is logged against its vehicle; if the same vehicle has a similar issue repeatedly within a configurable time window, the manager sees a 🔁 warning right on the approval card, before approving yet another one-off fix.

### 🔒 Reliability, safety & engineering
- **SQLite-backed persistent state** for every incident, approval, quote, vehicle history, and driver-activity timestamp — none of it lives only in memory, so a server restart/redeploy doesn't wipe anything out.
- **API-key protection** on the endpoints the dashboard calls (`/api/triage`, `/api/send-whatsapp-interactive`), so outsiders can't rack up Gemini/Maps/WhatsApp API costs.
- **Rate limiting** (20 requests/minute per IP) on those same endpoints as a second layer of abuse protection.
- **CORS configured** so the frontend (Vercel) and backend (Render) can talk to each other even though they're hosted separately.
- **Graceful fallbacks everywhere** — if `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, or `WHATSAPP_TOKEN` isn't set, the relevant feature simply simulates/skips instead of crashing the whole app.
- **Built-in debug endpoints** for troubleshooting without touching server logs — check API key setup, inspect any in-flight vendor quote's live status, clear stale test data, or test the Gemini connection directly (see the endpoint table below).

---

## 📸 See it in action

### 1. The Manager's Dashboard (web app)
This is where an incident is first reported/injected into the system — either by an admin, or automatically when a driver reports a breakdown by voice note. It runs the full Agentic RAG pipeline and shows the diagnosis live.

| Reporting a breakdown & getting a manual-grounded diagnosis | A trickier issue falling back to live AI diagnosis |
|---|---|
| ![Dashboard - triage form](docs/screenshots/01-dashboard-triage-form.png) | ![Dashboard - AI fallback diagnosis](docs/screenshots/02-dashboard-ai-diagnosis-fallback.png) |
| Clutch overheating → matched instantly to the Tata Prima manual at 90% confidence, with the exact spare part. | Steering oil leak → no manual matched confidently, so Gemini generates a real diagnosis on the spot, honestly labeled "AI-Generated" instead of faking manual-level certainty. |

### 2. The Driver's WhatsApp
The driver never sees a dashboard — everything happens in one chat thread with the fleet's WhatsApp number.

| Repair & parts confirmed | Tapping "Accept Repair" activates the AI agent |
|---|---|
| ![Driver - repair confirmed](docs/screenshots/03-driver-repair-confirmed.png) | ![Driver - accept repair button](docs/screenshots/04-driver-accept-repair-button.png) |

| Driver sends a photo mid-repair — AI diagnoses it instantly | Full AI diagnosis + natural back-and-forth chat |
|---|---|
| ![Driver - photo diagnosis](docs/screenshots/05-driver-photo-diagnosis-start.png) | ![Driver - photo diagnosis detail](docs/screenshots/06-driver-photo-diagnosis-detail.png) |

| Driver asks the AI to escalate to the manager | AI keeps the driver updated on dispatch status |
|---|---|
| ![Driver - chat follow-up](docs/screenshots/07-driver-chat-followup.png) | ![Driver - dispatch update](docs/screenshots/08-driver-chat-dispatch-update.png) |

*(In this real conversation, the driver's water pump had failed completely — the AI correctly identified it as unrepairable, told the driver not to start the vehicle, and kept him informed while a replacement was arranged.)*

### 3. The Vendor's WhatsApp
Spare-part vendors near the breakdown location are contacted automatically — no manual phone calls by the manager.

| Vendor gets an availability enquiry with Yes/No buttons | AI negotiates the price down in real time |
|---|---|
| ![Vendor - price enquiry](docs/screenshots/09-vendor-price-enquiry.png) | ![Vendor - negotiation](docs/screenshots/10-vendor-negotiation-1.png) |

| A second vendor is contacted the same way, in parallel |
|---|
| ![Vendor - second negotiation](docs/screenshots/11-vendor-negotiation-2.png) |

### 4. The Manager's WhatsApp
The manager makes every real decision with a single tap — no typing, no app.

| AI shows a side-by-side price comparison; manager picks the best one | Driver's mid-repair photo is escalated with a CRITICAL severity flag |
|---|---|
| ![Manager - price comparison](docs/screenshots/12-manager-price-comparison.png) | ![Manager - driver photo alert](docs/screenshots/13-manager-driver-photo-alert.png) |

| The exact photo and AI diagnosis, with an emergency-level alert |
|---|
| ![Manager - critical alert](docs/screenshots/14-manager-critical-alert.png) |

---

## 🏗️ Tech stack

| Layer | Technology |
|---|---|
| Backend API | Python, FastAPI |
| Frontend dashboard | React + Tailwind CSS (deployed on Vercel) |
| AI / LLM | Google Gemini (text + vision) |
| Search / RAG | BM25 + vector hybrid search, cross-encoder reranking |
| Maps & routing | Google Maps Places, Geocoding & Directions APIs |
| Messaging | WhatsApp Business Cloud API (Meta) |
| Persistence | SQLite (survives restarts/redeploys) |
| Hosting | Backend on Render, Frontend on Vercel |

---

## 📁 Project structure

```
fleet-swarm-project/
├── backend/
│   ├── main.py                  # FastAPI app — all endpoints, WhatsApp webhook, agent orchestration
│   ├── advanced/                 # Hybrid search, self-RAG, multi-agent graph, observability, vision
│   ├── knowledge_base/           # Repair-manual documents (JSON) the RAG engine searches — add more any time
│   ├── tests/                    # Backend test suite
│   └── requirements.txt
├── frontend/
│   ├── src/App.js                # The manager's web dashboard (incident injection + live status)
│   └── ...
├── docs/screenshots/              # Screenshots used in this README
└── workflows/ci.yml               # CI pipeline
```

---

## ⚙️ Running it yourself

### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Environment variables it reads (set these before running for real, otherwise the relevant feature simply falls back to a safe simulated/skipped mode instead of crashing):

| Variable | What it's for |
|---|---|
| `WHATSAPP_TOKEN` | Meta WhatsApp Business Cloud API access token |
| `PHONE_NUMBER_ID` | Your WhatsApp Business phone number ID |
| `GEMINI_API_KEY` | Powers all the AI understanding, diagnosis, vision, and language matching |
| `GOOGLE_MAPS_API_KEY` | Nearby workshop search, geocoding, and route calculation |
| `FLEET_API_KEY` | Protects `/api/triage` and `/api/send-whatsapp-interactive` from public abuse |
| `FOLLOWUP_CHECK_DELAY_SECONDS` | How long to wait before proactively checking in on a silent driver (default: 2 hours) |
| `PREDICTIVE_PATTERN_WINDOW_DAYS` | How far back to look for repeat issues on the same vehicle (default: 90 days) |
| `PREDICTIVE_PATTERN_MIN_REPEATS` | How many repeats before flagging a predictive-maintenance warning (default: 2) |

### Frontend

```bash
cd frontend
npm install
npm start
```

Set `REACT_APP_API_URL` to point at your backend if it's not running locally.

---

## 🔌 All API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/triage` | Runs the full Agentic RAG pipeline for a new incident |
| `POST /api/send-whatsapp-interactive` | Sends the Approve/Reject card to the manager on WhatsApp |
| `GET /api/approval-status/{incident_id}` | Lets the dashboard poll for the manager's decision |
| `GET` / `POST /api/whatsapp-webhook` | Meta's webhook verification, and where every incoming WhatsApp message (driver/manager/vendor) actually lands and gets handled |
| `GET /api/health` | Simple health/uptime check |
| `GET /api/debug-api-key` | Checks whether `FLEET_API_KEY` is configured correctly, without exposing the actual key value |
| `GET /api/debug-pending-quotes` | Shows the live, real-time state of every in-flight vendor price quote (who's replied, who's still waiting, what prices came in) |
| `POST /api/debug-clear-pending-quotes` | Clears out stale/test vendor-quote data before a fresh test run |
| `GET /api/test-gemini` | Quick one-click check that the `GEMINI_API_KEY` and Gemini connection are working |

---

## 🚧 Good to know / current limitations

- The free hosting tier (Render) can "sleep" when idle — background timers (like the proactive follow-up) reset if the server restarts, though all incident data itself is safely persisted.
- Forwarding a driver's actual photo to the manager (not just the AI's text description of it) is best-effort and depends on your WhatsApp Business setup — the text diagnosis always goes through regardless.
- Vendor phone numbers are currently a small test list for demoing the price-comparison flow; swapping in real vendor numbers found via Google Places is a one-line config change (see the `TEST_VENDOR_NUMBERS` / `TEST_HUB_NUMBERS` notes in `backend/main.py`).

---

Built for **Beekay Infra & Logistics** as a real, working prototype of what an AI-run fleet operations desk can look like — one that talks to everyone on the road in the one app they already have open: WhatsApp.
