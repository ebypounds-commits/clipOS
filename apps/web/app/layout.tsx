import type { ReactNode } from "react";

export const metadata = {
  title: "ClipOS",
  description: "Local control surface skeleton",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
