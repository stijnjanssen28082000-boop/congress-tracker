export const revalidate = 3600;

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const chamber = searchParams.get("chamber") || "house";

  try {
    let data;
    if (chamber === "senate") {
      const res = await fetch("https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json", { next: { revalidate: 3600 } });
      if (!res.ok) throw new Error("Senate API failed");
      data = await res.json();
    } else {
      const res = await fetch("https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json", { next: { revalidate: 3600 } });
      if (!res.ok) throw new Error("House API failed");
      data = await res.json();
    }

    const trades = data
      .filter(t => t.transaction_date && t.transaction_date !== "N/A")
      .sort((a, b) => new Date(b.transaction_date) - new Date(a.transaction_date))
      .slice(0, 500)
      .map(t => ({
        representative: t.representative || t.senator || "Onbekend",
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
