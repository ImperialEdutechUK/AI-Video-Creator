export const metadata = {
  title: "SLC Video Merger",
  description: "Upload a NotebookLM video, get back a branded, watermark-free version.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          fontFamily:
            "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
          background: "#0a2a3c",
          color: "#ffffff",
          minHeight: "100vh",
        }}
      >
        {children}
      </body>
    </html>
  );
}
