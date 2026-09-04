import "./globals.css";

export const metadata = {
  title: "SLC Video Merger",
  description: "Upload a NotebookLM video, get back a branded, watermark-free version.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
