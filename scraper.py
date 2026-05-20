"""
Playwright-based async scraper for https://www.dbd.go.th financial data.

Navigation flow
───────────────
1. Open the company-search page (chkdata_niti).
2. Enter the registration number (preferred) or company name.
3. Click the matching result row.
4. On the company detail page, click the "งบการเงิน" (financial statements) tab.
5. Iterate over all available year rows and harvest the 6 financial fields.
"""

import asyncio
import logging
import re
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

# ── selectors ────────────────────────────────────────────────────────────────

# Search page
SEL_SEARCH_INPUT    = "input#txtSearch, input[name='search'], input[type='text']"
SEL_SEARCH_BTN      = "button[type='submit'], input[type='submit'], #btnSearch"

# Result table – first <a> in each data row
SEL_RESULT_ROWS     = "table.table-search tr:not(:first-child), table#resultTable tr:not(:first-child)"

# Company detail
SEL_FIN_TAB         = "a:has-text('งบการเงิน'), a:has-text('Financial'), #tab-financial"
SEL_FIN_TABLE       = "table#tblFinancial, table.financial-table, table"

# Column header keywords (Thai + English fallbacks)
_COL_REVENUE        = re.compile(r"รายได้รวม|total.?revenue|revenue", re.I)
_COL_NET_PROFIT     = re.compile(r"กำไรสุทธิ|net.?profit|profit", re.I)
_COL_ASSETS         = re.compile(r"สินทรัพย์รวม|total.?assets|assets", re.I)
_COL_LIABILITIES    = re.compile(r"หนี้สินรวม|total.?liab", re.I)
_COL_EQUITY         = re.compile(r"ส่วนของผู้ถือหุ้น|equity|stockholder", re.I)
_COL_YEAR           = re.compile(r"ปีบัญชี|ปี|year", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────

async def _safe_text(element) -> str:
    try:
        return (await element.inner_text()).strip()
    except Exception:
        return ""


async def _try_click(page: Page, selector: str, timeout: int = 5_000) -> bool:
    try:
        await page.click(selector, timeout=timeout)
        return True
    except Exception:
        return False


# ── main class ───────────────────────────────────────────────────────────────

class DBDScraper:
    def __init__(self, config: dict):
        cfg = config.get("scraper", {})
        self.headless       = cfg.get("headless", True)
        self.timeout        = cfg.get("timeout", 30_000)
        self.retry_attempts = cfg.get("retry_attempts", 3)
        self.retry_delay    = cfg.get("retry_delay", 2)
        self.search_url     = cfg.get(
            "search_url",
            "https://www.dbd.go.th/main.php?filename=chkdata_niti",
        )

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            locale="th-TH",
            timezone_id="Asia/Bangkok",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
        )
        logger.info("Browser started (headless=%s)", self.headless)

    async def stop(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    # ── public API ────────────────────────────────────────────────────────────

    async def scrape_company(
        self,
        company_name: str,
        registration_number: Optional[str] = None,
    ) -> list[dict]:
        """
        Scrape all available financial years for one company.

        Returns a list of raw dicts (unprocessed strings) with keys:
            company_name, year, revenue, net_profit, assets, liabilities, equity
        """
        for attempt in range(1, self.retry_attempts + 1):
            try:
                results = await self._scrape_once(company_name, registration_number)
                logger.info(
                    "✓ %s — %d year(s) extracted",
                    company_name,
                    len(results),
                )
                return results
            except PWTimeout as exc:
                logger.warning(
                    "Timeout on %s (attempt %d/%d): %s",
                    company_name, attempt, self.retry_attempts, exc,
                )
            except Exception as exc:
                logger.warning(
                    "Error on %s (attempt %d/%d): %s",
                    company_name, attempt, self.retry_attempts, exc,
                )

            if attempt < self.retry_attempts:
                delay = self.retry_delay * (2 ** (attempt - 1))
                logger.debug("Retrying in %.0fs …", delay)
                await asyncio.sleep(delay)

        logger.error("✗ %s — all %d attempts failed", company_name, self.retry_attempts)
        return []

    # ── internal ─────────────────────────────────────────────────────────────

    async def _scrape_once(
        self,
        company_name: str,
        registration_number: Optional[str],
    ) -> list[dict]:
        page = await self._context.new_page()
        try:
            await self._navigate_to_search(page)
            found = await self._search_and_select(page, company_name, registration_number)
            if not found:
                logger.warning("Company not found in search results: %s", company_name)
                return []
            return await self._extract_financials(page, company_name)
        finally:
            await page.close()

    async def _navigate_to_search(self, page: Page):
        await page.goto(self.search_url, wait_until="domcontentloaded", timeout=self.timeout)
        await page.wait_for_load_state("networkidle", timeout=self.timeout)

    async def _search_and_select(
        self,
        page: Page,
        company_name: str,
        registration_number: Optional[str],
    ) -> bool:
        query = registration_number if registration_number else company_name

        # Fill search box
        try:
            await page.wait_for_selector(SEL_SEARCH_INPUT, timeout=10_000)
            await page.fill(SEL_SEARCH_INPUT, query)
        except Exception:
            # Fallback: find first visible text input
            inputs = await page.query_selector_all("input[type='text']")
            if not inputs:
                raise RuntimeError("No text input found on search page")
            await inputs[0].fill(query)

        # Submit
        submitted = await _try_click(page, SEL_SEARCH_BTN, timeout=5_000)
        if not submitted:
            await page.keyboard.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=self.timeout)

        # Select from results
        return await self._pick_result(page, company_name, registration_number)

    async def _pick_result(
        self,
        page: Page,
        company_name: str,
        registration_number: Optional[str],
    ) -> bool:
        # Wait for any result-like element
        try:
            await page.wait_for_selector("table tr, .result-item, .list-group-item", timeout=10_000)
        except PWTimeout:
            return False

        # Collect all <a> tags visible on the page
        links = await page.query_selector_all("a")
        for link in links:
            text = await _safe_text(link)
            if not text:
                continue
            match_name = company_name.lower() in text.lower()
            match_reg  = registration_number and registration_number in text
            if match_name or match_reg:
                await link.click()
                await page.wait_for_load_state("networkidle", timeout=self.timeout)
                return True

        # Fallback: click first result row link
        rows = await page.query_selector_all(SEL_RESULT_ROWS)
        if rows:
            link = await rows[0].query_selector("a")
            if link:
                await link.click()
                await page.wait_for_load_state("networkidle", timeout=self.timeout)
                return True

        return False

    async def _extract_financials(self, page: Page, company_name: str) -> list[dict]:
        # Try clicking the financial statements tab
        clicked = await _try_click(page, SEL_FIN_TAB, timeout=5_000)
        if clicked:
            await page.wait_for_load_state("networkidle", timeout=self.timeout)

        # Also handle pages where financial data is on a sub-URL
        fin_link = await page.query_selector("a[href*='financial'], a[href*='งบ']")
        if fin_link and not clicked:
            await fin_link.click()
            await page.wait_for_load_state("networkidle", timeout=self.timeout)

        records = []

        # Scan every <table> on the page and try to parse financial data
        tables = await page.query_selector_all("table")
        for table in tables:
            parsed = await self._parse_table(table, company_name)
            records.extend(parsed)

        # Deduplicate by year (keep last)
        seen: dict[int, dict] = {}
        for r in records:
            try:
                y = int(str(r.get("year", 0)))
                seen[y] = r
            except ValueError:
                pass

        return list(seen.values())

    async def _parse_table(self, table, company_name: str) -> list[dict]:
        """
        Generic table parser.

        Strategy:
        - Read header row to identify column indices.
        - Each subsequent row is one fiscal year.
        - Alternatively: rows are financial items, columns are years.
        """
        rows = await table.query_selector_all("tr")
        if len(rows) < 2:
            return []

        # Read all cell texts
        grid: list[list[str]] = []
        for row in rows:
            cells = await row.query_selector_all("td, th")
            grid.append([await _safe_text(c) for c in cells])

        # Skip tables that are clearly navigation / layout (< 3 columns)
        if not grid or max(len(r) for r in grid) < 3:
            return []

        header = grid[0]

        # ── Layout A: rows = years, columns = financial fields ──────────────
        col_year   = _find_col(header, _COL_YEAR)
        col_rev    = _find_col(header, _COL_REVENUE)
        col_profit = _find_col(header, _COL_NET_PROFIT)
        col_assets = _find_col(header, _COL_ASSETS)
        col_liab   = _find_col(header, _COL_LIABILITIES)
        col_eq     = _find_col(header, _COL_EQUITY)

        if col_year is not None and any(
            c is not None for c in [col_rev, col_profit, col_assets]
        ):
            records = []
            for row in grid[1:]:
                if len(row) <= col_year:
                    continue
                records.append({
                    "company_name": company_name,
                    "year":         _cell(row, col_year),
                    "revenue":      _cell(row, col_rev),
                    "net_profit":   _cell(row, col_profit),
                    "assets":       _cell(row, col_assets),
                    "liabilities":  _cell(row, col_liab),
                    "equity":       _cell(row, col_eq),
                })
            return records

        # ── Layout B: rows = financial items, columns = years ───────────────
        # Detect year row (first column contains year-like values)
        year_row_idx = None
        for i, row in enumerate(grid):
            if row and re.search(r"\b(25\d{2}|20\d{2}|19\d{2})\b", row[0]):
                year_row_idx = i
                break

        if year_row_idx is None:
            return []

        year_row = grid[year_row_idx]
        # Years are in columns 1..n
        year_cols = list(range(1, len(year_row)))

        # Map each subsequent row label to a field
        field_map: dict[str, str] = {}
        for row in grid[year_row_idx + 1:]:
            if not row:
                continue
            label = row[0]
            if _COL_REVENUE.search(label):
                field_map["revenue"] = label
            elif _COL_NET_PROFIT.search(label):
                field_map["net_profit"] = label
            elif _COL_ASSETS.search(label):
                field_map["assets"] = label
            elif _COL_LIABILITIES.search(label):
                field_map["liabilities"] = label
            elif _COL_EQUITY.search(label):
                field_map["equity"] = label

        if not field_map:
            return []

        # Build per-year records
        records_b: dict[int, dict] = {
            c: {"company_name": company_name, "year": year_row[c]}
            for c in year_cols
        }

        for row in grid[year_row_idx + 1:]:
            label = row[0] if row else ""
            for field, pattern_label in field_map.items():
                if label == pattern_label:
                    for c in year_cols:
                        records_b[c][field] = _cell(row, c)

        return list(records_b.values())


# ── utilities ─────────────────────────────────────────────────────────────────

def _find_col(headers: list[str], pattern: re.Pattern) -> Optional[int]:
    for i, h in enumerate(headers):
        if pattern.search(h):
            return i
    return None


def _cell(row: list[str], idx: Optional[int]) -> Optional[str]:
    if idx is None or idx >= len(row):
        return None
    return row[idx] or None
