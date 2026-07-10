# Eastmoney Skill

Use the `alphabee.tools.eastmoney` functions to obtain Eastmoney research report data.

## Recommended combinations

### 1. Find reports for a stock or industry
1. Call `get_eastmoney_report_list(...)` with `code` and/or `industry_code`.
2. Read `reports[*].infoCode` and `reports[*].encodeUrl`.
3. Use the next steps to fetch detail, industry context, or PDF.

### 2. Get report detail
1. If you already have `infoCode`, call `get_eastmoney_report_detail_by_info_code(...)`.
2. If you only have `encodeUrl`, call `get_eastmoney_report_detail_by_encoded_url(...)`.
3. Use the returned `detail` directly; do not parse HTML yourself.

### 3. Get industry context for a report
1. Call `get_eastmoney_report_industry_info_by_info_code(...)`.
2. Use `industry_code` / `industry_name` to link the report to its sector.

### 4. Get all reports for an industry
1. Call `get_eastmoney_industry_reports(...)` with `industry_code`.
2. Use the returned `reports` list to retrieve `infoCode` / `encodeUrl` for each report.

### 5. Download PDF
1. If you have `infoCode`, prefer `download_eastmoney_report_pdf_by_info_code(...)`.
2. If you only have `encodeUrl`, use `download_eastmoney_report_pdf(...)`.
3. Save paths are returned in the response; use them as the final artifact.

## Rules

- `infoCode` is the report ID (`AP...`).
- `encodeUrl` is the list/detail lookup key.
- `industry_code` is for industry-level report queries.
- Prefer the `*_by_info_code` functions when you already have `infoCode`.
- Keep outputs structured and reuse helper results instead of manual parsing.
