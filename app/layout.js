export const metadata = {
  title: "Congress Trade Tracker",
  description: "Real-time STOCK Act disclosures van Amerikaanse politici",
};

export default function RootLayout({ children }) {
  return (
    <html lang="nl">
      <body style={{ margin: 0, background: "#07070E" }}>{children}</body>
    </html>
  );
}
