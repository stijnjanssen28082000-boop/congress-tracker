export const revalidate = 3600;

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const chamber = searchParams.get("chamber") || "house";

  try {
    const res = await fetch(
      "https://us-congress-insider-trading-data.p.rapidapi.com/trades/recent?limit=500",
      {
        headers: {
          "x-rapidapi-key": process.env.RAPIDAPI_KEY,
          "x-rapidapi-host": "us-congress-insider-trading-data.p.rapidapi.com",
          "Content-Type": "application/json"
        },
        next: { revalidate: 3600 }
      }
    );

    if (!res.ok) throw new Error(`API failed: ${res.status}`);
    const data = await res.json();

    const list = Array.isArray(data) ? data : data.trades || data.data || data.results || [];

    const trades = list
      .filter(t => t.transaction_date || t.transactionDate || t.date)
      .sort((a, b) => new Date(b.transaction_date || b.transactionDate || b.date) - new Date(a.transaction_date || a.transactionDate || a.date))
      .slice(0, 500)
      .map(t => ({
        representative: t.representative || t.member || t.name || t.politician || "Onbekend",
        party: normalizeParty(t.party),
        ticker: t.ticker || t.symbol || null,
        asset_description: t.asset_description || t.asset || t.company || "—",
        type: (t.type || t.transaction || t.transaction_type || "").toLowerCase().includes("purchase") ? "purchase" : "sale_full",
        amount: t.amount || t.range || t.value || "—",
        transaction_date: t.transaction_date || t.transactionDate || t.date || "—",
        chamber: t.chamber || chamber,
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
