#!/usr/bin/env node

function main() {
  const inputPayload = parseJson(process.env.SKILL_INPUT || "{}", {});
  const interactionMeta = isObject(inputPayload._interaction) ? inputPayload._interaction : {};
  const actionText = String(interactionMeta.action || "start").trim().toLowerCase();
  const stepText = String(interactionMeta.step || "step-1").trim();
  const stateData = isObject(interactionMeta.state) ? interactionMeta.state : {};

  if (actionText === "cancel") {
    output({ mode: "error", error: "使用者已取消請款流程", step: stepText });
    return;
  }

  if (actionText === "start" || actionText === "resume") {
    output(buildUiResult("step-1", stateData));
    return;
  }

  if (actionText === "back") {
    output(buildUiResult("step-1", stateData));
    return;
  }

  if (actionText === "heartbeat") {
    output(buildUiResult(stepText, stateData));
    return;
  }

  if (actionText === "submit" && stepText === "step-1") {
    const mergedState = mergeFormData(stateData, inputPayload);
    const errors = validateStepOne(mergedState);
    if (errors.length > 0) {
      output({ mode: "error", error: errors.join("；"), step: "step-1" });
      return;
    }
    output(buildUiResult("step-2", mergedState));
    return;
  }

  if (actionText === "submit" && stepText === "step-2") {
    const mergedState = mergeFormData(stateData, inputPayload);
    const expenseRequestId = generateExpenseId();
    output({
      mode: "final",
      step: "done",
      output: {
        request_id: expenseRequestId,
        status: "pending_finance_review",
        expense_request: mergedState,
      },
      assistant_message: `已送出請款申請，單號 ${expenseRequestId}，目前待財務審核。`,
    });
    return;
  }

  output({ mode: "error", error: `不支援的 action: ${actionText}`, step: stepText });
}

function buildUiResult(stepText, stateData) {
  return {
    mode: "ui",
    step: stepText,
    ui: {
      entry: "ui/index.html",
      title: "員工請款申請",
      state: stateData,
    },
  };
}

function validateStepOne(data) {
  const errors = [];
  const requiredFields = ["employeeName", "employeeId", "category", "amount", "currency", "description"];
  for (const fieldName of requiredFields) {
    if (String(data[fieldName] || "").trim().length === 0) {
      errors.push(`${fieldName} 為必填`);
    }
  }
  const amountValue = Number(data.amount || 0);
  if (!Number.isFinite(amountValue) || amountValue <= 0) {
    errors.push("amount 必須大於 0");
  }
  return errors;
}

function mergeFormData(stateData, inputPayload) {
  const cleanInput = { ...inputPayload };
  delete cleanInput._interaction;
  return {
    employeeName: String(cleanInput.employeeName || stateData.employeeName || "").trim(),
    employeeId: String(cleanInput.employeeId || stateData.employeeId || "").trim(),
    category: String(cleanInput.category || stateData.category || "travel").trim(),
    amount: String(cleanInput.amount || stateData.amount || "").trim(),
    currency: String(cleanInput.currency || stateData.currency || "TWD").trim(),
    description: String(cleanInput.description || stateData.description || "").trim(),
  };
}

function generateExpenseId() {
  const dateText = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const randomPart = String(Math.floor(Math.random() * 9000) + 1000);
  return `E${dateText}${randomPart}`;
}

function parseJson(rawText, fallbackValue) {
  try {
    return JSON.parse(rawText);
  } catch {
    return fallbackValue;
  }
}

function isObject(value) {
  return value && typeof value === "object" && !Array.isArray(value);
}

function output(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

main();
