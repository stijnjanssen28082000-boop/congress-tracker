export const revalidate = 3600;

export async function GET(request) {
  try {
    const results = [];
    for (let offset = 0; offset < 500; offset += 20) {
      const res = await fetch(
        `https://us-congress-insider-trading-data.p.rapidapi.com/trades/latest?limit=20&offset=${offset}`,
        {
          headers: {
            "x-rapidapi-key": process.env.RAPIDAPI_KEY,
            "x-rapidapi-host": "us-congress-insider-trading-data.p.rapidapi.com",
            "Content-Type": "application/json"
          },
          next: { revalidate: 3600 }
        }
      );
      if (!res.ok) break;
      const data = await res.json();
      const list = Array.isArray(data) ? data : data.trades || data.data || data.results || [];
      if (list.length === 0) break;
      results.push(...list);
      if (results.length >= 200) break;
    }

    const trades = results
      .sort((a, b) => new Date(b.transaction_date || b.transactionDate || b.date || 0) - new Date(a.transaction_date || a.transactionDate || a.date || 0))
      .map(t => ({
        representative: t.representative || t.member || t.name || t.politician || "Onbekend",
        party: normalizeParty(t.party),
        ticker: t.ticker || t.symbol || null,
        asset_description: t.asset_description || t.asset || t.company || "—",
        type: (t.type || t.transaction || t.transaction_type || "").toLowerCase().includes("purchase") ? "purchase" : "sale_full",
        amount: t.amount || t.range || t.value || "—",
        transaction_date: t.transaction_date || t.transactionDate || t.date || "—",
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
