"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  expenseFrequencyForCadence,
  goalPaycheckPlan,
  projectedPayDates,
  projectedPayDatesForMonth,
  totalGoalContribution,
} = require("../app.js");

const rows = [{ pay_date: "2026-07-10" }, { pay_date: "2026-07-24" }];
const today = new Date("2026-07-26T00:00:00");

test("goal schedule counts projected pay dates on or before the deadline", () => {
  assert.deepEqual(projectedPayDates(rows, "2026-08-19", today), ["2026-08-07"]);
  assert.deepEqual(projectedPayDates(rows, "2026-08-21", today), [
    "2026-08-07",
    "2026-08-21",
  ]);
});

test("calendar month highlights only projected paydays in that month", () => {
  assert.deepEqual(
    projectedPayDatesForMonth(rows, new Date("2026-08-01T00:00:00"), today),
    ["2026-08-07", "2026-08-21"],
  );
  assert.deepEqual(
    projectedPayDatesForMonth(rows, new Date("2026-09-01T00:00:00"), today),
    ["2026-09-04", "2026-09-18"],
  );
});

test("goal contribution divides the balance by actual projected checks", () => {
  const plan = goalPaycheckPlan(
    { target: 100, saved: 0, date: "2026-08-21" },
    rows,
    today,
  );

  assert.equal(plan.paycheckCount, 2);
  assert.equal(plan.contribution, 50);
  assert.deepEqual(plan.payDates, ["2026-08-07", "2026-08-21"]);
});

test("goal due before the next check requires the remaining balance", () => {
  const plan = goalPaycheckPlan(
    { target: 100, saved: 25, date: "2026-08-01" },
    rows,
    today,
  );

  assert.equal(plan.paycheckCount, 0);
  assert.equal(plan.contribution, 75);
  assert.equal(plan.nextPayDate.toISOString().slice(0, 10), "2026-08-07");
});

test("goal contributions total across active goals", () => {
  const goals = [
    { target: 100, saved: 0, date: "2026-08-21" },
    { target: 60, saved: 20, date: "2026-08-21" },
  ];

  assert.equal(totalGoalContribution(goals, rows, today), 70);
  assert.equal(expenseFrequencyForCadence(7), "weekly");
  assert.equal(expenseFrequencyForCadence(14), "biweekly");
  assert.equal(expenseFrequencyForCadence(30), "monthly");
});
