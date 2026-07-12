#!/usr/bin/env python3
"""
Build script for the SSDSE Dataset Repository website.

Reads the public Google Sheet of dataset submissions, then generates:
  - datasets.json          (feeds the sortable/filterable master table on the home page)
  - datasets/<slug>.qmd    (one Kaggle-style page per dataset, with a data explorer)

Data files are NOT copied into the repository. For the in-page data explorer, each
data file is downloaded at build time, a row-capped preview is embedded in the page,
and the temporary copy is discarded. Download buttons point at the original public
links from the sheet.

Run from the repository root:  python scripts/build.py
"""

import html
import io
import json
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------- config

SHEET_ID = "1tuCXok_y_zQpSDGssaypETf9lnB6nCh9XVoCiMaPCuc"
GID = "591469695"
SHEET_CSV_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&gid={GID}"
)
SHEET_VIEW_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid={GID}"

ROOT = Path(__file__).resolve().parent.parent
PAGES_DIR = ROOT / "datasets"
JSON_PATH = ROOT / "datasets.json"

PREVIEW_MAX_ROWS = 500          # rows embedded in the data explorer
PREVIEW_MAX_COLS = 60
PREVIEW_MAX_CELL_CHARS = 300
DOWNLOAD_MAX_BYTES = 30_000_000  # skip preview for files bigger than this
REQUEST_TIMEOUT = 60

# Column matching: sheet header -> internal key, matched by lowercase prefix so
# small wording tweaks in the form don't break the build.
COLUMN_PREFIXES = {
    "submission date": "date",
    "first name": "first_name",
    "last name": "last_name",
    "email": "email",
    "do you have permission": "permission",
    "dataset name": "name",
    "source or attribution": "source",
    "brief description": "description",
    "data file type": "file_type",
    "upload data": "upload_private",
    "url to access the data": "external_url",
    "supplementary material (codebook": "supp_private",
    "how clean are the data": "clean",
    "are the data real or synthetic": "real_synthetic",
    "how widely available is the data": "availability",
    "number of observations": "n_obs",
    "number of categorical": "n_cat",
    "number of numerical": "n_num",
    "what statistics topic": "topics",
    "what do you primarily use this dataset to teach": "primary_use",
    "is there any other information": "other_info",
    "lessons or activities": "lessons",
    "submission id": "submission_id",
    "data - public": "data_public",
    "supplementary material-public": "supp_public",
    "additional resources - public": "resources_public",
}

# ----------------------------------------------------------------------------- helpers


def clean_text(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none", "n/a"} else s


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "dataset"


def split_urls(cell: str) -> list[str]:
    """A sheet cell may hold several links separated by newlines/commas/spaces."""
    return re.findall(r"https?://[^\s,;]+", cell or "")


def split_topics(cell: str) -> list[str]:
    """Jotform multi-selects arrive newline- or comma-separated."""
    if not cell:
        return []
    parts = re.split(r"[\n;]+", cell)
    if len(parts) == 1:
        # Only split on commas if it doesn't look like a sentence.
        parts = cell.split(",") if cell.count(",") >= 1 and len(cell) < 400 else parts
    return [p.strip() for p in parts if p.strip()]


def yes(cell: str) -> bool:
    return clean_text(cell).lower().startswith("yes")


def e(v) -> str:
    """HTML-escape for interpolation into generated pages."""
    return html.escape(clean_text(v))


def json_for_script(obj) -> str:
    """JSON safe to embed inside a <script> tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def filename_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    return requests.utils.unquote(name.split("?")[0]) or "file"


def drive_direct(url: str) -> str:
    """Convert a Google Drive share/view link into a direct-download link."""
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
    if not m:
        m = re.search(r"drive\.google\.com/(?:open|uc)\?[^\s]*id=([\w-]+)", url)
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else url


def is_drive(url: str) -> bool:
    return "drive.google.com" in url


def label_for(url: str, hints: list[str], fallback: str) -> str:
    """Best display filename: real filename from URL, else a hint filename
    (e.g. from the private Jotform upload link), else a fallback label."""
    if not is_drive(url):
        name = filename_from_url(url)
        if "." in name:
            return name
    for h in hints:
        name = filename_from_url(h)
        if "." in name:
            return name
    return fallback


# ----------------------------------------------------------------------------- fetch sheet


def fetch_sheet() -> pd.DataFrame:
    print(f"Fetching sheet: {SHEET_CSV_URL}")
    r = requests.get(SHEET_CSV_URL, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text), dtype=str)
    df = df.dropna(axis=1, how="all")
    print(f"Sheet has {len(df)} rows, {len(df.columns)} non-empty columns")
    return df


def map_columns(df: pd.DataFrame) -> dict[str, str]:
    mapping = {}
    for col in df.columns:
        low = str(col).strip().lower()
        for prefix, key in COLUMN_PREFIXES.items():
            if low.startswith(prefix) and key not in mapping.values():
                mapping[col] = key
                break
    missing = set(COLUMN_PREFIXES.values()) - set(mapping.values())
    if missing:
        print(f"NOTE: sheet columns not found for: {sorted(missing)}")
    return mapping


# ----------------------------------------------------------------------------- records


def build_records(df: pd.DataFrame) -> list[dict]:
    colmap = map_columns(df)
    records, seen_slugs = [], set()

    for _, row in df.iterrows():
        rec = {key: clean_text(row.get(col)) for col, key in colmap.items()}

        if not rec.get("name"):
            continue
        # Respect the permission question: skip anything not affirmatively shareable.
        if rec.get("permission") and not yes(rec["permission"]):
            print(f"Skipping (no permission): {rec['name']}")
            continue

        rec["topics_list"] = split_topics(rec.get("topics", ""))
        rec["data_urls"] = [drive_direct(u) for u in split_urls(rec.get("data_public", ""))]
        rec["supp_urls"] = [drive_direct(u) for u in split_urls(rec.get("supp_public", ""))]
        rec["resource_urls"] = [drive_direct(u) for u in split_urls(rec.get("resources_public", ""))]
        rec["external_urls"] = split_urls(rec.get("external_url", ""))
        # Real filenames live in the private Jotform upload links; use them as labels.
        rec["data_name_hints"] = split_urls(rec.get("upload_private", ""))
        rec["supp_name_hints"] = split_urls(rec.get("supp_private", ""))
        rec["submitter"] = " ".join(
            p for p in [rec.get("first_name", ""), rec.get("last_name", "")] if p
        )
        # Keep only the date part of Jotform timestamps.
        rec["date_short"] = rec.get("date", "").split(" ")[0]

        slug = slugify(rec["name"])
        if slug in seen_slugs:
            suffix = slugify(rec.get("submission_id", "")) or str(len(records))
            slug = f"{slug}-{suffix[-8:]}"
        seen_slugs.add(slug)
        rec["slug"] = slug

        records.append(rec)

    print(f"Kept {len(records)} datasets")
    return records


# ----------------------------------------------------------------------------- data preview


def read_tabular(content: bytes, filename: str) -> pd.DataFrame | None:
    """Parse a data file, deciding format by magic bytes first, extension second."""
    name = filename.lower()
    is_xlsx = content[:4] == b"PK\x03\x04" or name.endswith((".xlsx", ".xlsm", ".xltx"))
    is_xls = content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".xls")
    try:
        if is_xlsx:
            return pd.read_excel(io.BytesIO(content), dtype=str)
        if is_xls:
            return pd.read_excel(io.BytesIO(content), dtype=str, engine="xlrd")
        # Otherwise assume delimited text and let pandas sniff the separator.
        text = content.decode("utf-8-sig", errors="replace")
        return pd.read_csv(io.StringIO(text), dtype=str, sep=None, engine="python")
    except Exception as exc:  # noqa: BLE001 - preview is best-effort
        print(f"  preview parse failed for {filename}: {exc}")
    return None


def column_stats(df: pd.DataFrame) -> list[dict]:
    """Kaggle-style per-column summaries, computed on the FULL file (not just the
    preview rows): histograms for numeric columns, top-value bars for categorical."""
    stats = []
    n_total = len(df)
    for col in df.columns[:PREVIEW_MAX_COLS]:
        vals = df[col].dropna().astype(str).str.strip()
        vals = vals[(vals != "") & (vals.str.lower() != "nan")]
        missing = n_total - len(vals)
        nunique = int(vals.nunique())
        num = pd.to_numeric(vals.str.replace(",", "", regex=False), errors="coerce")
        frac_numeric = float(num.notna().mean()) if len(vals) else 0.0

        entry = {
            "name": str(col),
            "missing": int(missing),
            "missing_pct": round(100 * missing / n_total, 1) if n_total else 0,
            "unique": nunique,
        }
        numv = num.dropna()
        if len(vals) and frac_numeric >= 0.9 and nunique > 5 and len(numv) > 1:
            counts, edges = np.histogram(numv, bins=10)
            entry.update(
                type="numeric",
                hist=[int(c) for c in counts],
                min=float(numv.min()),
                max=float(numv.max()),
                mean=float(numv.mean()),
            )
        else:
            vc = vals.value_counts()
            entry.update(
                type="categorical",
                top=[[str(k)[:60], int(v)] for k, v in vc.head(3).items()],
                other=int(vc.iloc[3:].sum()),
                total=int(len(vals)),
            )
        stats.append(entry)
    return stats


def fetch_file(url: str) -> tuple[bytes, str] | None:
    """Download a file, following Google Drive's virus-scan interstitial if needed.
    Returns (content, filename from Content-Disposition or URL)."""
    with requests.Session() as s:
        r = s.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "")
        if is_drive(url) and ctype.startswith("text/html"):
            # Large-file confirmation page: re-request with confirm token.
            m = re.search(r'name="confirm" value="([^"]+)"', r.text)
            token = m.group(1) if m else "t"
            sep = "&" if "?" in url else "?"
            r = s.get(f"{url}{sep}confirm={token}", timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            if r.headers.get("Content-Type", "").startswith("text/html"):
                return None
        if len(r.content) > DOWNLOAD_MAX_BYTES:
            return None
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
        fname = requests.utils.unquote(m.group(1)) if m else filename_from_url(url)
        return r.content, fname


def make_preview(rec: dict) -> dict | None:
    """Download the first parseable public data file and return a capped preview."""
    for url in rec["data_urls"]:
        try:
            fetched = fetch_file(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  download failed {url}: {exc}")
            continue
        if fetched is None:
            print(f"  no preview (file too large or not directly downloadable): {url}")
            continue
        content, fname = fetched
        # If the server gave no useful filename, fall back to the Jotform upload name.
        if "." not in fname:
            fname = label_for(url, rec.get("data_name_hints", []), rec.get("file_type", "data"))

        df = read_tabular(content, fname)
        if df is None or df.empty:
            continue

        total_rows, total_cols = df.shape
        stats = column_stats(df)  # computed on the full file
        df = df.iloc[:PREVIEW_MAX_ROWS, :PREVIEW_MAX_COLS].fillna("")
        cap = lambda s: s if len(s) <= PREVIEW_MAX_CELL_CHARS else s[: PREVIEW_MAX_CELL_CHARS - 1] + "…"
        rows = [[cap(str(v)) for v in row] for row in df.itertuples(index=False, name=None)]
        return {
            "file": fname,
            "columns": [str(c) for c in df.columns],
            "rows": rows,
            "stats": stats,
            "total_rows": int(total_rows),
            "total_cols": int(total_cols),
            "truncated": total_rows > PREVIEW_MAX_ROWS or total_cols > PREVIEW_MAX_COLS,
        }
    return None


# ----------------------------------------------------------------------------- page template


def download_buttons(rec: dict) -> str:
    btns = []
    for url in rec["data_urls"]:
        label = label_for(url, rec.get("data_name_hints", []), "data file")
        btns.append(f'<a class="btn btn-primary" href="{e(url)}">⬇ Download data ({e(label)})</a>')
    for url in rec["external_urls"]:
        btns.append(f'<a class="btn btn-outline-primary" href="{e(url)}">Access data (external site)</a>')
    for url in rec["supp_urls"]:
        label = label_for(url, rec.get("supp_name_hints", []), "supplementary file")
        btns.append(f'<a class="btn btn-outline-secondary" href="{e(url)}">Supplementary: {e(label)}</a>')
    for i, url in enumerate(rec["resource_urls"], 1):
        label = label_for(url, [], f"file {i}")
        btns.append(f'<a class="btn btn-outline-secondary" href="{e(url)}">Resource: {e(label)}</a>')
    return "\n".join(btns)


def meta_item(label: str, value: str) -> str:
    return f"<dt>{label}</dt><dd>{e(value)}</dd>" if clean_text(value) else ""


def stats_strip(rec: dict) -> str:
    stats = [
        (rec.get("n_obs", ""), "Observations"),
        (rec.get("n_cat", ""), "Categorical vars"),
        (rec.get("n_num", ""), "Numeric vars"),
    ]
    cells = "".join(
        f'<div class="stat"><div class="num">{e(v)}</div><div class="lbl">{l}</div></div>'
        for v, l in stats
        if clean_text(v)
    )
    return f'<div class="ds-stats-strip">{cells}</div>' if cells else ""


# Explorer JS is a plain string (not an f-string) so the braces stay literal.
EXPLORER_JS = """<script>
(function () {
  const pd = JSON.parse(document.getElementById("preview-data").textContent);

  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function fmt(x) {
    if (x == null || isNaN(x)) return "";
    const a = Math.abs(x);
    if (a >= 1e6) return (x / 1e6).toFixed(1).replace(/\\.0$/, "") + "M";
    if (a >= 1e3) return (x / 1e3).toFixed(1).replace(/\\.0$/, "") + "k";
    return String(Math.round(x * 100) / 100);
  }

  // ---- Kaggle-style column summary cards ----
  const cards = document.getElementById("col-cards");
  (pd.stats || []).forEach(st => {
    const card = document.createElement("div");
    card.className = "col-card";
    let body = "";

    if (st.type === "numeric") {
      const w = 134, h = 44, n = st.hist.length;
      const peak = Math.max(...st.hist, 1);
      const bw = w / n;
      let bars = "";
      st.hist.forEach((c, i) => {
        const bh = c === 0 ? 0 : Math.max(2, (c / peak) * (h - 2));
        bars += `<rect x="${(i * bw + 1).toFixed(1)}" y="${(h - bh).toFixed(1)}" ` +
                `width="${(bw - 2).toFixed(1)}" height="${bh.toFixed(1)}" rx="1">` +
                `<title>${fmt(c)} rows</title></rect>`;
      });
      body = `<svg viewBox="0 0 ${w} ${h}" class="cc-hist" preserveAspectRatio="none">${bars}</svg>
              <div class="cc-range"><span>${fmt(st.min)}</span><span>${fmt(st.max)}</span></div>`;
    } else {
      const total = st.total || 1;
      let rows = "";
      (st.top || []).forEach(([v, c]) => {
        const pct = Math.round(100 * c / total);
        rows += `<div class="cat-row" title="${esc(v)}: ${c}">
                   <span class="cat-name">${esc(v)}</span>
                   <div class="cat-bar-wrap"><div class="cat-bar" style="width:${pct}%"></div></div>
                   <span class="cat-pct">${pct}%</span>
                 </div>`;
      });
      if (st.other > 0) {
        const pct = Math.round(100 * st.other / total);
        rows += `<div class="cat-row other">
                   <span class="cat-name">Other (${fmt(st.other)})</span>
                   <div class="cat-bar-wrap"><div class="cat-bar" style="width:${pct}%"></div></div>
                   <span class="cat-pct">${pct}%</span>
                 </div>`;
      }
      body = rows || '<div class="cc-sub">no values</div>';
    }

    const subBits = [];
    if (st.type === "numeric") subBits.push("numeric");
    else subBits.push(`${fmt(st.unique)} unique`);
    if (st.missing > 0) subBits.push(`${st.missing_pct}% missing`);

    card.innerHTML = `<div class="cc-name" title="${esc(st.name)}">${esc(st.name)}</div>
                      <div class="cc-sub">${subBits.join(" · ")}</div>${body}`;
    cards.appendChild(card);
  });

  // ---- scrollable data table ----
  $("#preview-table").DataTable({
    data: pd.rows,
    columns: pd.columns.map(c => ({ title: esc(c) })),
    pageLength: 15,
    lengthMenu: [15, 50, 100],
    scrollX: true,
    autoWidth: false
  });
})();
</script>"""


def explorer_html(rec: dict, preview: dict | None) -> str:
    if preview is None:
        return (
            '<p class="ds-explorer-note">No in-page preview is available for this dataset. '
            "Use the download or access buttons above to get the data.</p>"
        )
    note = f"{preview['total_rows']:,} rows × {preview['total_cols']} columns"
    if preview["truncated"]:
        note += f" — showing the first {min(preview['total_rows'], PREVIEW_MAX_ROWS):,} rows"
    note += f" of <code>{e(preview['file'])}</code>. Download the file for the full data."
    payload = json_for_script(
        {"columns": preview["columns"], "rows": preview["rows"], "stats": preview["stats"]}
    )
    return f"""<p class="ds-explorer-note">{note}</p>
<div class="ds-col-cards" id="col-cards"></div>
<div class="ds-explorer">
  <table id="preview-table" class="table table-striped table-sm" style="width:100%" data-quarto-disable-processing="true"></table>
</div>
<script id="preview-data" type="application/json">{payload}</script>
{EXPLORER_JS}"""


def section(title: str, body_html: str) -> str:
    return f'<div class="ds-section"><h2>{title}</h2>{body_html}</div>' if body_html else ""


def para(text: str) -> str:
    return f"<p>{e(text)}</p>" if clean_text(text) else ""


def render_page(rec: dict, preview: dict | None) -> str:
    topics = "".join(f'<span class="topic-badge">{e(t)}</span> ' for t in rec["topics_list"])

    contact = ""
    if rec.get("email"):
        contact = f'<dt>Contact</dt><dd><a href="mailto:{e(rec["email"])}">{e(rec["email"])}</a></dd>'

    meta = "".join(
        [
            meta_item("Source / attribution", rec.get("source", "")),
            meta_item("Data file type", rec.get("file_type", "")),
            meta_item("Cleanliness", rec.get("clean", "")),
            meta_item("Real or synthetic", rec.get("real_synthetic", "")),
            meta_item("Availability", rec.get("availability", "")),
            meta_item("Submitted by", rec.get("submitter", "")),
            contact,
            meta_item("Submission date", rec.get("date_short", "")),
        ]
    )

    about = "".join(
        [
            para(rec.get("description", "")),
            section("Primarily used to teach", para(rec.get("primary_use", ""))),
            section("Additional notes", para(rec.get("other_info", ""))),
            section("Lessons &amp; activities", para(rec.get("lessons", ""))),
        ]
    )

    title_json = json.dumps(rec["name"])
    desc_json = json.dumps(rec.get("description", "")[:160])

    return f"""---
title: {title_json}
description: {desc_json}
page-layout: full
---

```{{=html}}
<div class="ds-header">
  <div>{topics}</div>
  <div class="ds-downloads">
{download_buttons(rec)}
  </div>
</div>

{stats_strip(rec)}

<div class="ds-layout">
  <div>
    <div class="ds-section">
      <h2>About this dataset</h2>
      {about}
    </div>
    <div class="ds-section">
      <h2>Data explorer</h2>
      {explorer_html(rec, preview)}
    </div>
  </div>
  <aside>
    <div class="ds-meta-card">
      <h3>Details</h3>
      <dl>{meta}</dl>
    </div>
  </aside>
</div>

<p class="mt-4"><a href="../index.html">← Back to all datasets</a></p>
```
"""


# ----------------------------------------------------------------------------- main


def main() -> int:
    df = fetch_sheet()
    records = build_records(df)
    if not records:
        print("ERROR: no datasets found — refusing to wipe the site.")
        return 1

    PAGES_DIR.mkdir(exist_ok=True)
    for old in PAGES_DIR.glob("*.qmd"):
        old.unlink()

    index_rows = []
    for rec in records:
        print(f"Building page: {rec['slug']}")
        preview = make_preview(rec)
        (PAGES_DIR / f"{rec['slug']}.qmd").write_text(render_page(rec, preview), encoding="utf-8")
        index_rows.append(
            {
                "name": rec["name"],
                "href": f"datasets/{rec['slug']}.html",
                "description": rec.get("description", ""),
                "topics": rec["topics_list"],
                "file_type": rec.get("file_type", ""),
                "clean": rec.get("clean", ""),
                "real_synthetic": rec.get("real_synthetic", ""),
                "availability": rec.get("availability", ""),
                "n_obs": rec.get("n_obs", ""),
                "n_cat": rec.get("n_cat", ""),
                "n_num": rec.get("n_num", ""),
                "submitter": rec.get("submitter", ""),
                "date": rec.get("date_short", ""),
            }
        )

    JSON_PATH.write_text(json.dumps(index_rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {JSON_PATH} and {len(records)} pages in {PAGES_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
