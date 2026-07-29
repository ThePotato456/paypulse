"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeCSVHeader, parseCSV } = require("../app.js");

test("CSV headers accept BOM-prefixed database field names", () => {
  const rows = parseCSV(
    "\uFEFFpay_date,period_begin,period_end,gross_pay,total_taxes,total_deductions,net_pay,hours_units\n" +
      "2026-07-24,2026-07-06,2026-07-19,894.04,131.02,20,743.02,79.07\n",
  );

  assert.equal(rows[0].pay_date, "2026-07-24");
  assert.equal(rows[0].gross_pay, 894.04);
});

test("dashboard export labels map back to persistent payroll fields", () => {
  assert.equal(normalizeCSVHeader("Pay Date"), "pay_date");
  assert.equal(normalizeCSVHeader("Hours / Units"), "hours_units");
  assert.equal(normalizeCSVHeader("Taxes"), "total_taxes");
  assert.equal(normalizeCSVHeader("Deductions"), "total_deductions");

  const rows = parseCSV(
    "Pay Date,Period Begin,Period End,Pay Type,Hours / Units,Regular Rate,Regular Hours,Overtime Hours,Gross Pay,Taxes,Deductions,Net Pay\n" +
      "2026-07-24,2026-07-06,2026-07-19,Payroll,79.07,11.25,78.27,0.8,894.04,131.02,20,743.02\n",
  );

  assert.equal(rows[0].payment_type, "Payroll");
  assert.equal(rows[0].overtime_hours, 0.8);
  assert.equal(rows[0].net_pay, 743.02);
});
