"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  calculatorInputsForPayStatement,
  expenseAmountFromMonthly,
  expensePlan,
  moveExpenseByOffset,
  monthlyIncomeFactor,
  monthlyExpenseAmount,
  monthlyPayPeriods,
  normalizeExpenseAmount,
  recurringManualIncome,
  reorderExpenses,
} = require("../app.js");

test("recurring frequencies use calendar-month increments", () => {
  assert.equal(monthlyExpenseAmount({ amount: 40, frequency: "weekly" }), 160);
  assert.equal(monthlyExpenseAmount({ amount: 300, frequency: "biweekly" }), 600);
  assert.equal(monthlyExpenseAmount({ amount: 600, frequency: "monthly" }), 600);
  assert.equal(monthlyExpenseAmount({ amount: 1200, frequency: "annual" }), 100);
  assert.equal(monthlyExpenseAmount({ amount: 100, frequency: "one-time" }), 0);
});

test("pay cadence resolves to calendar-month paycheck increments", () => {
  assert.equal(monthlyPayPeriods(7), 4);
  assert.equal(monthlyPayPeriods(14), 2);
  assert.equal(monthlyPayPeriods(15), 2);
  assert.equal(monthlyPayPeriods(30), 1);
});

test("per-paycheck reserve exactly funds a calendar month", () => {
  const expenses = [
    { amount: 600, frequency: "monthly" },
    { amount: 40, frequency: "biweekly" },
  ];
  const plan = expensePlan(expenses, 760.3, 14);

  assert.equal(plan.paychecksPerMonth, 2);
  assert.equal(plan.monthlyIncome, 1520.6);
  assert.equal(plan.expensePerPaycheck * plan.paychecksPerMonth, plan.monthlyTotal);
  assert.equal(plan.remaining, 760.3 - plan.expensePerPaycheck);
});

test("recurring manual deposits add calendar-month income", () => {
  const rows = [
    {
      pay_date: "2026-07-01",
      pay_type: "Manual: VA Disability",
      income_type: "va-benefits",
      income_frequency: "monthly",
      net_pay: 1800,
    },
    {
      pay_date: "2026-08-01",
      pay_type: "Manual: VA Disability",
      income_type: "va-benefits",
      income_frequency: "monthly",
      net_pay: 1850,
    },
    {
      pay_date: "2026-08-03",
      pay_type: "Manual: Reimbursement",
      income_type: "other",
      income_frequency: "one-time",
      net_pay: 250,
    },
  ];

  assert.equal(monthlyIncomeFactor("weekly"), 4);
  assert.equal(monthlyIncomeFactor("biweekly"), 2);
  assert.equal(monthlyIncomeFactor("semimonthly"), 2);
  assert.equal(recurringManualIncome(rows), 1850);
  assert.equal(
    recurringManualIncome([
      ...rows,
      {
        pay_date: "2026-08-08",
        pay_type: "Manual: Employer paystub",
        income_type: "paystub",
        income_frequency: "biweekly",
        net_pay: 900,
      },
    ]),
    1850,
  );

  const plan = expensePlan([{ amount: 2000, frequency: "monthly" }], 760.3, 14, 1850);
  assert.equal(plan.monthlyIncome, 3370.6);
  assert.equal(plan.recurringMonthlyIncome, 1850);
  assert.equal(plan.expensePerPaycheck, 75);
  assert.equal(plan.remaining, 685.3);
});

test("inline expense edits accept positive currency amounts", () => {
  assert.equal(normalizeExpenseAmount("42.567"), 42.57);
  assert.equal(normalizeExpenseAmount("0"), null);
  assert.equal(normalizeExpenseAmount("not a number"), null);
  assert.equal(expenseAmountFromMonthly("600", "biweekly"), 300);
  assert.equal(expenseAmountFromMonthly("100", "annual"), 1200);
  assert.equal(expenseAmountFromMonthly("100", "one-time"), null);
});

test("expenses can be moved and drag-reordered without mutating the source list", () => {
  const expenses = [{ id: "rent" }, { id: "gas" }, { id: "phone" }];

  assert.deepEqual(moveExpenseByOffset(expenses, "gas", -1).map(({ id }) => id), [
    "gas",
    "rent",
    "phone",
  ]);
  assert.deepEqual(reorderExpenses(expenses, "rent", "phone", true).map(({ id }) => id), [
    "gas",
    "phone",
    "rent",
  ]);
  assert.deepEqual(expenses.map(({ id }) => id), ["rent", "gas", "phone"]);
});

test("pay statements seed the hypothetical paycheck calculator", () => {
  const inputs = calculatorInputsForPayStatement({
    gross_pay: 975,
    regular_rate: 10,
    regular_hours: 80,
    overtime_hours: 5,
    total_taxes: 100,
    total_deductions: 25,
  });

  assert.equal(inputs.regularHours, 80);
  assert.equal(inputs.overtime, 5);
  assert.equal(inputs.bonus, 100);
  assert.equal(inputs.taxRate, (100 / 975) * 100);
  assert.equal(inputs.deductionRate, (25 / 975) * 100);
});
