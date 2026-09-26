import React, { useState, useEffect } from 'react';
import './App.css';

function App() {
  const [formData, setFormData] = useState({
    vehicle_id: '',
    location: '',
    destination: '',
    issue_type: '',
    severity: '',
    cargo_type: ''
  });

  const [loading, setLoading] = useState(false);
  const [responseResult, setResponseResult] = useState(null);
  const [error, setError] = useState(null);
  const [approvalStatus, setApprovalStatus] = useState('PENDING_MANAGER_APPROVAL');

  const handleChange = (e) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResponseResult(null);
    setApprovalStatus('PENDING_MANAGER_APPROVAL');

    const API_URL = process.env.REACT_APP_API_URL || 'https://fleet-swarm-backend.onrender.com';

    try {
      const res = await fetch(`${API_URL}/api/triage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': 'abhi123secretkey456' },
        body: JSON.stringify(formData)
      });

      const data = await res.json();
      if (res.ok) {
        setResponseResult(data);
        setApprovalStatus(data.approval_status || 'PENDING_MANAGER_APPROVAL');
      } else {
        setError(data.detail || 'Validation error from backend.');
      }
    } catch (err) {
      setError('Failed to connect to backend server.');
    } finally {
      setLoading(false);
    }
  };

  // Live Polling Effect for Webhook Updates
  useEffect(() => {
    if (!responseResult?.incident_id) return;
    const API_URL = process.env.REACT_APP_API_URL || 'https://fleet-swarm-backend.onrender.com';
    const incidentId = responseResult.incident_id;

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_URL}/api/approval-status/${incidentId}`);
        const data = await res.json();
        if (data && data.approval_status) {
          setApprovalStatus(data.approval_status);
          if (data.approval_status.includes('APPROVED') || data.approval_status.includes('REJECTED')) {
            clearInterval(interval);
          }
        }
      } catch (err) {
        console.error("Polling error:", err);
      }
    }, 3000);

    return () => clearInterval(interval);
  }, [responseResult?.incident_id]);

  const routingTrace = responseResult?.traces?.find(t => t.step_name === 'Routing_Recalculation')?.output_payload;
  const ragTrace = responseResult?.traces?.find(t => t.step_name === 'Agentic_RAG_Diagnostics')?.output_payload;
  const supervisorTrace = responseResult?.traces?.find(t => t.step_name === 'Supervisor_Triage')?.output_payload;

  const nearestHub = supervisorTrace?.destination_workshop || 'Authorized Service Hub';
  const allNearbyHubs = supervisorTrace?.all_nearby_service_centers || [];

  const searchScope = responseResult?.final_resolution?.service_center_search_scope;

  const googleMapsRouteUrl =
    responseResult?.final_resolution?.open_full_route_url ||
    `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(formData.location)}&destination=${encodeURIComponent(formData.destination)}&waypoints=${encodeURIComponent(
      nearestHub.replace(/^\d+\.\s*/, '').replace(/\s*\(Rating:.*?\)\s*$/, '')
    )}&travelmode=driving`;
  const googleMapsHubPinUrl = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(nearestHub)}`;

  const handleTriggerInteractiveApproval = async () => {
    if (!responseResult) return;
    const API_URL = process.env.REACT_APP_API_URL || 'https://fleet-swarm-backend.onrender.com';

    try {
      const res = await fetch(`${API_URL}/api/send-whatsapp-interactive`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': 'abhi123secretkey456' },
        body: JSON.stringify({
          incident_id: responseResult.incident_id,
          phone: responseResult.assigned_whatsapp_number,
          vehicle_id: formData.vehicle_id,
          hub: nearestHub,
          location: formData.location,
          issue_type: formData.issue_type,
          severity: formData.severity,
          cargo_type: formData.cargo_type,
          recommended_part: ragTrace?.recommended_part || 'Heavy Duty Spare Kit'
        })
      });
      const data = await res.json();
      if (data.status === 'meta_api_error') {
        alert(`Meta API Error: ${JSON.stringify(data.error_details)}`);
      } else {
        alert(`Agentic RAG WhatsApp alert successfully dispatched to manager!`);
      }
    } catch (err) {
      alert('Failed to dispatch Agentic WhatsApp message.');
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-4 md:p-8 font-sans">
      <div className="max-w-5xl mx-auto space-y-8">
        
        <header className="border-b border-slate-800 pb-5 text-center">
          <div className="inline-block bg-purple-500/10 text-purple-400 text-xs font-semibold px-3 py-1 rounded-full mb-2 border border-purple-500/20">
            ● Enterprise Agentic AI & RAG Swarm v4.0
          </div>
          <h1 className="text-3xl font-extrabold tracking-tight text-white">Autonomous Fleet Intelligence Engine</h1>
          <p className="text-slate-400 text-sm mt-1">Multi-Agent Swarm with Vector RAG Diagnostics & Conversational WhatsApp LLM Gates</p>
        </header>

        {/* Form Card */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-2xl">
          <h2 className="text-lg font-semibold text-purple-400 mb-4 flex items-center gap-2">
            <span>🧠</span> Inject Incident & Run Agentic RAG Pipeline
          </h2>
          
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Heavy Fleet Unit ID</label>
                <select name="vehicle_id" value={formData.vehicle_id} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white font-mono" required>
                  <option value="" disabled>SELECT VEHICLE</option>
                  <option value="BR01GP9621">BR01GP9621</option>
                  <option value="BR01GM7465">BR01GM7465</option>
                  <option value="BR01GP0756">BR01GP0756</option>
                  <option value="BR01GP0757">BR01GP0757</option>
                  <option value="BR01GP8148">BR01GP8148</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Severity Level</label>
                <select name="severity" value={formData.severity} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white" required>
                  <option value="" disabled>SELECT SEVERITY</option>
                  <option value="CRITICAL">CRITICAL</option>
                  <option value="MODERATE">MODERATE</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Cargo Type</label>
                <input type="text" name="cargo_type" value={formData.cargo_type} onChange={handleChange} placeholder="YOUR LOAD TYPE" className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white" required />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Breakdown Location (Origin)</label>
                <input type="text" name="location" value={formData.location} onChange={handleChange} placeholder="TYPE BREAKDOWN LOCATION WITH CITY" className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white" required />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Destination Workshop / Hub</label>
                <input type="text" name="destination" value={formData.destination} onChange={handleChange} placeholder="TYPE YOUR DESTINATION WITH CITY" className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white" required />
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1">Issue Type / Breakdown Details</label>
              <input type="text" name="issue_type" value={formData.issue_type} onChange={handleChange} placeholder="WRITE YOUR ISSUE" className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white" required />
            </div>

            <button type="submit" disabled={loading} className="w-full bg-purple-600 hover:bg-purple-500 text-white font-medium py-3 rounded-lg transition shadow-lg shadow-purple-600/20 disabled:opacity-50">
              {loading ? 'Running Vector RAG & Multi-Agent Swarm...' : '🚀 Execute Agentic RAG Swarm & Dispatch WhatsApp Alert'}
            </button>
          </form>
        </div>

        {error && (
          <div className="bg-red-950/80 border border-red-800 text-red-200 p-4 rounded-xl text-sm">
            {String(error)}
          </div>
        )}

        {responseResult && (
          <div className="space-y-6">
            
            {/* Agentic RAG Diagnostics Card */}
            {ragTrace && (
              <div className="bg-slate-900 border border-purple-500/40 rounded-xl p-6 shadow-xl">
                <h3 className="text-base font-bold text-purple-400 mb-3 flex items-center gap-2">
                  <span>🔬</span> Agentic RAG Vector Search & Part Diagnostic Result
                </h3>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
                  <div className="bg-slate-950 p-3 rounded-lg border border-slate-800">
                    <span className="text-slate-400 block mb-1">Vector Match Score</span>
                    <span className="text-emerald-400 font-bold text-sm">{(ragTrace.vector_match_score * 100).toFixed(0)}% Accuracy</span>
                  </div>
                  <div className="bg-slate-950 p-3 rounded-lg border border-slate-800 md:col-span-2">
                    <span className="text-slate-400 block mb-1">Referenced Technical Manual</span>
                    <span className="text-slate-200 font-mono">{ragTrace.referenced_manual}</span>
                  </div>
                </div>
                <div className="mt-3 bg-slate-950 p-3 rounded-lg border border-slate-800 text-xs">
                  <span className="text-slate-400 block mb-1">AI Diagnostic & Part Recommendation:</span>
                  <span className="text-amber-300 font-bold">{ragTrace.recommended_part}</span> — <span className="text-slate-300">{ragTrace.diagnostic_summary}</span>
                </div>
              </div>
            )}

            {/* HITL & WhatsApp Conversational Status Card */}
            <div className="bg-gradient-to-br from-slate-900 to-slate-950 border border-blue-500/30 rounded-xl p-6 shadow-2xl relative overflow-hidden">
              <div className="absolute top-0 right-0 bg-purple-600 text-white text-[10px] font-bold px-3 py-1 rounded-bl-xl uppercase tracking-wider">
                Conversational LLM Gate Active
              </div>

              <h3 className="text-lg font-bold text-white mb-3 flex items-center gap-2">
                <span>💬</span> WhatsApp Interactive & Natural Language Agent Gate
              </h3>

              <div className={`p-4 rounded-lg mb-5 border flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3 ${
                approvalStatus.includes('APPROVED')
                  ? 'bg-emerald-950/50 border-emerald-500/40 text-emerald-300' 
                  : approvalStatus.includes('REJECTED')
                  ? 'bg-rose-950/50 border-rose-500/40 text-rose-300'
                  : 'bg-amber-950/40 border-amber-500/30 text-amber-300'
              }`}>
                <div>
                  <span className="text-[10px] uppercase font-bold tracking-wider block opacity-80">Manager Response State</span>
                  <div className="text-sm font-bold mt-0.5">
                    {approvalStatus === 'APPROVED_AND_DISPATCHED' && '✅ Approved via Button Click (Mechanic & Part Dispatched)'}
                    {approvalStatus === 'APPROVED_LOCAL_MECHANIC_REROUTED' && '🔄 Approved via LLM Text Parsing (Local Mechanic Rerouted)'}
                    {approvalStatus.includes('CUSTOM_INSTRUCTION') && `📝 Custom LLM Instruction Logged: ${approvalStatus}`}
                    {approvalStatus === 'REJECTED_REROUTING' && '❌ Rejected & Rerouting Triggered'}
                    {approvalStatus === 'PENDING_MANAGER_APPROVAL' && '⏳ Waiting for WhatsApp Button Click OR Natural Language Text Reply...'}
                  </div>
                </div>

                <div className="flex gap-2 w-full sm:w-auto">
                  {approvalStatus === 'PENDING_MANAGER_APPROVAL' && (
                    <button 
                      onClick={handleTriggerInteractiveApproval}
                      className="bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold py-2 px-3 rounded transition shadow"
                    >
                      📲 Send Agentic RAG Alert to WhatsApp
                    </button>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-5">
                <div className="bg-slate-950/80 p-4 rounded-lg border border-slate-800">
                  <span className="text-xs text-slate-400 block uppercase tracking-wider mb-1">Total Distance & ETA</span>
                  <div className="text-xl font-extrabold text-blue-400">
                    {routingTrace?.total_distance || '310 km'} <span className="text-sm font-normal text-slate-300">({routingTrace?.estimated_travel_time || '6 hours'})</span>
                  </div>
                </div>

                <div className="bg-slate-950/80 p-4 rounded-lg border border-slate-800 flex flex-col justify-between">
                  <div>
                    <span className="text-xs text-slate-400 block uppercase tracking-wider mb-1">Primary Nearest Hub</span>
                    <div className="text-sm font-bold text-emerald-400 truncate">
                      {nearestHub}
                    </div>
                  </div>
                  <a 
                    href={googleMapsHubPinUrl}
                    target="_blank" 
                    rel="noopener noreferrer" 
                    className="mt-3 text-center bg-slate-900 hover:bg-slate-800 text-emerald-400 border border-emerald-500/40 text-xs font-semibold py-1.5 px-3 rounded transition flex items-center justify-center gap-1.5"
                  >
                    <span>🎯</span> Pinpoint Hub on Google Maps
                  </a>
                </div>
              </div>

              {/* Hubs List */}
              <div className="bg-slate-900/90 p-4 rounded-lg border border-slate-800 mb-5">
                <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
                  <strong className="text-white block text-xs uppercase tracking-wider text-purple-400">
                    Detected Authorized Service Centers:
                  </strong>
                  {searchScope && (
                    <span className={`text-[10px] font-bold px-2 py-1 rounded-full border uppercase tracking-wider ${
                      searchScope === 'STATEWIDE_BIHAR'
                        ? 'bg-amber-950/50 border-amber-500/40 text-amber-300'
                        : 'bg-blue-950/50 border-blue-500/40 text-blue-300'
                    }`}>
                      {searchScope === 'STATEWIDE_BIHAR' ? '🗺️ Statewide Bihar Search' : '📍 Patna Metro Search'}
                    </span>
                  )}
                </div>
                <ul className="space-y-2.5 text-xs text-slate-300">
                  {allNearbyHubs.map((hubName, idx) => (
                    <li key={idx} className="bg-slate-950 p-3 rounded-lg border border-slate-800/80 font-mono flex items-start gap-3 shadow-inner">
                      <span className="text-purple-400 font-bold text-sm bg-purple-950/50 px-2 py-0.5 rounded border border-purple-800/40">{idx + 1}</span>
                      <span className="text-slate-200 leading-relaxed self-center">{typeof hubName === 'string' ? hubName.replace(/^\d+\.\s*/, '') : JSON.stringify(hubName)}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <a 
                href={googleMapsRouteUrl} 
                target="_blank" 
                rel="noopener noreferrer" 
                className="block text-center w-full bg-emerald-600 hover:bg-emerald-500 text-white font-semibold py-3 px-4 rounded-lg transition shadow-lg shadow-emerald-600/20 text-sm flex items-center justify-center gap-2"
              >
                <span>🗺️</span> Open Full Route with Hub Waypoint in Google Maps
              </a>
            </div>

          </div>
        )}

      </div>
    </div>
  );
}

export default App;
