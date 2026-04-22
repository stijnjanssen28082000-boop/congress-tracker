export const revalidate = 3600;

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const chamber = searchParams.get("chamber") || "house";

  try {
    const urls = [
      "https://efts.congress.gov/ESPCH/search.json?q=%7B%22source%22%3A%22members%22%2C%22type%22%3A%22PT%22%7D&pageSize=250&page=1",
      "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2025/ptr-data-2025.json"
    ];

    let data = null;

    const res1 = await fetch(
      "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
      { headers: { "User-Agent": "Mozilla/5.0", "Accept": "application/json" }, cache: "no-store" }
    );

    if (res1.ok) {
      data = await res1.json();
    } else {
      const res2 = await fetch(
        "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
        { headers: { "User-Agent": "Mozilla/5.0", "Accept": "application/json" }, cache: "no-store" }
      );
      if (res2.ok) data = await res2.json();
    }

    if (!data || !Array.isArray(data) || data.length === 0) {
      throw new Error("Geen data ontvangen");
    }

    const trades = data
      .filter(t => t.transaction_date && t.transaction_date !== "N/A")
      .sort((a, b) => new Date(b.transaction_date) - new Date(a.transaction_date))
      .slice(0, 500)
      .map(t => ({
        representative: t.representative || t.senator || "Onbekend",
        party: normalizeParty(t.party),
        ticker: t.ticker && t.ticker !== "--" ? t.ticker.trim() : null,
        asset_description: t.asset_description || "—",
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
