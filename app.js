"use strict";

const DATA_URL = "data/paystubs.csv";
const PLANNER_STORAGE_KEY = "paypulse-planner-v1";
const DEFAULT_ALLOCATIONS = {
  needs: 50,
  savings: 20,
  debt: 15,
  flexible: 15,
};
const NUMERIC_FIELDS = new Set([
  "year",
  "check_amount",
  "gross_pay",
  "total_taxes",
  "total_deductions",
  "calculated_net",
  "net_pay",
  "net_difference",
  "hours_units",
  "earnings_table_total",
  "paid_detail_gross",
  "regular_rate",
  "regular_hours",
  "regular_pay",
  "overtime_rate",
  "overtime_hours",
  "overtime_pay",
  "bonus_pay",
  "reported_tips",
  "employer_insurance_premium",
  "employer_401k_match",
  "employer_flsa_ot_premium",
  "social_security_tax",
  "medicare_tax",
  "federal_withholding",
  "mississippi_withholding",
  "roth_401k",
  "dental_insurance",
  "health_insurance",
]);

const TABLE_EXPORT_FIELDS = [
  ["pay_date", "Pay Date"],
  ["period_begin", "Period Begin"],
  ["period_end", "Period End"],
  ["payment_type", "Pay Type"],
  ["hours_units", "Hours / Units"],
  ["regular_rate", "Regular Rate"],
  ["regular_hours", "Regular Hours"],
  ["overtime_hours", "Overtime Hours"],
  ["gross_pay", "Gross Pay"],
  ["total_taxes", "Taxes"],
  ["total_deductions", "Deductions"],
  ["net_pay", "Net Pay"],
];

const palette = {
  navy: "#245276",
  teal: "#10a58f",
  orange: "#f28a4b",
  gold: "#e7bd5e",
  red: "#d86262",
  slate: "#7d8a9b",
  paleTeal: "rgba(16, 165, 143, 0.12)",
  paleNavy: "rgba(36, 82, 118, 0.10)",
};

const state = {
  allRows: [],
  filteredRows: [],
  sourceName: "Bundled paystubs.csv",
  pendingPdf: null,
  ingestionAvailable: null,
  sortKey: "pay_date",
  sortDirection: "desc",
  page: 1,
  pageSize: 20,
  activeView: "overview",
  calculatorDirty: false,
  calculatorNet: 0,
  plannerAvailable: false,
  plannerSaveTimer: null,
  plannerSaveChain: Promise.resolve(),
  allocationMode: "percent",
  allocations: { ...DEFAULT_ALLOCATIONS },
  expenses: [],
  goals: [],
  goalEditingId: null,
  charts: {},
};

const el = (id) => document.getElementById(id);
const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});
const compactCurrency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  notation: "compact",
  maximumFractionDigits: 1,
});
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("en-US", {
  style: "percent",
  maximumFractionDigits: 1,
});
const dateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});
const shortDateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "2-digit",
});

function parseCSV(text) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;

  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];

    if (char === '"' && quoted && next === '"') {
      field += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === "," && !quoted) {
      row.push(field);
      field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && next === "\n") index += 1;
      row.push(field);
      if (row.some((value) => value !== "")) rows.push(row);
      row = [];
      field = "";
    } else {
      field += char;
    }
  }

  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }

  const headers = rows.shift()?.map((header) => header.trim()) ?? [];
  return rows.map((values) => {
    const record = {};
    headers.forEach((header, index) => {
      const raw = values[index] ?? "";
      record[header] = NUMERIC_FIELDS.has(header)
        ? raw === ""
          ? 0
          : Number(raw)
        : raw.trim();
    });
    return record;
  });
}

function dateValue(isoDate) {
  return new Date(`${isoDate}T00:00:00`);
}

function formatDate(isoDate, short = false) {
  if (!isoDate) return "—";
  const parsed = dateValue(isoDate);
  return Number.isNaN(parsed.valueOf())
    ? isoDate
    : (short ? shortDateFormatter : dateFormatter).format(parsed);
}

function sum(rows, key) {
  return rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function inputNumber(id) {
  const value = Number(el(id).value);
  return Number.isFinite(value) ? value : 0;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function validateRows(rows) {
  const required = [
    "pay_date",
    "period_begin",
    "period_end",
    "gross_pay",
    "total_taxes",
    "total_deductions",
    "net_pay",
    "hours_units",
  ];
  if (!rows.length) throw new Error("The CSV does not contain any payroll rows.");
  const missing = required.filter((field) => !(field in rows[0]));
  if (missing.length) {
    throw new Error(`Missing required CSV columns: ${missing.join(", ")}`);
  }
}

function configureChartDefaults() {
  if (!window.Chart) {
    throw new Error("Chart.js could not be loaded.");
  }

  Chart.defaults.color = "#667487";
  Chart.defaults.font.family =
    'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
  Chart.defaults.font.size = 11;
  Chart.defaults.borderColor = "rgba(40, 64, 88, 0.10)";
  Chart.defaults.animation.duration = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? 0
    : 450;
}

function chartCurrency(value) {
  return Math.abs(value) >= 1000 ? compactCurrency.format(value) : currency.format(value);
}

function baseChartOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#173a59",
        titleColor: "#ffffff",
        bodyColor: "#e4edf3",
        borderColor: "rgba(255,255,255,.12)",
        borderWidth: 1,
        padding: 11,
        displayColors: true,
        boxPadding: 4,
      },
    },
  };
}

function destroyCharts() {
  Object.values(state.charts).forEach((chart) => chart.destroy());
  state.charts = {};
}

function median(values) {
  if (!values.length) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const midpoint = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[midpoint]
    : (ordered[midpoint - 1] + ordered[midpoint]) / 2;
}

function linearRegression(values) {
  if (values.length < 2) {
    return { slope: 0, intercept: values[0] || 0 };
  }
  const n = values.length;
  const sumX = ((n - 1) * n) / 2;
  const sumY = values.reduce((total, value) => total + value, 0);
  const sumXY = values.reduce((total, value, index) => total + index * value, 0);
  const sumXX = values.reduce((total, _, index) => total + index * index, 0);
  const denominator = n * sumXX - sumX * sumX;
  const slope = denominator ? (n * sumXY - sumX * sumY) / denominator : 0;
  return {
    slope,
    intercept: (sumY - slope * sumX) / n,
  };
}

function toIsoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function buildProjection(rows) {
  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const recent = ordered.slice(-Math.min(12, ordered.length));
  const cadenceSamples = ordered
    .slice(1)
    .map((row, index) =>
      Math.round((dateValue(row.pay_date) - dateValue(ordered[index].pay_date)) / 86400000),
    )
    .filter((days) => days > 0 && days <= 45);
  const cadenceDays = Math.round(median(cadenceSamples)) || 14;
  const horizonMonths = Number(el("projectionHorizon")?.value || 6);
  const horizonDays = Math.round(horizonMonths * 30.4375);
  const estimateCount = Math.max(1, Math.round(horizonDays / cadenceDays));
  const scenario = el("projectionScenario")?.value || "expected";
  const scenarioConfig = {
    conservative: { factor: 0.95, label: "Conservative" },
    expected: { factor: 1, label: "Expected" },
    upside: { factor: 1.05, label: "Upside" },
  }[scenario];

  const grossValues = recent.map((row) => row.gross_pay);
  const regression = linearRegression(grossValues);
  const recentGross = sum(recent, "gross_pay");
  const averageGross = recentGross / Math.max(recent.length, 1);
  const taxRate = recentGross ? sum(recent, "total_taxes") / recentGross : 0;
  const deductionRate = recentGross ? sum(recent, "total_deductions") / recentGross : 0;
  const lastDate = dateValue(ordered.at(-1).pay_date);

  const estimates = Array.from({ length: estimateCount }, (_, index) => {
    const trendGross = regression.intercept + regression.slope * (recent.length + index);
    const stabilizedGross = clamp(trendGross, averageGross * 0.78, averageGross * 1.22);
    const gross = Math.max(0, stabilizedGross * scenarioConfig.factor);
    const taxes = gross * taxRate;
    const deductions = gross * deductionRate;
    const net = Math.max(0, gross - taxes - deductions);
    const date = new Date(lastDate);
    date.setDate(date.getDate() + cadenceDays * (index + 1));
    return {
      pay_date: toIsoDate(date),
      gross_pay: gross,
      total_taxes: taxes,
      total_deductions: deductions,
      net_pay: net,
    };
  });

  return {
    ordered,
    recent,
    estimates,
    cadenceDays,
    horizonMonths,
    scenario,
    scenarioLabel: scenarioConfig.label,
    taxRate,
    deductionRate,
  };
}

function estimateCadenceDays(rows) {
  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const samples = ordered
    .slice(1)
    .map((row, index) =>
      Math.round((dateValue(row.pay_date) - dateValue(ordered[index].pay_date)) / 86400000),
    )
    .filter((days) => days > 0 && days <= 45);
  return Math.round(median(samples)) || 14;
}

function plannerPayload() {
  return {
    allocations: {
      mode: state.allocationMode,
      values: { ...state.allocations },
    },
    expenses: state.expenses.map((expense) => ({ ...expense })),
    goals: state.goals.map((goal) => ({ ...goal })),
  };
}

function updatePlannerSyncStatus(message, status = "saved") {
  const badge = el("plannerSyncStatus");
  badge.lastChild.textContent = ` ${message}`;
  badge.classList.toggle("is-offline", status === "offline");
  badge.classList.toggle("is-saving", status === "saving");
}

function applyPlannerData(planner) {
  if (!planner || typeof planner !== "object") return;
  const allocationBlock = planner.allocations || {};
  const values = allocationBlock.values || allocationBlock;
  state.allocationMode = allocationBlock.mode === "amount" ? "amount" : "percent";
  Object.keys(DEFAULT_ALLOCATIONS).forEach((key) => {
    const value = Number(values[key]);
    state.allocations[key] = Number.isFinite(value)
      ? Math.max(0, value)
      : DEFAULT_ALLOCATIONS[key];
  });
  state.expenses = Array.isArray(planner.expenses)
    ? planner.expenses.map((expense) => ({
        id: String(expense.id || ""),
        name: String(expense.name || "").slice(0, 80),
        category: String(expense.category || "Other").slice(0, 40),
        amount: Math.max(0, Number(expense.amount) || 0),
        frequency: String(expense.frequency || "monthly"),
      }))
    : [];
  state.goals = Array.isArray(planner.goals)
    ? planner.goals.map((goal) => ({
        id: String(goal.id || ""),
        name: String(goal.name || "").slice(0, 80),
        target: Math.max(0, Number(goal.target) || 0),
        saved: Math.max(0, Number(goal.saved) || 0),
        date: String(goal.date || ""),
      }))
    : [];

  el("allocationMode").value = state.allocationMode;
  Object.entries(state.allocations).forEach(([key, value]) => {
    el(`allocation${key[0].toUpperCase()}${key.slice(1)}`).value = value;
  });
  el("goalDate").min = toIsoDate(new Date());
}

function migrateLocalPlanner(stored) {
  if (!stored || typeof stored !== "object") return null;
  const migrated = {
    allocations: stored.allocations || { ...DEFAULT_ALLOCATIONS },
    expenses: Array.isArray(stored.expenses) ? stored.expenses : [],
    goals: Array.isArray(stored.goals) ? stored.goals : [],
  };
  if (
    stored.goal &&
    stored.goal.name &&
    Number(stored.goal.target) > 0 &&
    !migrated.goals.length
  ) {
    migrated.goals.push({
      id: `migrated-${Date.now()}`,
      name: String(stored.goal.name),
      target: Number(stored.goal.target),
      saved: Number(stored.goal.saved) || 0,
      date: String(stored.goal.date || ""),
    });
  }
  return migrated;
}

async function persistPlanner(immediate = false) {
  const payload = plannerPayload();
  try {
    localStorage.setItem(PLANNER_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Server persistence remains available when browser storage is disabled.
  }

  if (!state.plannerAvailable) {
    updatePlannerSyncStatus("Browser backup only — run the PayPulse server to sync", "offline");
    return false;
  }

  const save = async () => {
    updatePlannerSyncStatus("Saving to PayPulse server…", "saving");
    try {
      const response = await fetch("/api/planner", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || "Planner data could not be saved.");
      applyPlannerData(result.planner);
      updatePlannerSyncStatus("Saved on this PayPulse server");
      return true;
    } catch (error) {
      updatePlannerSyncStatus("Server save failed — browser backup retained", "offline");
      showToast(error.message);
      return false;
    }
  };

  const enqueueSave = () => {
    state.plannerSaveChain = state.plannerSaveChain.then(save, save);
    return state.plannerSaveChain;
  };
  window.clearTimeout(state.plannerSaveTimer);
  if (immediate) return enqueueSave();
  state.plannerSaveTimer = window.setTimeout(enqueueSave, 400);
  return true;
}

async function loadPlannerState() {
  let localPlanner = null;
  try {
    localPlanner = migrateLocalPlanner(
      JSON.parse(localStorage.getItem(PLANNER_STORAGE_KEY) || "null"),
    );
  } catch {
    localPlanner = null;
  }
  if (localPlanner) applyPlannerData(localPlanner);
  else applyPlannerData({ allocations: { mode: "percent", values: DEFAULT_ALLOCATIONS } });

  try {
    const response = await fetch("/api/planner", { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || "Planner storage is unavailable.");
    state.plannerAvailable = true;
    if (!result.persisted && localPlanner) {
      await persistPlanner(true);
    } else {
      applyPlannerData(result.planner);
      try {
        localStorage.setItem(PLANNER_STORAGE_KEY, JSON.stringify(result.planner));
      } catch {
        // The server remains the source of truth.
      }
      updatePlannerSyncStatus("Saved on this PayPulse server");
    }
  } catch {
    state.plannerAvailable = false;
    updatePlannerSyncStatus("Browser backup only — run the PayPulse server to sync", "offline");
  }
}

function resetCalculatorDefaults(rows) {
  if (!rows.length) return;
  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const recent = ordered.slice(-Math.min(6, ordered.length));
  const positiveRates = recent.map((row) => row.regular_rate).filter((value) => value > 0);
  const gross = sum(recent, "gross_pay");
  const average = (key) => sum(recent, key) / Math.max(recent.length, 1);

  el("calcRate").value = (positiveRates.length
    ? positiveRates.reduce((total, value) => total + value, 0) / positiveRates.length
    : gross / Math.max(sum(recent, "hours_units"), 1)
  ).toFixed(2);
  el("calcHours").value = average("regular_hours").toFixed(2);
  el("calcOvertime").value = average("overtime_hours").toFixed(2);
  el("calcBonus").value = average("bonus_pay").toFixed(2);
  el("calcTaxRate").value = (gross ? (sum(recent, "total_taxes") / gross) * 100 : 0).toFixed(1);
  el("calcDeductionRate").value = (
    gross ? (sum(recent, "total_deductions") / gross) * 100 : 0
  ).toFixed(1);
  state.calculatorDirty = false;
  renderCalculator();
}

function renderCalculator() {
  const rate = Math.max(0, inputNumber("calcRate"));
  const hours = Math.max(0, inputNumber("calcHours"));
  const overtime = Math.max(0, inputNumber("calcOvertime"));
  const bonus = Math.max(0, inputNumber("calcBonus"));
  const taxRate = clamp(inputNumber("calcTaxRate"), 0, 100) / 100;
  const deductionRate = clamp(inputNumber("calcDeductionRate"), 0, 100) / 100;
  const gross = rate * hours + rate * 1.5 * overtime + bonus;
  const taxes = gross * taxRate;
  const deductions = gross * deductionRate;
  const net = Math.max(0, gross - taxes - deductions);

  state.calculatorNet = net;
  el("calcGross").textContent = currency.format(gross);
  el("calcTaxes").textContent = currency.format(taxes);
  el("calcDeductions").textContent = currency.format(deductions);
  el("calcNet").textContent = currency.format(net);
  el("calcTakeHome").textContent = gross ? percent.format(net / gross) : "—";
  renderAllocation();
  renderExpenses(state.filteredRows.length ? state.filteredRows : state.allRows);
}

function renderAllocation() {
  const net = state.calculatorNet;
  const amountMode = state.allocationMode === "amount";
  let totalAmount = 0;
  let totalPercent = 0;
  el("allocationMode").value = state.allocationMode;

  Object.keys(DEFAULT_ALLOCATIONS).forEach((key) => {
    const inputId = `allocation${key[0].toUpperCase()}${key.slice(1)}`;
    const input = el(inputId);
    const rawValue = amountMode
      ? Math.max(0, inputNumber(inputId))
      : clamp(inputNumber(inputId), 0, 100);
    if (amountMode) input.removeAttribute("max");
    else input.max = "100";
    el(`${inputId}Unit`).textContent = amountMode ? "$" : "%";
    state.allocations[key] = rawValue;
    const amount = amountMode ? rawValue : net * (rawValue / 100);
    const share = net ? (amount / net) * 100 : 0;
    totalAmount += amount;
    totalPercent += share;
    el(`${inputId}Amount`).textContent = amountMode
      ? percent.format(share / 100)
      : currency.format(amount);
    el(`${inputId}Bar`).style.width = `${clamp(share, 0, 100)}%`;
  });

  el("allocationNet").textContent = currency.format(net);
  const status = el("allocationStatus");
  const amountDifference = net - totalAmount;
  const percentDifference = 100 - totalPercent;
  const balanced = Math.abs(amountDifference) < 0.01;
  status.classList.toggle("is-warning", !balanced);
  if (balanced) {
    status.textContent = `${currency.format(totalAmount)} fully allocated`;
  } else if (amountDifference > 0) {
    status.textContent = amountMode
      ? `${currency.format(amountDifference)} still unallocated`
      : `${number.format(percentDifference)}% still unallocated`;
  } else {
    status.textContent = amountMode
      ? `${currency.format(Math.abs(amountDifference))} over-allocated`
      : `${number.format(Math.abs(percentDifference))}% over-allocated`;
  }
}

function monthlyExpenseAmount(expense) {
  const amount = Number(expense.amount) || 0;
  return {
    weekly: (amount * 52) / 12,
    biweekly: (amount * 26) / 12,
    monthly: amount,
    annual: amount / 12,
    "one-time": 0,
  }[expense.frequency] ?? amount;
}

function frequencyLabel(frequency) {
  return {
    weekly: "Weekly",
    biweekly: "Every 2 weeks",
    monthly: "Monthly",
    annual: "Annual",
    "one-time": "One time",
  }[frequency] || frequency;
}

function renderExpenses(rows) {
  const monthlyTotal = state.expenses.reduce(
    (total, expense) => total + monthlyExpenseAmount(expense),
    0,
  );
  const cadenceDays = estimateCadenceDays(rows);
  const paychecksPerYear = 365 / cadenceDays;
  const expensePerPaycheck = paychecksPerYear ? (monthlyTotal * 12) / paychecksPerYear : 0;
  const monthlyIncome = paychecksPerYear ? (state.calculatorNet * paychecksPerYear) / 12 : 0;
  const remaining = state.calculatorNet - expensePerPaycheck;

  el("expenseMonthlyTotal").textContent = currency.format(monthlyTotal);
  el("expensePerPaycheck").textContent = currency.format(expensePerPaycheck);
  el("expenseRemaining").textContent = currency.format(remaining);
  el("expenseRemaining").classList.toggle("negative", remaining < 0);
  el("expenseRatio").textContent = monthlyIncome
    ? percent.format(monthlyTotal / monthlyIncome)
    : "—";

  el("expensesTableBody").innerHTML = state.expenses
    .map((expense) => {
      const monthly = monthlyExpenseAmount(expense);
      return `
        <tr>
          <td><strong>${escapeHtml(expense.name)}</strong></td>
          <td><span class="record-chip">${escapeHtml(expense.category)}</span></td>
          <td>${escapeHtml(frequencyLabel(expense.frequency))}</td>
          <td class="number">${currency.format(expense.amount)}</td>
          <td class="number">${expense.frequency === "one-time" ? "—" : currency.format(monthly)}</td>
          <td class="record-actions">
            <button type="button" data-delete-expense="${escapeHtml(expense.id)}" aria-label="Delete ${escapeHtml(expense.name)}">Delete</button>
          </td>
        </tr>`;
    })
    .join("");
  el("expensesEmpty").hidden = state.expenses.length > 0;
  el("expensesTableBody").closest("table").hidden = state.expenses.length === 0;
}

function goalContribution(goal, rows) {
  const remaining = Math.max(0, goal.target - goal.saved);
  if (!remaining) return 0;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const daysRemaining = Math.max(
    0,
    Math.ceil((dateValue(goal.date) - today) / 86400000),
  );
  const paychecks = Math.max(0, Math.floor(daysRemaining / estimateCadenceDays(rows)));
  return paychecks ? remaining / paychecks : remaining;
}

function renderGoals(rows) {
  const totalSaved = state.goals.reduce((total, goal) => total + goal.saved, 0);
  const totalRemaining = state.goals.reduce(
    (total, goal) => total + Math.max(0, goal.target - goal.saved),
    0,
  );
  el("goalsCount").textContent = state.goals.length;
  el("goalsSavedTotal").textContent = currency.format(totalSaved);
  el("goalsRemainingTotal").textContent = currency.format(totalRemaining);

  el("goalsTableBody").innerHTML = state.goals
    .map((goal) => {
      const progress = goal.target ? clamp(goal.saved / goal.target, 0, 1) : 0;
      return `
        <tr>
          <td><strong>${escapeHtml(goal.name)}</strong></td>
          <td class="number">
            <span class="table-progress"><i style="width:${progress * 100}%"></i></span>
            ${percent.format(progress)}
          </td>
          <td class="number">${currency.format(goal.saved)} / ${currency.format(goal.target)}</td>
          <td>${escapeHtml(formatDate(goal.date))}</td>
          <td class="number net-cell">${currency.format(goalContribution(goal, rows))}</td>
          <td class="record-actions">
            <button type="button" data-edit-goal="${escapeHtml(goal.id)}">Edit</button>
            <button type="button" data-delete-goal="${escapeHtml(goal.id)}">Delete</button>
          </td>
        </tr>`;
    })
    .join("");
  el("goalsEmpty").hidden = state.goals.length > 0;
  el("goalsTableBody").closest("table").hidden = state.goals.length === 0;
}

function clearGoalForm() {
  state.goalEditingId = null;
  el("goalForm").reset();
  el("goalSaved").value = "0";
  el("goalSubmit").textContent = "Add goal";
  el("goalCancelEdit").hidden = true;
}

function planningRows() {
  return state.filteredRows.length ? state.filteredRows : state.allRows;
}

function createRecordId(prefix) {
  const suffix =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

function changeAllocationMode() {
  const nextMode = el("allocationMode").value === "amount" ? "amount" : "percent";
  if (nextMode === state.allocationMode) return;
  const net = state.calculatorNet;
  Object.keys(DEFAULT_ALLOCATIONS).forEach((key) => {
    state.allocations[key] =
      nextMode === "amount"
        ? net * (state.allocations[key] / 100)
        : net
          ? (state.allocations[key] / net) * 100
          : 0;
    const inputId = `allocation${key[0].toUpperCase()}${key.slice(1)}`;
    el(inputId).value = state.allocations[key].toFixed(2);
  });
  state.allocationMode = nextMode;
  renderAllocation();
  persistPlanner();
}

function addExpense(event) {
  event.preventDefault();
  const name = el("expenseName").value.trim();
  const amount = Math.max(0, inputNumber("expenseAmount"));
  if (!name || !amount) {
    showToast("Enter an expense name and amount.");
    return;
  }
  state.expenses.push({
    id: createRecordId("expense"),
    name: name.slice(0, 80),
    category: el("expenseCategory").value,
    amount,
    frequency: el("expenseFrequency").value,
  });
  el("expenseForm").reset();
  el("expenseCategory").value = "Other";
  el("expenseFrequency").value = "monthly";
  renderExpenses(planningRows());
  persistPlanner(true);
  showToast(`${name} was added to recurring expenses.`);
}

function handleExpenseTableClick(event) {
  const button = event.target.closest("[data-delete-expense]");
  if (!button) return;
  const expense = state.expenses.find((item) => item.id === button.dataset.deleteExpense);
  state.expenses = state.expenses.filter((item) => item.id !== button.dataset.deleteExpense);
  renderExpenses(planningRows());
  persistPlanner(true);
  showToast(`${expense?.name || "Expense"} was removed.`);
}

function saveGoal(event) {
  event.preventDefault();
  const name = el("goalName").value.trim();
  const target = Math.max(0, inputNumber("goalTarget"));
  const saved = Math.max(0, inputNumber("goalSaved"));
  const date = el("goalDate").value;
  if (!name || !target || !date) {
    showToast("Enter a goal name, target amount, and target date.");
    return;
  }
  const goal = {
    id: state.goalEditingId || createRecordId("goal"),
    name: name.slice(0, 80),
    target,
    saved,
    date,
  };
  if (state.goalEditingId) {
    state.goals = state.goals.map((item) => (item.id === state.goalEditingId ? goal : item));
  } else {
    state.goals.push(goal);
  }
  const action = state.goalEditingId ? "updated" : "added";
  clearGoalForm();
  renderGoals(planningRows());
  persistPlanner(true);
  showToast(`${name} was ${action}.`);
}

function handleGoalTableClick(event) {
  const editButton = event.target.closest("[data-edit-goal]");
  const deleteButton = event.target.closest("[data-delete-goal]");
  if (editButton) {
    const goal = state.goals.find((item) => item.id === editButton.dataset.editGoal);
    if (!goal) return;
    state.goalEditingId = goal.id;
    el("goalName").value = goal.name;
    el("goalTarget").value = goal.target;
    el("goalSaved").value = goal.saved;
    el("goalDate").value = goal.date;
    el("goalSubmit").textContent = "Update goal";
    el("goalCancelEdit").hidden = false;
    el("goalName").focus();
  }
  if (deleteButton) {
    const goal = state.goals.find((item) => item.id === deleteButton.dataset.deleteGoal);
    state.goals = state.goals.filter((item) => item.id !== deleteButton.dataset.deleteGoal);
    if (state.goalEditingId === deleteButton.dataset.deleteGoal) clearGoalForm();
    renderGoals(planningRows());
    persistPlanner(true);
    showToast(`${goal?.name || "Goal"} was removed.`);
  }
}

function renderHealth(rows) {
  if (!rows.length) return;
  const reconciled = rows.filter(
    (row) =>
      Math.abs(
        row.gross_pay - row.total_taxes - row.total_deductions - row.net_pay,
      ) <= 0.02,
  ).length;
  const signatures = new Set();
  let duplicateCount = 0;
  rows.forEach((row) => {
    const signature = [
      row.pay_date,
      row.period_begin,
      row.period_end,
      Number(row.gross_pay).toFixed(2),
      Number(row.net_pay).toFixed(2),
    ].join("|");
    if (signatures.has(signature)) duplicateCount += 1;
    signatures.add(signature);
  });
  const complete = rows.filter(
    (row) =>
      row.pay_date &&
      row.period_begin &&
      row.period_end &&
      Number.isFinite(row.gross_pay) &&
      Number.isFinite(row.net_pay),
  ).length;
  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const cadenceDays = estimateCadenceDays(rows);
  const cadenceGaps = ordered.slice(1).filter((row, index) => {
    const gap = Math.round(
      (dateValue(row.pay_date) - dateValue(ordered[index].pay_date)) / 86400000,
    );
    return gap > cadenceDays * 1.6;
  }).length;
  const reconciliationFailures = rows.length - reconciled;
  const missingRows = rows.length - complete;
  const score = Math.round(
    clamp(
      100 -
        (reconciliationFailures / rows.length) * 40 -
        (missingRows / rows.length) * 20 -
        Math.min(25, duplicateCount * 10) -
        Math.min(15, cadenceGaps * 3),
      0,
      100,
    ),
  );

  el("healthScore").textContent = `${score}`;
  el("healthReconcile").textContent = `${reconciled} of ${rows.length} pass`;
  el("healthDuplicates").textContent = duplicateCount ? `${duplicateCount} found` : "None found";
  el("healthFields").textContent = `${complete} of ${rows.length} complete`;
  el("healthCadence").textContent = cadenceGaps ? `${cadenceGaps} unusual` : "No unusual gaps";
  [
    ["healthReconcileIcon", reconciliationFailures],
    ["healthDuplicateIcon", duplicateCount],
    ["healthFieldsIcon", missingRows],
    ["healthCadenceIcon", cadenceGaps],
  ].forEach(([id, issueCount]) => {
    el(id).textContent = issueCount ? "!" : "✓";
    el(id).classList.toggle("has-issue", Boolean(issueCount));
  });
}

function renderPlanning(rows) {
  if (!state.calculatorDirty) resetCalculatorDefaults(rows);
  else renderCalculator();
  renderExpenses(rows);
  renderGoals(rows);
  renderHealth(state.allRows);
}

function renderProjection(rows) {
  const projection = buildProjection(rows);
  const { ordered, estimates, cadenceDays, horizonMonths, scenarioLabel } = projection;
  const totalProjectedGross = sum(estimates, "gross_pay");
  const totalProjectedNet = sum(estimates, "net_pay");
  const averageProjectedNet = totalProjectedNet / Math.max(estimates.length, 1);
  const annualizedNet = averageProjectedNet * (365 / cadenceDays);
  const next = estimates[0];

  el("projectionNextNet").textContent = currency.format(next.net_pay);
  el("projectionNextDate").textContent = `Expected near ${formatDate(next.pay_date)}`;
  el("projectionGross").textContent = currency.format(totalProjectedGross);
  el("projectionGrossSub").textContent = `${scenarioLabel} · ${horizonMonths}-month horizon`;
  el("projectionNet").textContent = currency.format(totalProjectedNet);
  el("projectionNetSub").textContent =
    `${currency.format(sum(estimates, "total_taxes") + sum(estimates, "total_deductions"))} projected withheld`;
  el("projectionAnnualized").textContent = currency.format(annualizedNet);
  el("projectionCadence").textContent = `Approximately every ${cadenceDays} days`;
  el("projectionCount").innerHTML =
    `<span></span>${estimates.length} estimate${estimates.length === 1 ? "" : "s"}`;

  el("forecastRows").innerHTML = estimates
    .slice(0, 6)
    .map(
      (estimate) => `
        <article class="forecast-row">
          <div>
            <time datetime="${escapeHtml(estimate.pay_date)}">${escapeHtml(formatDate(estimate.pay_date))}</time>
            <span>${escapeHtml(scenarioLabel)} scenario</span>
          </div>
          <strong class="forecast-gross">${currency.format(estimate.gross_pay)} gross</strong>
          <strong>${currency.format(estimate.net_pay)} net</strong>
        </article>`,
    )
    .join("");

  const history = ordered.slice(-Math.min(18, ordered.length));
  const projectionLabels = [
    ...history.map((row) => formatDate(row.pay_date, true)),
    ...estimates.map((row) => formatDate(row.pay_date, true)),
  ];
  const actualData = [
    ...history.map((row) => row.net_pay),
    ...Array(estimates.length).fill(null),
  ];
  const projectedData = [
    ...Array(Math.max(0, history.length - 1)).fill(null),
    history.at(-1).net_pay,
    ...estimates.map((row) => row.net_pay),
  ];
  const projectionOptions = baseChartOptions();
  projectionOptions.scales = {
    x: {
      grid: { display: false },
      ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 11 },
    },
    y: {
      beginAtZero: false,
      ticks: { callback: (value) => chartCurrency(value) },
      title: { display: true, text: "Net pay" },
    },
  };
  projectionOptions.plugins.tooltip.callbacks = {
    label: (context) => `${context.dataset.label}: ${currency.format(context.parsed.y)}`,
  };

  state.charts.projection = new Chart(el("projectionChart"), {
    type: "line",
    data: {
      labels: projectionLabels,
      datasets: [
        {
          label: "Actual net pay",
          data: actualData,
          borderColor: palette.navy,
          backgroundColor: palette.paleNavy,
          borderWidth: 2.2,
          pointRadius: 2.5,
          pointHoverRadius: 5,
          tension: 0.2,
        },
        {
          label: `${scenarioLabel} projection`,
          data: projectedData,
          borderColor: palette.orange,
          backgroundColor: "rgba(242, 138, 75, 0.10)",
          borderWidth: 2.2,
          borderDash: [7, 5],
          pointRadius: 2.5,
          pointHoverRadius: 5,
          tension: 0.2,
          fill: true,
        },
      ],
    },
    options: projectionOptions,
  });
}

function renderAnnualChart(rows) {
  const byYear = new Map();
  rows.forEach((row) => {
    const year = String(row.year || row.pay_date.slice(0, 4));
    if (!byYear.has(year)) {
      byYear.set(year, { gross: 0, net: 0, dates: [] });
    }
    const bucket = byYear.get(year);
    bucket.gross += row.gross_pay;
    bucket.net += row.net_pay;
    bucket.dates.push(row.pay_date);
  });

  const years = [...byYear.keys()].sort();
  const latestYear = years.at(-1);
  const latestDate = byYear.get(latestYear).dates.sort().at(-1);
  const labels = years.map((year) =>
    year === latestYear && latestDate.slice(5, 7) !== "12" ? `${year} YTD` : year,
  );
  const annualOptions = baseChartOptions();
  annualOptions.plugins.legend = {
    display: true,
    position: "bottom",
    labels: {
      usePointStyle: true,
      pointStyle: "circle",
      padding: 18,
      boxWidth: 8,
      boxHeight: 8,
    },
  };
  annualOptions.scales = {
    x: { grid: { display: false } },
    y: {
      beginAtZero: true,
      position: "left",
      ticks: { callback: (value) => chartCurrency(value) },
    },
    yRate: {
      beginAtZero: true,
      max: 100,
      position: "right",
      grid: { drawOnChartArea: false },
      ticks: { callback: (value) => `${value}%` },
    },
  };
  annualOptions.plugins.tooltip.callbacks = {
    label: (context) =>
      context.dataset.yAxisID === "yRate"
        ? `${context.dataset.label}: ${context.parsed.y.toFixed(1)}%`
        : `${context.dataset.label}: ${currency.format(context.parsed.y)}`,
  };

  state.charts.annual = new Chart(el("annualChart"), {
    data: {
      labels,
      datasets: [
        {
          type: "bar",
          label: "Gross pay",
          data: years.map((year) => byYear.get(year).gross),
          backgroundColor: palette.navy,
          borderRadius: 5,
          borderSkipped: false,
        },
        {
          type: "bar",
          label: "Net pay",
          data: years.map((year) => byYear.get(year).net),
          backgroundColor: palette.teal,
          borderRadius: 5,
          borderSkipped: false,
        },
        {
          type: "line",
          label: "Take-home rate",
          data: years.map((year) => {
            const bucket = byYear.get(year);
            return bucket.gross ? (bucket.net / bucket.gross) * 100 : 0;
          }),
          yAxisID: "yRate",
          borderColor: palette.orange,
          backgroundColor: palette.orange,
          borderWidth: 2.5,
          pointRadius: 4,
          pointHoverRadius: 6,
          tension: 0.2,
        },
      ],
    },
    options: annualOptions,
  });
}

function renderCharts(rows) {
  destroyCharts();

  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const labels = ordered.map((row) => formatDate(row.pay_date, true));
  const gross = ordered.map((row) => row.gross_pay);
  const net = ordered.map((row) => row.net_pay);

  const trendOptions = baseChartOptions();
  trendOptions.scales = {
    x: {
      grid: { display: false },
      ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 9 },
    },
    y: {
      beginAtZero: false,
      ticks: { callback: (value) => chartCurrency(value) },
    },
  };
  trendOptions.plugins.tooltip.callbacks = {
    label: (context) => `${context.dataset.label}: ${currency.format(context.parsed.y)}`,
    afterBody: (contexts) => {
      const row = ordered[contexts[0].dataIndex];
      return [`Hours: ${number.format(row.hours_units)}`, `Pay period: ${row.period_begin} – ${row.period_end}`];
    },
  };

  state.charts.trend = new Chart(el("payTrendChart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Gross pay",
          data: gross,
          borderColor: palette.navy,
          backgroundColor: palette.paleNavy,
          borderWidth: 2.2,
          pointRadius: ordered.length > 35 ? 1.5 : 2.5,
          pointHoverRadius: 5,
          tension: 0.22,
          fill: false,
        },
        {
          label: "Net pay",
          data: net,
          borderColor: palette.teal,
          backgroundColor: palette.paleTeal,
          borderWidth: 2.2,
          pointRadius: ordered.length > 35 ? 1.5 : 2.5,
          pointHoverRadius: 5,
          tension: 0.22,
          fill: true,
        },
      ],
    },
    options: trendOptions,
  });

  const totalGross = sum(rows, "gross_pay");
  const totalNet = sum(rows, "net_pay");
  const totalTaxes = sum(rows, "total_taxes");
  const totalDeductions = sum(rows, "total_deductions");
  const compositionOptions = baseChartOptions();
  compositionOptions.cutout = "72%";
  compositionOptions.plugins.legend = {
    display: true,
    position: "bottom",
    labels: {
      usePointStyle: true,
      pointStyle: "circle",
      padding: 18,
      boxWidth: 8,
      boxHeight: 8,
    },
  };
  compositionOptions.plugins.tooltip.callbacks = {
    label: (context) => {
      const share = totalGross ? context.parsed / totalGross : 0;
      return ` ${context.label}: ${currency.format(context.parsed)} (${percent.format(share)})`;
    },
  };

  state.charts.composition = new Chart(el("compositionChart"), {
    type: "doughnut",
    data: {
      labels: ["Net pay", "Taxes", "Deductions"],
      datasets: [
        {
          data: [totalNet, totalTaxes, totalDeductions],
          backgroundColor: [palette.teal, palette.orange, palette.gold],
          borderColor: "#ffffff",
          borderWidth: 4,
          hoverOffset: 5,
        },
      ],
    },
    options: compositionOptions,
  });

  const components = [
    ["Social Security", sum(rows, "social_security_tax")],
    ["Federal tax", sum(rows, "federal_withholding")],
    ["State tax", sum(rows, "mississippi_withholding")],
    ["Health insurance", sum(rows, "health_insurance")],
    ["Medicare", sum(rows, "medicare_tax")],
    ["Roth 401K", sum(rows, "roth_401k")],
    ["Dental insurance", sum(rows, "dental_insurance")],
  ]
    .filter(([, value]) => value > 0)
    .sort((a, b) => b[1] - a[1]);

  const withholdingOptions = baseChartOptions();
  withholdingOptions.indexAxis = "y";
  withholdingOptions.scales = {
    x: {
      beginAtZero: true,
      ticks: { callback: (value) => chartCurrency(value) },
    },
    y: { grid: { display: false } },
  };
  withholdingOptions.plugins.tooltip.callbacks = {
    label: (context) => ` ${currency.format(context.parsed.x)}`,
  };

  state.charts.withholdings = new Chart(el("withholdingsChart"), {
    type: "bar",
    data: {
      labels: components.map(([label]) => label),
      datasets: [
        {
          data: components.map(([, value]) => value),
          backgroundColor: components.map((_, index) =>
            index < 3 ? palette.navy : index < 5 ? palette.orange : palette.gold,
          ),
          borderRadius: 5,
          borderSkipped: false,
          barThickness: components.length < 5 ? 26 : 20,
        },
      ],
    },
    options: withholdingOptions,
  });

  const scatterRows = ordered.filter((row) => row.hours_units > 0 && row.gross_pay > 0);
  const scatterOptions = baseChartOptions();
  scatterOptions.interaction = { mode: "nearest", intersect: true };
  scatterOptions.scales = {
    x: {
      beginAtZero: false,
      title: { display: true, text: "Hours / units" },
      ticks: { callback: (value) => number.format(value) },
    },
    y: {
      beginAtZero: false,
      title: { display: true, text: "Gross pay" },
      ticks: { callback: (value) => chartCurrency(value) },
    },
  };
  scatterOptions.plugins.tooltip.callbacks = {
    title: (contexts) => contexts[0]?.raw.label ?? "",
    label: (context) =>
      ` ${number.format(context.parsed.x)} hours · ${currency.format(context.parsed.y)} gross`,
  };

  state.charts.hours = new Chart(el("hoursChart"), {
    type: "scatter",
    data: {
      datasets: [
        {
          label: "Pay statement",
          data: scatterRows.map((row) => ({
            x: row.hours_units,
            y: row.gross_pay,
            label: formatDate(row.pay_date),
          })),
          backgroundColor: palette.teal,
          borderColor: "#ffffff",
          borderWidth: 1.5,
          pointRadius: 4.5,
          pointHoverRadius: 7,
        },
      ],
    },
    options: scatterOptions,
  });

  renderProjection(rows);
  renderAnnualChart(rows);
  el("donutRate").textContent = totalGross ? percent.format(totalNet / totalGross) : "—";
}

function renderKPIs(rows) {
  const totalGross = sum(rows, "gross_pay");
  const totalNet = sum(rows, "net_pay");
  const totalHours = sum(rows, "hours_units");
  const averageNet = rows.length ? totalNet / rows.length : 0;
  const grossRate = totalHours ? totalGross / totalHours : 0;

  el("kpiNet").textContent = currency.format(totalNet);
  el("kpiGross").textContent = currency.format(totalGross);
  el("kpiTakeHome").textContent = totalGross ? percent.format(totalNet / totalGross) : "—";
  el("kpiHours").textContent = number.format(totalHours);
  el("kpiAverage").textContent = currency.format(averageNet);

  el("kpiNetSub").textContent = `Across ${rows.length} selected statement${rows.length === 1 ? "" : "s"}`;
  el("kpiGrossSub").textContent = `${currency.format(sum(rows, "total_taxes"))} in taxes`;
  el("kpiTakeHomeSub").textContent = `${currency.format(sum(rows, "total_deductions"))} in deductions`;
  el("kpiHoursSub").textContent = `Average gross rate: ${currency.format(grossRate)}`;
  el("kpiAverageSub").textContent = rows.length ? `${number.format(rows.length)} pay statements` : "No statements";
}

function renderInsights(rows) {
  const insightList = el("insightList");
  if (!rows.length) {
    insightList.innerHTML = "";
    return;
  }

  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const highest = rows.reduce((best, row) => (row.gross_pay > best.gross_pay ? row : best));
  const lowest = rows.reduce((best, row) => (row.net_pay < best.net_pay ? row : best));
  const totalGross = sum(rows, "gross_pay");
  const totalNet = sum(rows, "net_pay");
  const totalHours = sum(rows, "hours_units");
  const firstThird = ordered.slice(0, Math.max(1, Math.ceil(ordered.length / 3)));
  const lastThird = ordered.slice(-Math.max(1, Math.ceil(ordered.length / 3)));
  const earlyAvg = sum(firstThird, "gross_pay") / firstThird.length;
  const recentAvg = sum(lastThird, "gross_pay") / lastThird.length;
  const change = earlyAvg ? recentAvg / earlyAvg - 1 : 0;
  const taxRate = totalGross ? sum(rows, "total_taxes") / totalGross : 0;

  const insights = [
    {
      title: "Highest gross paycheck",
      detail: `${currency.format(highest.gross_pay)} on ${formatDate(highest.pay_date)} with ${number.format(highest.hours_units)} hours.`,
    },
    {
      title: "Gross pay trend",
      detail: `The latest-period average is ${percent.format(Math.abs(change))} ${change >= 0 ? "higher" : "lower"} than the earliest-period average.`,
    },
    {
      title: "Effective tax rate",
      detail: `${percent.format(taxRate)} of gross pay was withheld for taxes in the current view.`,
    },
    {
      title: "Hourly earning power",
      detail: `${currency.format(totalHours ? totalGross / totalHours : 0)} gross per recorded hour, with a ${percent.format(totalGross ? totalNet / totalGross : 0)} take-home rate.`,
    },
    {
      title: "Smallest net paycheck",
      detail: `${currency.format(lowest.net_pay)} on ${formatDate(lowest.pay_date)}; use the trend chart to compare its components.`,
    },
  ];

  insightList.innerHTML = insights
    .map(
      (insight, index) => `
        <article class="insight">
          <span class="insight-index">${String(index + 1).padStart(2, "0")}</span>
          <div>
            <strong>${escapeHtml(insight.title)}</strong>
            <span>${escapeHtml(insight.detail)}</span>
          </div>
        </article>`,
    )
    .join("");
}

function sortedRows(rows) {
  const direction = state.sortDirection === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const left = a[state.sortKey];
    const right = b[state.sortKey];
    if (typeof left === "number" || typeof right === "number") {
      return ((Number(left) || 0) - (Number(right) || 0)) * direction;
    }
    return String(left).localeCompare(String(right)) * direction;
  });
}

function renderTable(rows) {
  const ordered = sortedRows(rows);
  const pageCount = Math.max(1, Math.ceil(ordered.length / state.pageSize));
  state.page = clamp(state.page, 1, pageCount);
  const start = (state.page - 1) * state.pageSize;
  const pageRows = ordered.slice(start, start + state.pageSize);

  el("payTableBody").innerHTML = pageRows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(formatDate(row.pay_date))}</td>
          <td>${escapeHtml(formatDate(row.period_begin, true))} – ${escapeHtml(formatDate(row.period_end, true))}</td>
          <td><span class="pay-type">${escapeHtml(row.payment_type || row.pay_type || "Payroll")}</span></td>
          <td class="number">${number.format(row.hours_units)}</td>
          <td class="number">${currency.format(row.regular_rate)}</td>
          <td class="number">${currency.format(row.gross_pay)}</td>
          <td class="number">${currency.format(row.total_taxes)}</td>
          <td class="number">${currency.format(row.total_deductions)}</td>
          <td class="number net-cell">${currency.format(row.net_pay)}</td>
        </tr>`,
    )
    .join("");

  const shownStart = rows.length ? start + 1 : 0;
  const shownEnd = Math.min(start + pageRows.length, rows.length);
  el("tableCount").textContent = `Showing ${shownStart}–${shownEnd} of ${rows.length} statements`;
  el("pageLabel").textContent = `Page ${state.page} of ${pageCount}`;
  el("prevPage").disabled = state.page <= 1;
  el("nextPage").disabled = state.page >= pageCount;
}

function updateHeader(rows) {
  const ordered = [...rows].sort((a, b) => a.pay_date.localeCompare(b.pay_date));
  const first = ordered[0];
  const last = ordered.at(-1);
  const coverage = rows.length
    ? `${formatDate(first.pay_date)} through ${formatDate(last.pay_date)}`
    : "No matching date range";

  el("coverageText").textContent = `${coverage}. Filter any field to recalculate every metric, chart, insight, and table row.`;
  el("recordCount").textContent = `${rows.length} statement${rows.length === 1 ? "" : "s"}`;
  el("dataStatus").textContent = state.sourceName;
}

function renderActiveView() {
  const hasData = state.filteredRows.length > 0;
  const planningActive = state.activeView === "planning";

  document.querySelector(".filter-bar").hidden = planningActive;
  el("kpiGrid").hidden = planningActive || !hasData;
  document.querySelector(".chart-grid").hidden = planningActive || !hasData;
  el("healthPanel").hidden = planningActive || !hasData;
  el("history").hidden = planningActive || !hasData;
  el("planning").hidden = !planningActive || !hasData;
  el("emptyState").hidden = planningActive || hasData;

  el("overviewTab").classList.toggle("active", !planningActive);
  el("overviewTab").toggleAttribute("aria-current", !planningActive);
  el("planningTab").classList.toggle("active", planningActive);
  el("planningTab").toggleAttribute("aria-current", planningActive);
}

function activateView(view, scrollTarget = null, updateUrl = true) {
  state.activeView = view === "planning" ? "planning" : "overview";
  renderActiveView();

  const hash = scrollTarget ? `#${scrollTarget}` : state.activeView === "planning" ? "#planning" : "#overview";
  if (updateUrl && window.location.hash !== hash) {
    window.history.pushState(null, "", hash);
  }

  window.requestAnimationFrame(() => {
    const target = scrollTarget ? el(scrollTarget) : state.activeView === "planning" ? el("planning") : el("overview");
    target?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function activateViewFromLocation() {
  const target = window.location.hash.slice(1);
  if (target === "planning") {
    activateView("planning", null, false);
  } else if (target === "forecast" || target === "history") {
    activateView("overview", target, false);
  } else {
    activateView("overview", null, false);
  }
}

function applyFilters() {
  const year = el("yearFilter").value;
  const from = el("dateFrom").value;
  const to = el("dateTo").value;
  const query = el("searchInput").value.trim().toLowerCase();

  state.filteredRows = state.allRows.filter((row) => {
    const yearMatches = year === "all" || String(row.year || row.pay_date.slice(0, 4)) === year;
    const fromMatches = !from || row.pay_date >= from;
    const toMatches = !to || row.pay_date <= to;
    const haystack = [
      row.pay_date,
      row.period_begin,
      row.period_end,
      row.payment_type,
      row.pay_type,
      row.year,
    ]
      .join(" ")
      .toLowerCase();
    const searchMatches = !query || haystack.includes(query);
    return yearMatches && fromMatches && toMatches && searchMatches;
  });

  state.page = 1;
  updateDashboard();
}

function updateDashboard() {
  const rows = state.filteredRows;
  updateHeader(rows);
  renderKPIs(rows);
  renderTable(rows);
  renderInsights(rows);

  const hasData = rows.length > 0;
  if (hasData) {
    renderCharts(rows);
    renderPlanning(rows);
  } else {
    destroyCharts();
  }
  renderActiveView();
}

function populateFilterOptions() {
  const years = [
    ...new Set(state.allRows.map((row) => String(row.year || row.pay_date.slice(0, 4)))),
  ].sort();
  el("yearFilter").innerHTML = [
    '<option value="all">All years</option>',
    ...years.map((year) => `<option value="${escapeHtml(year)}">${escapeHtml(year)}</option>`),
  ].join("");

  const dates = state.allRows.map((row) => row.pay_date).filter(Boolean).sort();
  if (dates.length) {
    el("dateFrom").min = dates[0];
    el("dateFrom").max = dates.at(-1);
    el("dateTo").min = dates[0];
    el("dateTo").max = dates.at(-1);
  }
}

function resetFilters() {
  el("yearFilter").value = "all";
  el("dateFrom").value = "";
  el("dateTo").value = "";
  el("searchInput").value = "";
  applyFilters();
}

function showToast(message) {
  const toast = el("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 3200);
}

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setPendingPdf(file) {
  if (!file) {
    state.pendingPdf = null;
    el("pdfUpload").value = "";
    el("selectedPdf").hidden = true;
    el("processPdf").disabled = true;
    return;
  }
  if (file.type && file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    renderIngestError("Only PDF files are supported.");
    return;
  }
  if (file.size > 15 * 1024 * 1024) {
    renderIngestError("The PDF exceeds the 15 MB upload limit.");
    return;
  }
  state.pendingPdf = file;
  el("selectedPdfName").textContent = file.name;
  el("selectedPdfSize").textContent = formatFileSize(file.size);
  el("selectedPdf").hidden = false;
  el("processPdf").disabled = false;
  el("ingestResult").hidden = true;
}

function openIngestModal() {
  el("ingestModal").hidden = false;
  document.body.classList.add("modal-open");
  window.setTimeout(() => el("closeIngest").focus(), 0);
  if (state.ingestionAvailable === false) {
    renderIngestError(
      "PDF ingestion needs the PayPulse server. Start the site with “python server.py” instead of a basic static server.",
    );
  }
}

function closeIngestModal() {
  el("ingestModal").hidden = true;
  document.body.classList.remove("modal-open");
  el("ingestButton").focus();
}

function renderIngestError(message) {
  const result = el("ingestResult");
  result.className = "ingest-result is-error";
  result.innerHTML = `
    <h3>Could not ingest this paystub</h3>
    <p>${escapeHtml(message)}</p>
  `;
  result.hidden = false;
}

function renderIngestResult(payload) {
  const result = el("ingestResult");
  const added = Number(payload.added || 0);
  const duplicates = Number(payload.duplicates || 0);
  const duplicateOnly = added === 0 && duplicates > 0;
  result.className = `ingest-result${duplicateOnly ? " is-duplicate" : ""}`;
  const heading = duplicateOnly
    ? "This statement is already in pay history"
    : `${added} statement${added === 1 ? "" : "s"} added`;
  const description = duplicateOnly
    ? "The PDF reconciled successfully, but its pay date, period, gross pay, and net pay match an existing row. The CSV was not changed."
    : `The pay-history CSV now contains ${payload.total_records} statement${payload.total_records === 1 ? "" : "s"}. Every added row passed the reconciliation checks.`;
  const rows = (payload.rows || [])
    .map(
      (row) => `
        <div class="ingested-row">
          <div>
            <strong>${escapeHtml(formatDate(row.pay_date))}</strong>
            <span>${currency.format(row.gross_pay)} gross · ${number.format(row.hours_units)} hours · ${escapeHtml(row.status)}</span>
          </div>
          <b>${currency.format(row.net_pay)} net</b>
        </div>`,
    )
    .join("");
  result.innerHTML = `
    <h3>${escapeHtml(heading)}</h3>
    <p>${escapeHtml(description)}</p>
    <div class="ingested-rows">${rows}</div>
  `;
  result.hidden = false;
}

async function processPendingPdf() {
  if (!state.pendingPdf) return;
  const button = el("processPdf");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Analyzing…";

  try {
    const form = new FormData();
    form.append("file", state.pendingPdf, state.pendingPdf.name);
    const response = await fetch("/api/ingest", { method: "POST", body: form });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : { message: "The ingestion API is unavailable. Start the site with “python server.py”." };
    if (!response.ok) throw new Error(payload.message || "The paystub could not be ingested.");

    renderIngestResult(payload);
    if (payload.added > 0) {
      await loadBundledCSV();
      showToast(`${payload.added} new pay statement${payload.added === 1 ? "" : "s"} added.`);
    } else {
      showToast("Duplicate detected; the CSV was not changed.");
    }
    setPendingPdf(null);
  } catch (error) {
    renderIngestError(error.message);
  } finally {
    button.textContent = originalLabel;
    button.disabled = !state.pendingPdf;
  }
}

async function checkIngestionAvailability() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    state.ingestionAvailable = response.ok && (await response.json()).ingestion === true;
  } catch {
    state.ingestionAvailable = false;
  }
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function exportFilteredRows() {
  if (!state.filteredRows.length) {
    showToast("There are no rows to export.");
    return;
  }

  const header = TABLE_EXPORT_FIELDS.map(([, label]) => csvEscape(label)).join(",");
  const lines = sortedRows(state.filteredRows).map((row) =>
    TABLE_EXPORT_FIELDS.map(([field]) => csvEscape(row[field])).join(","),
  );
  const blob = new Blob([[header, ...lines].join("\r\n")], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `payroll-view-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showToast(`Exported ${state.filteredRows.length} standard payroll rows.`);
}

async function setData(text, sourceName) {
  const rows = parseCSV(text);
  validateRows(rows);
  state.allRows = rows;
  state.filteredRows = rows;
  state.sourceName = sourceName;
  state.page = 1;
  populateFilterOptions();
  resetFilters();
  showToast(`Loaded ${rows.length} payroll statements from ${sourceName}.`);
}

async function loadBundledCSV() {
  try {
    const response = await fetch(DATA_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`CSV request failed with status ${response.status}.`);
    await setData(await response.text(), "Bundled paystubs.csv");
  } catch (error) {
    el("coverageText").textContent =
      "The bundled CSV could not be opened. Run this folder through a local web server or use Load CSV.";
    el("dataStatus").textContent = "CSV needed";
    el("emptyState").hidden = false;
    el("emptyState").querySelector("strong").textContent = "Load the payroll CSV to begin.";
    el("emptyState").querySelector("p").textContent =
      "Browsers block local file access when index.html is opened directly.";
    document.querySelector(".chart-grid").hidden = true;
    el("kpiGrid").hidden = true;
    showToast(error.message);
  }
}

function bindEvents() {
  document.querySelectorAll(".section-nav a[data-view]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      activateView(link.dataset.view, link.dataset.scrollTarget || null);
    });
  });
  window.addEventListener("popstate", activateViewFromLocation);
  ["yearFilter", "dateFrom", "dateTo"].forEach((id) =>
    el(id).addEventListener("change", applyFilters),
  );
  ["projectionScenario", "projectionHorizon"].forEach((id) =>
    el(id).addEventListener("change", () => {
      if (state.filteredRows.length) renderCharts(state.filteredRows);
    }),
  );
  el("searchInput").addEventListener("input", applyFilters);
  el("resetFilters").addEventListener("click", resetFilters);
  [
    "calcRate",
    "calcHours",
    "calcOvertime",
    "calcBonus",
    "calcTaxRate",
    "calcDeductionRate",
  ].forEach((id) =>
    el(id).addEventListener("input", () => {
      state.calculatorDirty = true;
      renderCalculator();
    }),
  );
  el("resetCalculator").addEventListener("click", () => {
    resetCalculatorDefaults(state.filteredRows.length ? state.filteredRows : state.allRows);
    showToast("Calculator reset to recent payroll averages.");
  });
  el("allocationMode").addEventListener("change", changeAllocationMode);
  document.querySelectorAll("[data-allocation]").forEach((input) =>
    input.addEventListener("input", () => {
      renderAllocation();
      persistPlanner();
    }),
  );
  el("expenseForm").addEventListener("submit", addExpense);
  el("expensesTableBody").addEventListener("click", handleExpenseTableClick);
  el("goalForm").addEventListener("submit", saveGoal);
  el("goalCancelEdit").addEventListener("click", clearGoalForm);
  el("goalsTableBody").addEventListener("click", handleGoalTableClick);
  el("exportButton").addEventListener("click", exportFilteredRows);
  el("ingestButton").addEventListener("click", openIngestModal);
  el("closeIngest").addEventListener("click", closeIngestModal);
  document.querySelector("[data-close-ingest]").addEventListener("click", closeIngestModal);
  el("clearPdf").addEventListener("click", () => setPendingPdf(null));
  el("processPdf").addEventListener("click", processPendingPdf);
  el("pdfUpload").addEventListener("change", (event) => {
    setPendingPdf(event.target.files?.[0] || null);
  });
  const dropzone = el("pdfDropzone");
  ["dragenter", "dragover"].forEach((eventName) =>
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.add("dragging");
    }),
  );
  ["dragleave", "drop"].forEach((eventName) =>
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropzone.classList.remove("dragging");
    }),
  );
  dropzone.addEventListener("drop", (event) => {
    setPendingPdf(event.dataTransfer?.files?.[0] || null);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("ingestModal").hidden) closeIngestModal();
  });
  el("pageSize").addEventListener("change", (event) => {
    state.pageSize = Number(event.target.value);
    state.page = 1;
    renderTable(state.filteredRows);
  });
  el("prevPage").addEventListener("click", () => {
    state.page -= 1;
    renderTable(state.filteredRows);
  });
  el("nextPage").addEventListener("click", () => {
    state.page += 1;
    renderTable(state.filteredRows);
  });

  document.querySelectorAll("th button[data-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextKey = button.dataset.sort;
      if (state.sortKey === nextKey) {
        state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
      } else {
        state.sortKey = nextKey;
        state.sortDirection = nextKey === "pay_date" ? "desc" : "asc";
      }
      renderTable(state.filteredRows);
    });
  });

  el("csvUpload").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      await setData(await file.text(), file.name);
    } catch (error) {
      showToast(error.message);
    } finally {
      event.target.value = "";
    }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  try {
    configureChartDefaults();
    bindEvents();
    await loadPlannerState();
    await checkIngestionAvailability();
    await loadBundledCSV();
    activateViewFromLocation();
  } catch (error) {
    console.error(error);
    showToast(error.message);
  }
});
