# SEO Agent Dashboard (Next.js + Supabase + Vercel)

Pages: Overview (KPIs, clicks/impressions, mobile vs desktop position, site mode switch), Proposals (Approve / Reject / Rollback), Audits (issue counts + table).

## Setup
1. Supabase SQL Editor: run `schema_v2.sql` (once).
2. Supabase -> Authentication -> Users -> Add user (your email + password). Then Authentication -> Sign In / Providers -> Email -> turn OFF "Allow new users to sign up".
3. Supabase -> Project Settings -> API: copy Project URL and the `anon` public key (NOT service_role).
4. Push this folder to a new GitHub repo (private) and import it in Vercel.
5. Vercel -> Settings -> Environment Variables:
   NEXT_PUBLIC_SUPABASE_URL, NEXT_PUBLIC_SUPABASE_ANON_KEY
   Optional ("Run now" button): GITHUB_TOKEN (fine-grained token, repo seo-Agent only, Actions: Read and write), GITHUB_REPO=sarkaribima-png/seo-Agent
6. Deploy, open the URL, sign in.

Security: the dashboard uses only the anon key + login; Row Level Security allows signed-in users only. The agent uses the service key inside GitHub Actions (never in Vercel).
