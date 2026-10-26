"use client";
import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import Shell from "@/components/Shell";
import SiteSelect from "@/components/SiteSelect";
import { runAgent, supabase } from "@/lib/supabase";

const FILTERS: Record<string, (r: any) => boolean> = {
  all: () => true, "H1 issue": r => r.h1 !== 1, "No alt text": r => r.images_no_alt > 0,
  "Risky claims": r => (r.risky_claims || []).length > 0, "Schema issue": r => (r.schema_issues || []).some((s: string) => !s.startsWith("h1_")) };
const issues = (r: any) => (r.h1 !== 1 ? 1 : 0) + (r.images_no_alt > 0 ? 1 : 0) + ((r.risky_claims || []).length ? 1 : 0) + ((r.schema_issues || []).some((s: string) => !s.startsWith("h1_")) ? 1 : 0);

export default function Audits() {
  const [sites, setSites] = useState<any[]>([]); const [site, setSite] = useState(""); const [rows, setRows] = useState<any[]>([]);
  const [f, setF] = useState("all"); const [msg, setMsg] = useState("");
  useEffect(() => { supabase.from("sites").select("id,domain").order("id").then(({ data }) => { setSites(data || []); if (data?.length) setSite(data[0].id); }); }, []);
  useEffect(() => { if (site) supabase.from("page_audits_latest").select("*").eq("site_id", site).limit(1000).then(({ data }) => setRows(data || [])); }, [site]);
  const chart = useMemo(() => Object.keys(FILTERS).filter(k => k !== "all").map(k => ({ issue: k, pages: rows.filter(FILTERS[k]).length })), [rows]);
  const list = useMemo(() => rows.filter(FILTERS[f]).sort((a, b) => issues(b) - issues(a) || b.images_no_alt - a.images_no_alt), [rows, f]);
  async function run() { setMsg("Starting…"); const r = await runAgent("audit-all"); setMsg(r.message); }
  return (<Shell>
    <div className="row"><h1 style={{ margin: 0 }}>Page audits</h1><span className="sp" /><SiteSelect sites={sites} value={site} onChange={setSite} /><button onClick={run}>Run audit</button></div>
    {msg && <p className="muted small">{msg}</p>}
    <div className="grid g4" style={{ marginBottom: 14 }}>
      <div className="card kpi"><div className="l">Pages audited</div><div className="v">{rows.length}</div></div>
      <div className="card kpi"><div className="l">Images without alt text</div><div className="v">{rows.reduce((s, r) => s + (r.images_no_alt || 0), 0)}</div></div>
      <div className="card kpi"><div className="l">Pages with no/many H1</div><div className="v">{rows.filter(FILTERS["H1 issue"]).length}</div></div>
      <div className="card kpi"><div className="l">Pages with risky claims</div><div className="v">{rows.filter(FILTERS["Risky claims"]).length}</div></div></div>
    <div className="card" style={{ marginBottom: 14 }}><h2>Pages per issue</h2><div style={{ height: 200 }}><ResponsiveContainer><BarChart data={chart}><CartesianGrid strokeDasharray="3 3" stroke="#8884" /><XAxis dataKey="issue" fontSize={12} /><YAxis allowDecimals={false} fontSize={12} /><Tooltip /><Bar dataKey="pages" fill="#1f6feb" /></BarChart></ResponsiveContainer></div></div>
    <div className="row tabs">{Object.keys(FILTERS).map(k => <button key={k} className={f === k ? "on" : ""} onClick={() => setF(k)}>{k}</button>)}</div>
    <div className="card tw"><table><thead><tr><th>Page</th><th>H1</th><th>H2</th><th>Words</th><th>Imgs no alt</th><th>Claims</th><th>Schema</th></tr></thead><tbody>
      {list.map(r => (<tr key={r.id}><td className="small"><a href={r.page} target="_blank" rel="noreferrer">{r.page.replace(/^https?:\/\/[^/]+/, "") || "/"}</a></td>
        <td className={r.h1 !== 1 ? "down" : ""}>{r.h1}</td><td>{r.h2}</td><td>{r.words}</td><td className={r.images_no_alt ? "down" : ""}>{r.images_no_alt}/{r.images}</td>
        <td className="small">{(r.risky_claims || []).join(", ")}</td><td className="small">{(r.schema_issues || []).filter((s: string) => !s.startsWith("h1_")).join(", ")}</td></tr>))}
      {!list.length && <tr><td colSpan={7} className="muted">No audit data yet. Press "Run audit".</td></tr>}</tbody></table></div>
    <p className="muted small">Heading counts include Elementor heading widgets only (FAQ accordion titles and headings inside HTML blocks are not counted).</p>
  </Shell>);
}
