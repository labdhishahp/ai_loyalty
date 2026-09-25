import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";
import { SessionProvider } from "@/components/Session";
import { SignIn } from "@/components/SignIn";

export const metadata: Metadata = {
  title: "L-Mart Loyalty Operations",
  description: "AI investigation, campaign proposals and approvals for L-Mart.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <SessionProvider>
          <header className="top">
            <div className="shell">
              <div className="brand">L-Mart <span>Loyalty Operations</span></div>
              <Nav />
              <SignIn />
            </div>
          </header>
          <main className="shell">{children}</main>
        </SessionProvider>
      </body>
    </html>
  );
}
