# PayPulse payroll dashboard

A self-hosted payroll dashboard that reads `data/paystubs.csv` in the browser. It uses a locally bundled copy of Chart.js 4.5.1 and does not send payroll or planning data to an external service. The bundled CSV is a sanitized copy containing payroll measures only.

## Run locally

From the `pay-dashboard` folder:

```powershell
python -m pip install -r requirements.txt
python server.py
```

Then open `http://localhost:8000`.

The PayPulse server hosts the dashboard, provides the local PDF-ingestion endpoint, and stores Tools data in `data/planner.json`. A basic static server can still display the dashboard and use a browser-storage fallback, but it cannot append statements or provide durable server-side planner storage.

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
- Dedicated Tools tab with a paycheck what-if calculator seeded from recent averages
- Take-home allocation by percentages or exact dollar amounts
- Persistent expense calculator with weekly, biweekly, monthly, annual, and one-time costs
- Multiple persistent savings goals with progress, editing, and per-paycheck runway guidance
- Payroll health audit for reconciliation, duplicates, required fields, and cadence gaps
- Filterable, sortable, paginated pay table
- Export of the filtered standard payroll fields
- PDF paystub ingestion with reconciliation, duplicate protection, backups, and live refresh

Payment IDs, employee IDs, company IDs, and source-document fields are never shown or included in exports.

## Planner persistence

Allocations, expenses, and savings goals are validated and written atomically to
`data/planner.json` by the local PayPulse server. Browser local storage is retained only as an
offline backup and first-run migration source. Planner records are never added to the payroll CSV
or sent to an external service.

Use a different planner file when starting the server if needed:

```powershell
python server.py --planner "C:\path\to\planner.json"
```

## Projection methodology

The projection lab estimates pay cadence from historical pay dates and models gross pay from up to the 12 most recent statements. Trend estimates are capped around the recent average to reduce outlier effects. Recent tax and deduction ratios are then applied to estimated gross pay. Conservative and upside scenarios apply a 5% adjustment to the stabilized expected case.

Projections are planning estimates only. They do not account for future schedule changes, raises, bonuses, benefit elections, or withholding changes.
