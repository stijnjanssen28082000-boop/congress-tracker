export const revalidate = 1800;

export async function GET(request) {
  try {
    const res = await fetch(
      "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult?lastName=&firstName=&periodOfReport=&filingYear=2025&clerkFilter=PTR&State=00&pageNumber=1",
      {
        headers: {
          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
          "Accept": "application/json, text/html, */*",
          "X-Requested-With": "XMLHttpRequest",
        },
        cache: "no-store"
      }
    );

    if (!res.ok) throw new Error(`House.gov failed: ${res.status}`);
    const data = await res.json();

    const list = data.data || data.results || data || [];

    const trades = list
      .filter(t => t.FilingDate || t.Name)
      .map(t => ({
        representative: t.Name || t.FirstName + " " + t.LastName || "Onbekend",
        party: "Unknown",
        ticker: null,
        asset_description: t.DocType || "Periodic Transaction Report",
        type: "purchase",
        amount: "—",
        transaction_date: t.FilingDate?.slice(0, 10) || t.StateName || "—",
        filing_url: t.URL ? `https://disclosures-clerk.house.gov${t.URL}` : null,
      }));

    if (trades.length === 0) throw new Error("Geen data van house.gov");

    return Response.json({ trades, updatedAt: new Date().toISOString() });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500 });
  }
}
