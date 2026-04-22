"use client";
import { useState, useEffect, useMemo } from "react";

const PARTY_COLOR = {
  Democrat: "#4B9EF4",
  Republican: "#F45B5B",
  Independent: "#A78BFA",
  Unknown: "#555",
};

const AMOUNT_ORDER = [
  "Over $5,000,000",
  "$1,000,001 - $5,000,000",
  "$500,001 - $1,000,000",
  "$250,001 - $500,000",
  "$100,001 - $250,000",
  "$50,001 - $100,000",
  "$15,001 - $50,000",
  "$1,001 - $15,000",
];

function formatAmount(amount) {
  const map = {
    "Over $5,000,000": ">$5M",
    "$1,000,001 - $5,000,000": "~$3M",
    "$500,001 - $1,000,000": "~$750K",
    "$250,001 - $500,000": "~$375K",
    "$100,001 - $250,000": "~$175K",
    "$50,001 - $100,000": "~$75K",
    "$15,001 - $50,000": "~$33K",
    "$1,001 - $15,000": "~$8K",
  };
  return map[amount] || amount || "—";
}

function formatDate(d) {
  if (!d || d === "N/A") return "—";
  return new Date(d).toLocaleDateString("nl-NL", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

function daysSince(d) {
  if (!d) return null;
  const diff = Math.floor((Date.now() - new Date(d)) / 86400000);
  if (diff === 0) return "vandaag";
  if (diff === 1) return "gisteren";
  return `${diff}d geleden`;
}

export default function Home() {
  const [trades, setTrades] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [chamber, setChamber] = useState("house");
  const [search, setSearch] = useState("");
  const [partyFilter, setPartyFilter] = useState("All");
  const [typeFilter, setTypeFilter] = useState("All");
  const [sortBy, setSortBy] = useState("date");
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 50;

  useEffect(() => { load(chamber); }, [chamber]);

  async function load(c) {
    setLoading(true); setError(null); setPage(0);
    try {
      const res = await fetch(`/api/trades?chamber=${c}`);
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      setTrades(data.trades); setUpdatedAt(data.updatedAt);
    } catch (e) { setError(e.message); } finally { setLoading(false); }
  }

  const filtered = useMemo(() => trades.filter(t => {
    const matchParty = partyFilter === "All" || t.party === partyFilter;
    const matchType = typeFilter === "All" ||
      (typeFilter === "purchase" && t.type?.toLowerCase().includes("purchase")) ||
      (typeFilter === "sale" && t.type?.toLowerCase().includes("sale"));
    const q = search.toLowerCase();
    const matchSearch = !q || t.representative?.toLowerCase().includes(q) ||
      t.ticker?.toLowerCase().includes(q) || t.asset_description?.toLowerCase().includes(q);
    return matchParty && matchType && matchSearch;
  }).sort((a, b) => sortBy === "date"
    ? new Date(b.transaction_date) - new Date(a.transaction_date)
    : AMOUNT_ORDER.indexOf(a.amount) - AMOUNT_ORDER.indexOf(b.amount)
  ), [trades, partyFilter, typeFilter, search, sortBy]);

  const paginated = filtered.slice(0, (page + 1) * PAGE_SIZE);
  const stats = useMemo(() => ({
    total: trades.length,
    buys: trades.filter(t => t.type?.toLowerCase().includes("purchase")).length,
    sells: trades.filter(t => t.type?.toLowerCase().includes("sale")).length,
    politicians: [...new Set(trades.map(t => t.representative))].length,
  }), [trades]);

  const topTickers = useMemo(() => {
    const counts = {};
    trades.filter(t => t.ticker).forEach(t => { counts[t.ticker] = (counts[t.ticker] || 0) + 1; });
    return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 6);
  }, [trades]);

  return (
    <div style={{ minHeight: "100vh", background: "#07070E", color: "#E0E0F0", fontFamily: "'Courier New', monospace" }}>
      <div style={{ position: "fixed", inset: 0, pointerEvents: "none", zIndex: 0, backgroundImage: "repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.04) 2px,rgba(0,0,0,0.04) 4px)" }} />
      <div style={{ position: "relative", zIndex: 1, maxWidth: 1100, margin: "0 auto", padding: "0 16px 80px" }}>
        <header style={{ padding: "28px 0 22px", borderBottom: "1px solid #181828" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 12 }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
                <div style={{ width: 8, height: 8, borderRadius: "50%", background: loading ? "#FFB800" : error ? "#FF4C6A" : "#00FF9C", animation: "blink 1.8s infinite" }} />
                <span style={{ fontSize: 10, letterSpacing: 3, color: loading ? "#FFB800" : error ? "#FF4C6A" : "#00FF9C" }}>
                  {loading ? "LIVE DATA OPHALEN..." : error ? "VERBINDINGSFOUT" : `LIVE • ${updatedAt ? formatDate(updatedAt) : ""}`}
                </span>
              </div>
              <h1 style={{ margin: 0, fontSize: "clamp(20px, 4vw, 40px)", fontWeight: 900, background: "linear-gradient(100deg,#fff 30%,#00FF9C 100%)", WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>CONGRESS TRADE TRACKER</h1>
              <p style={{ margin: "6px 0 0", fontSize: 10, color: "#2A2A44", letterSpacing: 2 }}>REAL-TIME STOCK ACT DISCLOSURES</p>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              {[["house", "HUIS"], ["senate", "SENAAT"]].map(([c, label]) => (
                <button key={c} onClick={() => setChamber(c)} style={{ background: chamber === c ? "#00FF9C" : "#0D0D1A", border: `1px solid ${chamber === c ? "transparent" : "#252535"}`, color: chamber === c ? "#000" : "#555", padding: "10px 18px", borderRadius: 8, fontFamily: "inherit", fontSize: 12, fontWeight: 700, cursor: "pointer" }}>{label}</button>
              ))}
            </div>
          </div>
        </header>

        {error && <div style={{ padding: "30px 0", textAlign: "center", color: "#FF4C6A" }}>Fout: {error}</div>}

        {!loading && !error && (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 8, padding: "18px 0 12px" }}>
              {[["TRANSACTIES", stats.total, "#E0E0F0"], ["AANKOPEN", stats.buys, "#00FF9C"], ["VERKOPEN", stats.sells, "#FF4C6A"], ["POLITICI", stats.politicians, "#4B9EF4"]].map(([l, v, c]) => (
                <div key={l} style={{ background: "#0D0D1A", border: "1px solid #181828", borderRadius: 8, padding: "12px 14px" }}>
                  <div style={{ fontSize: 9, color: "#2A2A44", letterSpacing: 2, marginBottom: 4 }}>{l}</div>
                  <div style={{ fontSize: 22, fontWeight: 900, color: c }}>{v}</div>
                </div>
              ))}
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", paddingBottom: 14, borderBottom: "1px solid #181828" }}>
              <span style={{ fontSize: 9, color: "#2A2A44", letterSpacing: 2, alignSelf: "center" }}>POPULAIR:</span>
              {topTickers.map(([ticker, count]) => (
                <button key={ticker} onClick={() => setSearch(ticker)} style={{ background: "#0D0D1A", border: "1px solid #252535", color: "#4B9EF4", padding: "4px 10px", borderRadius: 4, fontFamily: "inherit", fontSize: 11, fontWeight: 900, cursor: "pointer" }}>{ticker} ×{count}</button>
              ))}
            </div>
          </>
        )}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 7, padding: "14px 0", borderBottom: "1px solid #181828", alignItems: "center" }}>
          <input placeholder="Naam, ticker, bedrijf..." value={search} onChange={e => { setSearch(e.target.value); setPage(0); }} style={{ background: "#0D0D1A", border: "1px solid #252535", color: "#E0E0F0", padding: "8px 12px", borderRadius: 6, fontFamily: "inherit", fontSize: 12, width: 210, outline: "none" }} />
          {["All", "Democrat", "Republican"].map(p => (
            <button key={p} onClick={() => { setPartyFilter(p); setPage(0); }} style={{ background: partyFilter === p ? (p === "Democrat" ? "#4B9EF4" : p === "Republican" ? "#F45B5B" : "#00FF9C") : "#0D0D1A", border: "1px solid #252535", color: partyFilter === p ? "#000" : "#555", padding: "8px 10px", borderRadius: 6, fontFamily: "inherit", fontSize: 10, fontWeight: 700, cursor: "pointer" }}>{p === "All" ? "ALLE" : p.toUpperCase()}</button>
          ))}
          {[["All", "ALLES"], ["purchase", "KOOP"], ["sale", "VERKOOP"]].map(([val, label]) => (
            <button key={val} onClick={() => { setTypeFilter(val); setPage(0); }} style={{ background: typeFilter === val ? "#ffffff15" : "#0D0D1A", border: "1px solid #252535", color: typeFilter === val ? "#fff" : "#444", padding: "8px 10px", borderRadius: 6, fontFamily: "inherit", fontSize: 10, fontWeight: 700, cursor: "pointer" }}>{label}</button>
          ))}
        </div>

        {loading && <div style={{ padding: "80px 0", textAlign: "center", color: "#FFB800", fontSize: 12, letterSpacing: 3 }}>LIVE DATA OPHALEN...</div>}

        {!loading && !error && (
          <>
            <div style={{ padding: "12px 0 6px", fontSize: 10, color: "#2A2A44", letterSpacing: 2 }}>{filtered.length} TRANSACTIES</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {paginated.map((t, i) => {
                const isBuy = t.type?.toLowerCase().includes("purchase");
                const isSell = t.type?.toLowerCase().includes("sale");
                const isRecent = t.transaction_date && (Date.now() - new Date(t.transaction_date)) < 7 * 86400000;
                return (
                  <div key={i} style={{ display: "grid", gridTemplateColumns: "3px 1fr auto 100px 120px 96px", background: "#0C0C18", border: "1px solid #161626", borderRadius: 6, overflow: "hidden" }}>
                    <div style={{ background: PARTY_COLOR[t.party] || "#555" }} />
                    <div style={{ padding: "11px 14px" }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: "#DDD" }}>{t.representative} {isRecent && <span style={{ fontSize: 8, background: "#00FF9C18", color: "#00FF9C", border: "1px solid #00FF9C33", padding: "1px 5px", borderRadius: 3 }}>NIEUW</span>}</div>
                      <div style={{ fontSize: 10, color: "#252540" }}>{t.asset_description?.slice(0, 50)}</div>
                    </div>
                    <div style={{ padding: "11px 14px", display: "flex", alignItems: "center" }}>
                      {t.ticker ? <span style={{ background: "#181830", border: "1px solid #2A2A4A", color: "#4B9EF4", padding: "3px 9px", borderRadius: 4, fontSize: 12, fontWeight: 900 }}>{t.ticker}</span> : <span style={{ color: "#1E1E30" }}>—</span>}
                    </div>
                    <div style={{ padding: "11px 8px", display: "flex", alignItems: "center" }}>
                      <span style={{ fontSize: 10, fontWeight: 700, padding: "3px 7px", borderRadius: 4, background: isBuy ? "#00FF9C10" : isSell ? "#FF4C6A10" : "#ffffff05", color: isBuy ? "#00FF9C" : isSell ? "#FF4C6A" : "#444", border: `1px solid ${isBuy ? "#00FF9C30" : isSell ? "#FF4C6A30" : "#1E1E30"}` }}>{isBuy ? "KOOP" : isSell ? "VERKOOP" : "—"}</span>
                    </div>
                    <div style={{ padding: "11px 8px", display: "flex", alignItems: "center" }}>
                      <span style={{ fontSize: 12, color: "#B0B0C8", fontWeight: 600 }}>{formatAmount(t.amount)}</span>
                    </div>
                    <div style={{ padding: "11px 12px", display: "flex", alignItems: "center" }}>
                      <span style={{ fontSize: 10, color: "#252540" }}>{formatDate(t.transaction_date)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
            {paginated.length < filtered.length && (
              <div style={{ textAlign: "center", padding: "24px 0" }}>
                <button onClick={() => setPage(p => p + 1)} style={{ background: "#0D0D1A", border: "1px solid #252535", color: "#666", padding: "12px 32px", borderRadius: 8, fontFamily: "inherit", fontSize: 11, cursor: "pointer" }}>LAAD MEER ({filtered.length - paginated.length} resterend)</button>
              </div>
            )}
          </>
        )}
      </div>
      <style>{`@keyframes blink{0%,100%{opacity:1}50%{opacity:.15}}*{box-sizing:border-box;margin:0;padding:0}body{background:#07070E}`}</style>
    </div>
  );
}
