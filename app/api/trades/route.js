export const revalidate = 1800;

export async function GET(request) {
  try {
    const pages = [1, 2, 3, 4, 5];
    const allTrades = [];

    for (const page of pages) {
      const res = await fetch(
        `https://www.capitoltrades.com/trades?per_page=96&page=${page}`,
        {
          headers: {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.capitoltrades.com/",
          },
          cache: "no-store"
        }
      );

      if (!res.ok) break;
      const html = await res.text();

      const rows = html.match(/<tr[^>]*data-row[^>]*>[\s\S]*?<\/tr>/g) || [];

      for (const row of rows) {
        const getText = (pattern) => {
          const m = row.match(pattern);
          return m ? m[1].replace(/<[^>]+>/g, '').trim() : null;
        };

        const politician = getText(/politician[^>]*>[\s\S]*?<span[^>]*>([\s\S]*?)<\/span>/);
        const party = getText(/party[^>]*>\s*<span[^>]*>([\s\S]*?)<\/span>/);
        const ticker = getText(/ticker[^>]*>\s*<span[^>]*>([\s\S]*?)<\/span>/);
        const asset = getText(/asset[^>]*>\s*<span[^>]*>([\s\S]*?)<\/span>/);
        const tradeType = getText(/trade-type[^>]*>\s*<span[^>]*>([\s\S]*?)<\/span>/);
        const amount = getText(/trade-size[^>]*>\s*<span[^>]*>([\s\S]*?)<\/span>/);
        const dateMatch = row.match(/(\d{4}-\d{2}-\d{2})/);
        const date = dateMatch ? dateMatch[1] : null;

        if (politician && date) {
          allTrades.push({
            representative: politician,
            party: normalizeParty(party),
            ticker: ticker && ticker !== "--" ? ticker : null,
            asset_description: asset || "—",
            type: (tradeType || "").toLowerCase().includes("buy") ? "purchase" : "sale_full",
            amount: amount || "—",
            transaction_date: date,
          });
        }
      }

      await new Promise(r => setTimeout(r, 500));
    }

    if (allTrades.length === 0) {
      throw new Error("Geen trades gevonden via scraping");
    }

    allTrades.sort((a, b) => new Date(b.transaction_date) - new Date(a.transaction_date));

    return Response.json({ trades: allTrades, updatedAt: new Date().toISOString() });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500 });
  }
}

function normalizeParty(party) {
  if (!party) return "Unknown";
  const p = (party || "").toLowerCase();
  if (p.includes("democrat") || p === "d") return "Democrat";
  if (p.includes("republican") || p === "r") return "Republican";
  if (p.includes("independent") || p === "i") return "Independent";
  return "Unknown";
}
