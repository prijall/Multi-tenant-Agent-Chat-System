import type { Metadata } from "next";
import "./globals.css";
import "./workspace.css";
import "./admin.css";
import "./chat.css";

export const metadata: Metadata = {
  description: "Open your organization's portal",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
