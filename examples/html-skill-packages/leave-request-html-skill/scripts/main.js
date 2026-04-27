#!/usr/bin/env node

function main() {
  const rawInput = process.env.SKILL_INPUT || "{}";
  const inputPayload = parseJson(rawInput, {});
  const interactionMeta = isObject(inputPayload._interaction) ? inputPayload._interaction : {};
  const actionText = String(interactionMeta.action || "start").trim().toLowerCase();
  const stepText = String(interactionMeta.step || "step-1").trim();
  const stateData = isObject(interactionMeta.state) ? interactionMeta.state : {};

  if (actionText === "cancel") {
    output({
      mode: "error",
      error: "使用者已取消請假流程",
      step: stepText,
    });
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
      output({
        mode: "error",
        error: errors.join("；"),
        step: "step-1",
      });
      return;
    }
    output(buildUiResult("step-2", mergedState));
    return;
  }

  if (actionText === "submit" && stepText === "step-2") {
    const mergedState = mergeFormData(stateData, inputPayload);
    const leaveRequestId = generateLeaveRequestId();
    output({
      mode: "final",
      step: "done",
      output: {
        request_id: leaveRequestId,
        status: "pending_review",
        leave_request: mergedState,
      },
      assistant_message: `已送出請假申請，單號 ${leaveRequestId}，目前待主管審核。`,
    });
    return;
  }

  output({
    mode: "error",
    error: `不支援的 action: ${actionText}`,
    step: stepText,
  });
}

function buildUiResult(stepText, stateData) {
  return {
    mode: "ui",
    step: stepText,
    ui: {
      entry: "ui/index.html",
      title: "員工請假申請",
      state: stateData,
    },
  };
}

function validateStepOne(data) {
  const errors = [];
  const requiredFields = ["employeeName", "employeeId", "leaveType", "startDate", "endDate", "reason"];
  for (const fieldName of requiredFields) {
    if (String(data[fieldName] || "").trim().length === 0) {
      errors.push(`${fieldName} 為必填`);
    }
  }
  if (String(data.startDate || "") && String(data.endDate || "") && String(data.startDate) > String(data.endDate)) {
    errors.push("開始日期不可晚於結束日期");
  }
  return errors;
}

function mergeFormData(stateData, inputPayload) {
  const cleanInput = { ...inputPayload };
  delete cleanInput._interaction;
  return {
    employeeName: String(cleanInput.employeeName || stateData.employeeName || "").trim(),
    employeeId: String(cleanInput.employeeId || stateData.employeeId || "").trim(),
    leaveType: String(cleanInput.leaveType || stateData.leaveType || "annual").trim(),
    startDate: String(cleanInput.startDate || stateData.startDate || "").trim(),
    endDate: String(cleanInput.endDate || stateData.endDate || "").trim(),
    reason: String(cleanInput.reason || stateData.reason || "").trim(),
  };
}

function generateLeaveRequestId() {
  const dateText = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  const randomPart = String(Math.floor(Math.random() * 9000) + 1000);
  return `L${dateText}${randomPart}`;
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
