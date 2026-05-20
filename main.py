"""
Entry point for the DBD Financial Data Scraper.

Usage:
    python main.py                          # uses config.yaml defaults
    python main.py --config custom.yaml
    python main.py --no-resume              # reprocess all companies
    python main.py --export-only            # skip scraping, export DB → Excel
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from database import Database
from exporter import export_to_excel
from processor import process_batch
from scraper import DBDScraper


# ── logging setup ─────────────────────────────────────────────────────────────

def _setup_logging(log_file: str):
    fmt = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # Suppress noisy playwright internals
    logging.getLogger("playwright").setLevel(logging.WARNING)


# ── config loader ─────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── company list reader ───────────────────────────────────────────────────────

def read_company_list(file_path: str) -> list[dict]:
    """
    Read CSV or Excel company list.
    Expected columns: company_name, registration_number (optional)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    suffix = path.suffix.lower()

    if suffix in (".xlsx", ".xls"):
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip().lower() if h else "" for h in rows[0]]
        companies = []
        for row in rows[1:]:
            record = dict(zip(headers, row))
            company_name = str(record.get("company_name") or "").strip()
            reg_num = str(record.get("registration_number") or "").strip() or None
            if company_name:
                companies.append({"company_name": company_name, "registration_number": reg_num})
        return companies

    else:  # assume CSV
        import csv
        with open(file_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            companies = []
            for row in reader:
                company_name = (row.get("company_name") or "").strip()
                reg_num = (row.get("registration_number") or "").strip() or None
                if company_name:
                    companies.append({"company_name": company_name, "registration_number": reg_num})
        return companies


# ── core scrape loop ──────────────────────────────────────────────────────────

async def scrape_all(
    companies: list[dict],
    config: dict,
    db: Database,
    resume: bool,
):
    logger = logging.getLogger("main")
    batch_size = config.get("scraper", {}).get("batch_size", 10)

    already_done = db.get_processed_companies() if resume else set()
    pending = [c for c in companies if c["company_name"] not in already_done]

    total   = len(companies)
    skipped = total - len(pending)
    logger.info(
        "Companies: %d total, %d skipped (already done), %d to scrape",
        total, skipped, len(pending),
    )

    scraper = DBDScraper(config)
    await scraper.start()

    success_count = 0
    fail_count    = 0

    try:
        # Process in batches
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start: batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (len(pending) + batch_size - 1) // batch_size
            logger.info("── Batch %d/%d (%d companies) ──", batch_num, total_batches, len(batch))

            for company in batch:
                name   = company["company_name"]
                reg_no = company.get("registration_number")

                try:
                    raw_records = await scraper.scrape_company(name, reg_no)
                    cleaned     = process_batch(raw_records)

                    if cleaned:
                        inserted = db.upsert_many(cleaned)
                        db.mark_progress(name, reg_no, "done")
                        logger.info(
                            "Saved %d record(s) for '%s'",
                            inserted if inserted >= 0 else len(cleaned),
                            name,
                        )
                        success_count += 1
                    else:
                        db.mark_progress(name, reg_no, "no_data")
                        logger.warning("No usable data for '%s'", name)
                        # still count as processed so resume works
                        success_count += 1

                except Exception as exc:
                    fail_count += 1
                    db.mark_progress(name, reg_no, "failed", str(exc))
                    logger.error("FAILED '%s': %s", name, exc)

    finally:
        await scraper.stop()

    logger.info(
        "Scraping complete — %d succeeded, %d failed, %d skipped",
        success_count, fail_count, skipped,
    )


# ── main ──────────────────────────────────────────────────────────────────────

async def _async_main(args):
    config   = load_config(args.config)
    log_file = config.get("output", {}).get("log_file", "app.log")
    _setup_logging(log_file)

    logger = logging.getLogger("main")
    logger.info("=== DBD Financial Data Scraper starting ===")
    logger.info("Config: %s", args.config)

    db_path   = config.get("output", {}).get("database",  "financial_data.db")
    out_path  = config.get("output", {}).get("file_path",  "financial_report.xlsx")
    in_path   = config.get("input",  {}).get("file_path",  "companies.csv")
    resume    = config.get("processing", {}).get("resume_from_last", True)

    if args.no_resume:
        resume = False

    db = Database(db_path)

    if not args.export_only:
        companies = read_company_list(in_path)
        if not companies:
            logger.error("No companies found in %s — aborting.", in_path)
            sys.exit(1)

        logger.info("Loaded %d companies from %s", len(companies), in_path)
        await scrape_all(companies, config, db, resume)

    # Always export after scraping (unless --scrape-only flag added later)
    logger.info("Exporting to Excel …")
    export_to_excel(db, out_path)
    logger.info("=== Done. Output: %s ===", out_path)


def main():
    parser = argparse.ArgumentParser(description="DBD Financial Data Scraper")
    parser.add_argument("--config",      default="config.yaml",  help="Path to config.yaml")
    parser.add_argument("--no-resume",   action="store_true",    help="Reprocess all companies")
    parser.add_argument("--export-only", action="store_true",    help="Skip scraping; export DB to Excel only")
    args = parser.parse_args()

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
