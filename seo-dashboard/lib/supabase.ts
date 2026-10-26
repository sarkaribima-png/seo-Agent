import { createClient } from "@supabase/supabase-js";
const url = process.env.NEXT_PUBLIC_SUPABASE_URL || "http://localhost:54321";
const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "missing-key";
export const configured = !!process.env.NEXT_PUBLIC_SUPABASE_URL && !!process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
export const supabase = createClient(url, key);
export async function runAgent(command: string, proposal_id?: number) {
  const { data } = await supabase.auth.getSession();
  const r = await fetch("/api/run", { method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${data.session?.access_token}` },
    body: JSON.stringify({ command, proposal_id }) });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok, message: j.message || j.error || "" };
}
