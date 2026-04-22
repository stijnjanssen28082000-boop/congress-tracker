export const revalidate = 3600;

export async function GET(request) {
  const { searchParams } = new URL(request.url);
  const chamber = searchParams.get("chamber") || "house";

  try {
    const res = await fetch(
      "https://api.quiverquant.com/beta/live/congresstrading",
      {
        headers: {
          "Accept": "application/json",
          "User-Agent": "Mozilla/5.0"
        },
        next: { revalidate: 3600 }
      }
    );

    if (!res.ok) throw new Error(`API failed: ${res.status}`);
    const data = await res.json();

    const trades = data
      .filter(t => !chamber || chamber === "all" || (t.Chamber || "").toLowerCase() === chamber)
      .sort((a, b) => new Date(b.TransactionDate) - new Date(a.TransactionDate))
      .slice(0, 500)
      .map(t => ({
        representative: t.Representative || "Onbekend",
        party: normalizeParty(t.Party),
        ticker: t.Ticker || null,
        asset_description: t.Asset || "—",
        type: t.Transaction?.toLowerCase().includes("purchase") ? "purchase" : "sale_full",
        amount: t.Range || "—",
        transaction_date: t.TransactionDate,
        chamber: (t.Chamber || chamber),
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
