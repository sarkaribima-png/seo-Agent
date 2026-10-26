"use client";
import { useEffect, useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import Shell from "@/components/Shell";
import SiteSelect from "@/components/SiteSelect";
import { runAgent, supabase } from "@/lib/supabase";

const sum = (a: any[], k: string) => a.reduce((s, r) => s + (r[k] || 0), 0);
const wpos = (a: any[]) => { const i = sum(a, "impressions"); return i ? a.reduce((s, r) => s + (r.position || 0) * (r.impressions || 0), 0) / i : null; };
const delta = (n: number, p: number) => (p ? ((n - p) / p) * 100 : 0);

export default function Overview() {
  const [sites, setSites] = useState<any[]>([]); const [site, setSite] = useState("");
  const [rows, setRows] = useState<any[]>([]); const [counts, setCounts] = useState<any>({});
  const [device, setDevice] = useState("ALL"); const [msg, setMsg] = useState("");

  useEffect(() => { supabase.from("sites").select("*").order("id").then(({ data }) => { setSites(data || []); if (data?.length) setSite(data[0].id); }); }, []);
  useEffect(() => {
    if (!site) return;
    const since = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
    supabase.from("gsc_by_day").select("*").eq("site_id", site).gte("date", since).order("date").then(({ data }) => setRows(data || []));
    const c = (s: string) => supabase.from("proposals").select("id", { count: "exact", head: true }).eq("site_id", site).eq("status", s);
    Promise.all(["pending", "approved", "applied", "failed"].map(c)).then(r => setCounts({ pending: r[0].count, approved: r[1].count, applied: r[2].count, failed: r[3].count }));
  }, [site]);

  const cur = sites.find(s => s.id === site);
  const byDate = useMemo(() => {
    const m: any = {};
    rows.filter(r => device === "ALL" || r.device === device).forEach(r => { (m[r.date] ||= []).push(r); });
    return Object.keys(m).sort().map(d => ({ date: d.slice(5), clicks: sum(m[d], "clicks"), impressions: sum(m[d], "impressions") }));
  }, [rows, device]);
  const pos = useMemo(() => {
    const m: any = {};
    rows.forEach(r => { (m[r.date] ||= {})[r.device] = r; });
    return Object.keys(m).sort().map(d => ({ date: d.slice(5), MOBILE: m[d].MOBILE ? +m[d].MOBILE.position?.toFixed(1) : null, DESKTOP: m[d].DESKTOP ? +m[d].DESKTOP.position?.toFixed(1) : null }));
  }, [rows]);
  const kp = useMemo(() => {
    const dates = [...new Set(rows.map(r => r.date))].sort(); const last = dates.slice(-7), prev = dates.slice(-14, -7);
    const f = (ds: string[], dev?: string) => rows.filter(r => ds.includes(r.date) && (!dev || r.device === dev));
    return { c: [sum(f(last), "clicks"), sum(f(prev), "clicks")], i: [sum(f(last), "impressions"), sum(f(prev), "impressions")],
      pm: [wpos(f(last, "MOBILE")), wpos(f(prev, "MOBILE"))], pd: [wpos(f(last, "DESKTOP")), wpos(f(prev, "DESKTOP"))] };
  }, [rows]);

  async function setMode(mode: string) {
    if (mode === "live" && !confirm("Live mode: approved AND new title/meta proposals will be applied to the site (1 page/day). Continue?")) return;
    const { error } = await supabase.from("sites").update({ mode }).eq("id", site);
    if (error) alert(error.message); else setSites(sites.map(s => (s.id === site ? { ...s, mode } : s)));
  }
  async function run(cmd: string) { setMsg("Starting…"); const r = await runAgent(cmd); setMsg(r.message); }
  const Kpi = ({ l, v, d, inv }: any) => (<div className="card kpi"><div className="l">{l}</div><div className="v">{v}</div>
    {d !== undefined && <div className={(inv ? d < 0 : d > 0) ? "up small" : "down small"}>{d > 0 ? "▲" : "▼"} {Math.abs(d).toFixed(1)}% vs prev 7d</div>}</div>);
  const f1 = (x: number | null) => (x == null ? "–" : x.toFixed(1));

  return (<Shell>
    <div className="row"><h1 style={{ margin: 0 }}>Overview</h1><span className="sp" /><SiteSelect sites={sites} value={site} onChange={setSite} />
      {cur && <select value={cur.mode} onChange={e => setMode(e.target.value)}><option value="dry-run">dry-run (proposals only)</option><option value="live">live (auto-apply 1/day)</option><option value="paused">paused</option></select>}
      <button onClick={() => run("daily")}>Run now</button></div>
    {msg && <p className="muted small">{msg}</p>}
    <div className="grid g4" style={{ marginBottom: 14 }}>
      <Kpi l="Clicks (7d)" v={kp.c[0]} d={delta(kp.c[0], kp.c[1])} />
      <Kpi l="Impressions (7d)" v={kp.i[0].toLocaleString()} d={delta(kp.i[0], kp.i[1])} />
      <Kpi l="Avg position · Mobile" v={f1(kp.pm[0])} d={kp.pm[0] && kp.pm[1] ? delta(kp.pm[0], kp.pm[1]) : undefined} inv />
      <Kpi l="Avg position · Desktop" v={f1(kp.pd[0])} d={kp.pd[0] && kp.pd[1] ? delta(kp.pd[0], kp.pd[1]) : undefined} inv /></div>
    <div className="grid g4" style={{ marginBottom: 14 }}>
      <a className="card kpi" href="/proposals"><div className="l">Pending approval</div><div className="v">{counts.pending ?? "–"}</div></a>
      <div className="card kpi"><div className="l">Approved (waiting for run)</div><div className="v">{counts.approved ?? "–"}</div></div>
      <div className="card kpi"><div className="l">Applied</div><div className="v">{counts.applied ?? "–"}</div></div>
      <div className="card kpi"><div className="l">Failed</div><div className="v">{counts.failed ?? "–"}</div></div></div>
    <div className="grid g2">
      <div className="card"><div className="row" style={{ marginBottom: 4 }}><h2 style={{ margin: 0 }}>Clicks & impressions</h2><span className="sp" />
        <select value={device} onChange={e => setDevice(e.target.value)}><option value="ALL">All devices</option><option value="MOBILE">Mobile</option><option value="DESKTOP">Desktop</option></select></div>
        <div style={{ height: 260 }}><ResponsiveContainer><LineChart data={byDate}><CartesianGrid strokeDasharray="3 3" stroke="#8884" /><XAxis dataKey="date" fontSize={11} />
          <YAxis yAxisId="l" fontSize={11} /><YAxis yAxisId="r" orientation="right" fontSize={11} /><Tooltip /><Legend />
          <Line yAxisId="l" type="monotone" dataKey="clicks" stroke="#1f6feb" dot={false} /><Line yAxisId="r" type="monotone" dataKey="impressions" stroke="#8957e5" dot={false} /></LineChart></ResponsiveContainer></div></div>
      <div className="card"><h2>Average position: mobile vs desktop</h2>
        <div style={{ height: 260 }}><ResponsiveContainer><LineChart data={pos}><CartesianGrid strokeDasharray="3 3" stroke="#8884" /><XAxis dataKey="date" fontSize={11} />
          <YAxis reversed domain={["auto", "auto"]} fontSize={11} /><Tooltip /><Legend />
          <Line type="monotone" dataKey="MOBILE" stroke="#d29922" dot={false} connectNulls /><Line type="monotone" dataKey="DESKTOP" stroke="#1a7f4b" dot={false} connectNulls /></LineChart></ResponsiveContainer></div>
        <p className="muted small">Lower is better. The data is 2-3 days behind (Search Console delay).</p></div></div>
    {!sites.length && <p className="muted">No sites yet. Run the agent once (workflow "daily") to create the site row.</p>}
  </Shell>);
}
