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
  if (!payload || typeof payload !== "object") return;

  if (payload.type === "host.init") {
    channelNonce = String(payload.channel_nonce || "");
    interactionId = String(payload.interaction_id || "");
    currentStep = String(payload.step || "step-1");
    currentState = { ...(payload.state || {}) };
    renderUi();
    return;
  }

  if (payload.type === "host.update") {
    if (String(payload.channel_nonce || "") !== channelNonce) return;
    currentStep = String(payload.step || currentStep || "step-1");
    const nextState = payload.state && typeof payload.state === "object" ? payload.state : {};
    currentState = { ...(currentState || {}), ...nextState };
    renderUi();
    return;
  }

  if (payload.type === "host.error") {
    if (String(payload.channel_nonce || "") !== channelNonce) return;
    errorText.textContent = String(payload.error || "流程失敗");
  }
});

submitButton.addEventListener("click", () => {
  errorText.textContent = "";
  if (currentStep === "step-1" && !formElement.reportValidity()) return;
  window.parent.postMessage(
    {
      type: "skill.submit",
      channel_nonce: channelNonce,
      interaction_id: interactionId,
      step: currentStep,
      form_data: readFormData(),
    },
    "*",
  );
});

backButton.addEventListener("click", () => {
  window.parent.postMessage(
    {
      type: "skill.back",
      channel_nonce: channelNonce,
      interaction_id: interactionId,
      step: currentStep,
      form_data: readFormData(),
    },
    "*",
  );
});

cancelButton.addEventListener("click", () => {
  window.parent.postMessage(
    {
      type: "skill.cancel",
      channel_nonce: channelNonce,
      interaction_id: interactionId,
      step: currentStep,
      form_data: {},
    },
    "*",
  );
});

setInterval(() => {
  if (!channelNonce || !interactionId) return;
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
    stepLabel.textContent = "步驟 1/2：請填寫請款資訊";
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
    category: String(formData.get("category") || "travel").trim(),
    amount: String(formData.get("amount") || "").trim(),
    currency: String(formData.get("currency") || "TWD").trim(),
    description: String(formData.get("description") || "").trim(),
  };
}

function writeFormData(state) {
  const safeState = state && typeof state === "object" ? state : {};
  formElement.employeeName.value = String(safeState.employeeName || "");
  formElement.employeeId.value = String(safeState.employeeId || "");
  formElement.category.value = String(safeState.category || "travel");
  formElement.amount.value = String(safeState.amount || "");
  formElement.currency.value = String(safeState.currency || "TWD");
  formElement.description.value = String(safeState.description || "");
}

function buildReviewHtml(state) {
  const entries = [
    ["員工姓名", state.employeeName || ""],
    ["員工編號", state.employeeId || ""],
    ["請款類別", state.category || ""],
    ["金額", state.amount || ""],
    ["幣別", state.currency || ""],
    ["請款說明", state.description || ""],
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
