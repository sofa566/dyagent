const formElement = document.getElementById("step-form");
const reviewElement = document.getElementById("review");
const errorText = document.getElementById("error-text");
const stepLabel = document.getElementById("step-label");
const submitButton = document.getElementById("submit-btn");
const backButton = document.getElementById("back-btn");
const cancelButton = document.getElementById("cancel-btn");

let channelNonce = "";
let interactionId = "";
let currentStep = "step-1";
let currentState = {};

window.addEventListener("message", (event) => {
  const payload = event.data;
  if (!payload || typeof payload !== "object") {
    return;
  }
  if (payload.type === "host.init") {
    channelNonce = String(payload.channel_nonce || "");
    interactionId = String(payload.interaction_id || "");
    currentStep = String(payload.step || "step-1");
    currentState = { ...(payload.state || {}) };
    renderUi();
    return;
  }
  if (payload.type === "host.update") {
    if (String(payload.channel_nonce || "") !== channelNonce) {
      return;
    }
    currentStep = String(payload.step || currentStep || "step-1");
    const nextState = payload.state && typeof payload.state === "object" ? payload.state : {};
    currentState = { ...(currentState || {}), ...nextState };
    renderUi();
    return;
  }
  if (payload.type === "host.error") {
    if (String(payload.channel_nonce || "") !== channelNonce) {
      return;
    }
    errorText.textContent = String(payload.error || "流程失敗");
  }
});

submitButton.addEventListener("click", () => {
  errorText.textContent = "";
  if (currentStep === "step-1") {
    if (!formElement.reportValidity()) {
      return;
    }
  }
  const message = {
    type: "skill.submit",
    channel_nonce: channelNonce,
    interaction_id: interactionId,
    step: currentStep,
    form_data: readFormData(),
  };
  window.parent.postMessage(message, "*");
});

backButton.addEventListener("click", () => {
  const message = {
    type: "skill.back",
    channel_nonce: channelNonce,
    interaction_id: interactionId,
    step: currentStep,
    form_data: readFormData(),
  };
  window.parent.postMessage(message, "*");
});

cancelButton.addEventListener("click", () => {
  const message = {
    type: "skill.cancel",
    channel_nonce: channelNonce,
    interaction_id: interactionId,
    step: currentStep,
    form_data: {},
  };
  window.parent.postMessage(message, "*");
});

setInterval(() => {
  if (!channelNonce || !interactionId) {
    return;
  }
  window.parent.postMessage(
    {
      type: "skill.heartbeat",
      channel_nonce: channelNonce,
      interaction_id: interactionId,
      step: currentStep,
      form_data: readFormData(),
    },
    "*",
  );
}, 15000);

function renderUi() {
  writeFormData(currentState);
  if (currentStep === "step-2") {
    stepLabel.textContent = "步驟 2/2：確認後送出";
    submitButton.textContent = "送出申請";
    backButton.classList.remove("hidden");
    formElement.classList.add("hidden");
    reviewElement.classList.remove("hidden");
    reviewElement.innerHTML = buildReviewHtml(currentState);
  } else {
    stepLabel.textContent = "步驟 1/2：請填寫請假資訊";
    submitButton.textContent = "下一步";
    backButton.classList.add("hidden");
    formElement.classList.remove("hidden");
    reviewElement.classList.add("hidden");
    reviewElement.innerHTML = "";
  }
}

function readFormData() {
  const formData = new FormData(formElement);
  return {
    employeeName: String(formData.get("employeeName") || "").trim(),
    employeeId: String(formData.get("employeeId") || "").trim(),
    leaveType: String(formData.get("leaveType") || "annual").trim(),
    startDate: String(formData.get("startDate") || "").trim(),
    endDate: String(formData.get("endDate") || "").trim(),
    reason: String(formData.get("reason") || "").trim(),
  };
}

function writeFormData(state) {
  const safeState = state && typeof state === "object" ? state : {};
  formElement.employeeName.value = String(safeState.employeeName || "");
  formElement.employeeId.value = String(safeState.employeeId || "");
  formElement.leaveType.value = String(safeState.leaveType || "annual");
  formElement.startDate.value = String(safeState.startDate || "");
  formElement.endDate.value = String(safeState.endDate || "");
  formElement.reason.value = String(safeState.reason || "");
}

function buildReviewHtml(state) {
  const leaveTypeLabelMap = {
    annual: "特休",
    sick: "病假",
    personal: "事假",
  };
  const leaveTypeValue = String(state.leaveType || "");
  const entries = [
    ["員工姓名", state.employeeName || ""],
    ["員工編號", state.employeeId || ""],
    ["假別", leaveTypeLabelMap[leaveTypeValue] || leaveTypeValue],
    ["開始日期", state.startDate || ""],
    ["結束日期", state.endDate || ""],
    ["請假原因", state.reason || ""],
  ];
  return entries
    .map(([label, value]) => `<div class="review-item"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(String(value))}</span></div>`)
    .join("");
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

window.parent.postMessage({ type: "skill.ready", channel_nonce: channelNonce, interaction_id: interactionId }, "*");
