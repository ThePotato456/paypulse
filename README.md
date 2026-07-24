# PayPulse payroll dashboard

A static, self-hosted payroll dashboard that reads `data/paystubs.csv` in the browser. It uses a locally bundled copy of Chart.js 4.5.1 and does not send payroll data to an external service. The bundled CSV is a sanitized copy containing payroll measures only.

## Run locally

From the `pay-dashboard` folder:

```powershell
python -m pip install -r requirements.txt
python server.py
```

Then open `http://localhost:8000`.

The PayPulse server hosts the static dashboard and provides the local PDF-ingestion endpoint. A basic static server can still display the dashboard, but it cannot append uploaded statements to the CSV.

## Ingest a paystub

1. Start the dashboard with `python server.py`.
2. Select **Ingest paystub** in the header.
3. Choose or drop a supported paystub PDF.
4. Select **Analyze and add**.

The server:

- extracts every supported statement page in the PDF;
- stores no employee, company, payment, bank-account, address, or source-document identifiers;
- verifies gross pay against paid earnings;
- verifies tax and deduction component totals;
- verifies `gross - taxes - deductions = net`;
- detects duplicates using pay date, pay period, gross pay, and net pay;
- creates a timestamped CSV backup before a successful append;
- writes the CSV atomically and deletes the temporary PDF.

After a successful append, every metric, chart, projection, insight, and table row refreshes automatically.

The same workflow is available from the command line:

```powershell
# Validate without changing the CSV
python ingestion.py "C:\path\to\paystub.pdf"

# Append new, reconciled, nonduplicate statements
python ingestion.py "C:\path\to\paystub.pdf" --append
```

The **Load CSV** button remains available for temporary browser analysis of another compatible CSV. It does not modify the main pay-history file.

## Included analysis

- Gross and net pay trend
- Net pay, taxes, and deductions composition
- Tax and deduction category totals
- Hours worked versus gross pay
- Adjustable 3-, 6-, and 12-month paycheck projections
- Conservative, expected, and upside forecast scenarios
- Estimated upcoming paycheck schedule and annualized net pace
- Annual gross, net, and take-home-rate comparison
- Live payroll insights and summary metrics
- Filterable, sortable, paginated pay table
- Export of the filtered standard payroll fields
- PDF paystub ingestion with reconciliation, duplicate protection, backups, and live refresh

Payment IDs, employee IDs, company IDs, and source-document fields are never shown or included in exports.

## Projection methodology

The projection lab estimates pay cadence from historical pay dates and models gross pay from up to the 12 most recent statements. Trend estimates are capped around the recent average to reduce outlier effects. Recent tax and deduction ratios are then applied to estimated gross pay. Conservative and upside scenarios apply a 5% adjustment to the stabilized expected case.

Projections are planning estimates only. They do not account for future schedule changes, raises, bonuses, benefit elections, or withholding changes.
