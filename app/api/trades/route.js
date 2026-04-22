export const revalidate = 3600;

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const chamber = searchParams.get("chamber") || "house";

  const url = chamber === "senate"
    ? "https://efts.congress.gov/ESPCH/search.json?q=%7B%22source%22%3A%22members%22%7D&dateIsW=transaction_date&dateRange=custom&startdate=2024-01-01&enddate=2025-12-31&senate=true"
    : "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json";

  try {
    const res = await fetch(url, {
      headers: { "User-Agent": "Mozilla/5.0" },
      next: { revalidate: 3600 }
    });

    if (!res.ok) throw new Error(`API failed: ${res.status}`);
    const data = await res.json();

    const trades = (Array.isArray(data) ? data : data.results || [])
      .filter(t => t.transaction_date && t.transaction_date !== "N/A")
      .sort((a, b) => new Date(b.transaction_date) - new Date(a.transaction_date))
      .slice(0, 500)
      .map(t => ({
        representative: t.representative || t.senator || t.name || "Onbekend",
        party: normalizeParty(t.party),
        ticker: t.ticker && t.ticker !== "--" ? t.ticker.trim() : null,
        asset_description: t.asset_description || t.asset_type || "—",
        type: t.type || "unknown",
        amount: t.amount || "—",
        transaction_date: t.transaction_date,
        chamber,
      }));

    return Response.json({ trades, updatedAt: new Date().toISOString() });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500 });
  }
}

function normalizeParty(party) {
  if (!party) return "Unknown";
  const p = party.toLowerCase();
  if (p.includes("democrat") || p === "d") return "Democrat";
  if (p.includes("republican") || p === "r") return "Republican";
  if (p.includes("independent") || p === "i") return "Independent";
  return party;
}
