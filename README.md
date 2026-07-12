# SSDSE Dataset Repository Website

A [Quarto](https://quarto.org) website that displays the datasets submitted to the SSDSE Dataset
Repository [Google Sheet](https://docs.google.com/spreadsheets/d/1tuCXok_y_zQpSDGssaypETf9lnB6nCh9XVoCiMaPCuc/edit?gid=591469695).
The home page is a sortable, filterable master table of all datasets; each dataset gets its own
Kaggle-style page with a scrollable data explorer, submitter-provided details, and download buttons.

The site rebuilds itself automatically from the Google Sheet on a schedule, so new form submissions
appear without any manual work. **No data files are stored in this repository** — download buttons
point at the public Google Drive links in the sheet, and the in-page data previews are generated at
build time and embedded in the pages.

## How it works

```
Google Form (Jotform) ──> Google Sheet ──> scripts/build.py ──> quarto render ──> GitHub Pages
                                             (runs on a schedule via GitHub Actions)
```

`scripts/build.py` downloads the sheet as CSV, keeps rows that have a dataset name and permission
to share, then writes:

- `datasets.json` — feeds the master table on the home page
- `datasets/<slug>.qmd` — one page per dataset, with a row-capped data preview embedded

Both are regenerated on every build and are gitignored.

## One-time setup on GitHub

1. Create a new **public** repository on GitHub (e.g. `ssdse-dataset-repository`).
2. Push this folder to it:

   ```bash
   git init
   git add .
   git commit -m "Initial site"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
   git push -u origin main
   ```

3. On GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. Go to the **Actions** tab, select **Build and deploy site**, and click **Run workflow** for the
   first build. (Pushes to `main` also trigger a build.)

The site will be live at `https://YOUR-USERNAME.github.io/YOUR-REPO/`.

## Changing the update schedule

Edit `.github/workflows/publish.yml`. It currently runs daily at 6:00 UTC:

```yaml
schedule:
  - cron: "0 6 * * *"   # daily
```

For hourly, use `"0 * * * *"`. You can always also run it manually from the Actions tab.

> Note: GitHub may pause scheduled workflows in repositories with no activity for 60 days;
> if that happens, a button appears in the Actions tab to re-enable them.

## Previewing locally

Requires Python 3.10+ and [Quarto](https://quarto.org/docs/get-started/).

```bash
pip install -r scripts/requirements.txt
python scripts/build.py     # fetch sheet + generate pages
quarto preview              # local preview server
```

## Adjusting things

- **Sheet/form links, site title, navbar** — `_quarto.yml`
- **Which columns show in the master table** — `index.qmd` and the `index_rows` block in `scripts/build.py`
- **Dataset page layout** — `render_page()` in `scripts/build.py`
- **Preview size caps** (rows/columns/file size) — constants at the top of `scripts/build.py`
- **Look and feel** — `custom.scss`
