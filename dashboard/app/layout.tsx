import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Self-Healing Incident Agent",
  description:
    "Incidents detected by the monitor, diagnosed by the agent, and the fixes it proposed.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">{children}</div>
      </body>
    </html>
  );
}
