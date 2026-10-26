"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { supabase, configured } from "@/lib/supabase";
export default function Login() {
  const router = useRouter(); const [email, setEmail] = useState(""); const [pw, setPw] = useState(""); const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  async function go(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setErr("");
    const { error } = await supabase.auth.signInWithPassword({ email, password: pw });
    setBusy(false); if (error) setErr(error.message); else router.replace("/");
  }
  return (<div className="center"><form className="card" style={{ width: 340 }} onSubmit={go}>
    <h1>SEO Agent</h1>
    {!configured && <p className="down small">Supabase env vars are missing (NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY).</p>}
    <div className="grid"><input type="email" placeholder="Email" value={email} onChange={e => setEmail(e.target.value)} required />
      <input type="password" placeholder="Password" value={pw} onChange={e => setPw(e.target.value)} required />
      <button className="pri" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      {err && <span className="down small">{err}</span>}</div></form></div>);
}
