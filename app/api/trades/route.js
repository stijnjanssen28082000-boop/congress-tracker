export const revalidate = 3600;

export async function GET(request) {
  try {
    const res = await fetch(
      "https://capitol-trades.com/api/trades?page=1&page_size=100&order_by=-traded_at",
      {
        headers: {
          "Accept": "application/json",
          "User-Agent": "Mozilla/5.0"
        },
        cache: "no-store"
      }
    );

    if (!res.ok) throw new Error(`API failed: ${res.status}`);
    const data = await res.json();
    const list = data.results || data.trades || data || [];

    const trades = list.map(t => ({
      representative: t.politician?.name || t.name || "Onbekend",
      party: normalizeParty(t.politician?.party || t.party),
      ticker: t.asset?.ticker || t.ticker || null,
      asset_description: t.asset?.name || t.asset_description || "—",
      type: (t.order_type || t.type || "").toLowerCase().includes("buy") ? "purchase" : "sale_full",
      amount: t.amount || t.size || "—",
      transaction_date: t.traded_at?.slice(0, 10) || t.transaction_date || "—",
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
