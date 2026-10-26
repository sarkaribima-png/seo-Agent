"use client";
export default function SiteSelect({ sites, value, onChange }: { sites: any[]; value: string; onChange: (v: string) => void }) {
  return (<select value={value} onChange={e => onChange(e.target.value)}>{sites.map(s => <option key={s.id} value={s.id}>{s.domain}</option>)}</select>);
}
