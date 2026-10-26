import "./globals.css";
export const metadata = { title: "SEO Agent", description: "SEO agent control room" };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (<html lang="en"><body>{children}</body></html>);
}
