import React, { useState } from 'react';
import './App.css';

function App() {
  const [formData, setFormData] = useState({
    vehicle_id: 'TRUCK-BR-01-9922',
    location: 'Zero Mile, Patna',
    destination: 'Hajipur Industrial Area Workshop',
    issue_type: 'Engine Overheat & Transmission Breakdown',
    severity: 'CRITICAL',
    cargo_type: 'Heavy Construction Steel Rods'
  });

  const [loading, setLoading] = useState(false);
  const [responseResult, setResponseResult] = useState(null);
  const [error, setError] = useState(null);

  const handleChange = (e) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResponseResult(null);

    const API_URL = process.env.REACT_APP_API_URL || 'https://fleet-swarm-backend.onrender.com';

    try {
      const res = await fetch(`${API_URL}/api/triage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData)
      });

      const data = await res.json();
      if (res.ok) {
        setResponseResult(data);
      } else {
        setError(data.detail || 'Validation error from backend.');
      }
    } catch (err) {
      setError('Failed to connect to backend server.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 p-4 md:p-8 font-sans">
      <div className="max-w-5xl mx-auto space-y-8">
        
        {/* Header */}
        <header className="border-b border-slate-800 pb-5 text-center">
          <div className="inline-block bg-blue-500/10 text-blue-400 text-xs font-semibold px-3 py-1 rounded-full mb-2 border border-blue-500/20">
            ● Swarm Cluster: Healthy & Active
          </div>
          <h1 className="text-3xl font-extrabold tracking-tight text-white">Hyper-Local Fleet Swarm Intelligence</h1>
          <p className="text-slate-400 text-sm mt-1">Deterministic Multi-Agent Self-Healing Logistics & Google Maps Engine</p>
        </header>

        {/* Input Form Card */}
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-2xl">
          <h2 className="text-lg font-semibold text-blue-400 mb-4 flex items-center gap-2">
            <span>⚡</span> Inject Bottleneck / Incident
          </h2>
          
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Heavy Fleet Unit ID</label>
                <input type="text" name="vehicle_id" value={formData.vehicle_id} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" required />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Severity Level</label>
                <select name="severity" value={formData.severity} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500">
                  <option value="CRITICAL">CRITICAL</option>
                  <option value="MODERATE">MODERATE</option>
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Cargo Type</label>
                <input type="text" name="cargo_type" value={formData.cargo_type} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" required />
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Highway / Breakdown Location (Origin)</label>
                <input type="text" name="location" value={formData.location} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" required />
              </div>
              <div>
                <label className="block text-xs font-medium text-slate-400 mb-1">Destination Workshop / Hub (End)</label>
                <input type="text" name="destination" value={formData.destination} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" required />
              </div>
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-400 mb-1">Issue Type / Breakdown Details</label>
              <input type="text" name="issue_type" value={formData.issue_type} onChange={handleChange} className="w-full p-2.5 bg-slate-950 border border-slate-700 rounded-lg text-sm text-white focus:outline-none focus:border-blue-500" required />
            </div>

            <button type="submit" disabled={loading} className="w-full bg-blue-600 hover:bg-blue-500 text-white font-medium py-3 rounded-lg transition duration-200 shadow-lg shadow-blue-600/20 disabled:opacity-50">
              {loading ? 'Dispatching Agent Swarm & Computing Maps...' : '🚀 Dispatch Agent Swarm'}
            </button>
          </form>
        </div>

        {error && (
          <div className="bg-red-950/80 border border-red-800 text-red-200 p-4 rounded-xl text-sm">
            {String(error)}
          </div>
        )}

        {/* Execution Results */}
        {responseResult && (
          <div className="space-y-6">
            {/* Summary Card */}
            <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl">
              <div className="flex flex-col md:flex-row justify-between items-start md:items-center border-b border-slate-800 pb-4 mb-4 gap-2">
                <div>
                  <h3 className="text-lg font-bold text-emerald-400">Swarm Cluster Resolution Successful</h3>
                  <p className="text-xs text-slate-400">Incident ID: <span className="font-mono text-slate-200">{responseResult.incident_id}</span></p>
                </div>
                <div className="flex gap-2">
                  <span className="text-xs bg-slate-800 text-slate-300 px-3 py-1 rounded-full border border-slate-700">
                    ⏱ {responseResult.execution_time_ms} ms
                  </span>
                  <span className="text-xs bg-blue-950 text-blue-300 px-3 py-1 rounded-full border border-blue-800">
                    Steps: {responseResult.deterministic_steps_executed}
                  </span>
                </div>
              </div>

              <div className="bg-slate-950 p-4 rounded-lg border border-slate-800/80">
                <h4 className="text-xs font-bold uppercase tracking-wider text-amber-400 mb-2">Trip Mitigation & Rerouting Summary</h4>
                <p className="text-sm text-slate-300 leading-relaxed">{responseResult.final_resolution.mitigation_summary}</p>
                <div className="mt-3 pt-3 border-t border-slate-900 flex justify-between text-xs text-slate-400">
                  <span>ERP Reference: <strong className="text-slate-200 font-mono">{responseResult.final_resolution.erp_ref}</strong></span>
                  <span className="text-emerald-400 font-semibold">Status: Ledger Committed</span>
                </div>
              </div>
            </div>

            {/* Pipeline Traces */}
            <div className="space-y-4">
              <h3 className="text-md font-semibold text-slate-300">Multi-Agent Pipeline Execution Traces</h3>
              
              <div className="grid grid-cols-1 gap-4">
                {responseResult.traces?.map((trace, idx) => (
                  <div key={idx} className="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-lg hover:border-slate-700 transition">
                    <div className="flex flex-wrap justify-between items-center mb-2 pb-2 border-b border-slate-800/60">
                      <div className="flex items-center gap-2">
                        <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                        <span className="font-semibold text-sm text-slate-200">{trace.agent_role}</span>
                        <span className="text-xs text-slate-400 font-mono">({trace.step_name})</span>
                      </div>
                      <div className="flex items-center gap-3 text-xs">
                        <span className="text-emerald-400 font-medium">● {trace.status}</span>
                        <span className="text-slate-500">{trace.timestamp} ms</span>
                      </div>
                    </div>

                    {/* Formatted payload highlights */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 mt-3 text-xs">
                      {Object.entries(trace.output_payload || {}).map(([key, val], kIdx) => (
                        <div key={kIdx} className="bg-slate-950 p-2.5 rounded border border-slate-800/50 overflow-hidden">
                          <span className="block text-slate-500 uppercase tracking-wider text-[10px] mb-1">{key.replaceAll('_', ' ')}</span>
                          <span className="text-slate-300 font-mono truncate block">
                            {typeof val === 'object' ? JSON.stringify(val) : String(val)}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>

          </div>
        )}

      </div>
    </div>
  );
}

export default App;
