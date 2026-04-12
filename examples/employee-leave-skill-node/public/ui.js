const stepOneSection = document.getElementById("step-1");
const stepTwoSection = document.getElementById("step-2");
const reviewBox = document.getElementById("review-box");
const resultText = document.getElementById("result");

const formElement = document.getElementById("leave-form");
const nextButton = document.getElementById("next-button");
const backButton = document.getElementById("back-button");
const submitButton = document.getElementById("submit-button");

nextButton.addEventListener("click", () => {
  if (!formElement.reportValidity()) {
    return;
  }

  const formData = readFormData();
  renderReview(formData);
  toggleStep(true);
});

backButton.addEventListener("click", () => {
  resultText.textContent = "";
  toggleStep(false);
});

submitButton.addEventListener("click", async () => {
  const formData = readFormData();
  submitButton.disabled = true;
  resultText.textContent = "送出中...";

  try {
    const response = await fetch("/api/leave-requests", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(formData),
    });
    const result = await response.json();

    if (!response.ok) {
      resultText.textContent = `送出失敗：${result.message}`;
      return;
    }

    resultText.textContent = `送出成功，單號 ${result.requestId}，狀態 ${result.status}`;
  } catch {
    resultText.textContent = "送出失敗：網路異常。";
  } finally {
    submitButton.disabled = false;
  }
});

function readFormData() {
  const formData = new FormData(formElement);
  return {
    employeeName: String(formData.get("employeeName") || "").trim(),
    employeeId: String(formData.get("employeeId") || "").trim(),
    leaveType: String(formData.get("leaveType") || "").trim(),
    startDate: String(formData.get("startDate") || "").trim(),
    endDate: String(formData.get("endDate") || "").trim(),
    reason: String(formData.get("reason") || "").trim(),
  };
}

function renderReview(formData) {
  const reviewEntries = [
    ["員工姓名", formData.employeeName],
    ["員工編號", formData.employeeId],
    ["假別", formData.leaveType],
    ["開始日期", formData.startDate],
    ["結束日期", formData.endDate],
    ["請假原因", formData.reason],
  ];

  reviewBox.innerHTML = reviewEntries
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
}

function toggleStep(showReviewStep) {
  stepOneSection.classList.toggle("hidden", showReviewStep);
  stepTwoSection.classList.toggle("hidden", !showReviewStep);
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
