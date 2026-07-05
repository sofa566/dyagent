import crypto from "node:crypto";
import express from "express";

const PORT = Number(process.env.PORT || 3456);
const app = express();

app.use(express.json());
app.use(express.static(new URL("../public", import.meta.url).pathname));

app.post("/api/leave-requests", async (request, response) => {
  const validationResult = validateLeaveRequest(request.body);
  if (!validationResult.ok) {
    response.status(400).json({
      success: false,
      message: validationResult.message,
    });
    return;
  }

  const applicationPayload = mapToApplicationPayload(request.body);
  try {
    const applicationResponse = await fetchApplicationSystem(applicationPayload, request);
    response.status(201).json({
      success: true,
      requestId: applicationResponse.requestId,
      status: applicationResponse.status,
      message: "請假單已送出，等待主管審核。",
    });
  } catch {
    response.status(502).json({
      success: false,
      message: "目前無法連線應用系統，請稍後再試。",
    });
  }
});

app.post("/mock-application-system/leave-requests", (request, response) => {
  const { employeeCode, leaveType, totalDays } = request.body;
  if (!employeeCode || !leaveType || !Number.isFinite(totalDays)) {
    response.status(400).json({
      requestId: null,
      status: "rejected",
      message: "欄位不足，無法建立請假單。",
    });
    return;
  }

  const requestId = `LV-${new Date().toISOString().slice(0, 10).replace(/-/g, "")}-${crypto.randomInt(1000, 9999)}`;
  const requiresManualReview = totalDays >= 3 || leaveType === "special";

  response.status(201).json({
    requestId,
    status: requiresManualReview ? "pending_review" : "auto_accepted",
  });
});

app.listen(PORT, () => {
  console.log(`員工請假範例已啟動：http://localhost:${PORT}`);
});

function validateLeaveRequest(payload) {
  const requiredFields = [
    "employeeName",
    "employeeId",
    "leaveType",
    "startDate",
    "endDate",
    "reason",
  ];

  const missingField = requiredFields.find((fieldName) => {
    const fieldValue = payload[fieldName];
    return typeof fieldValue !== "string" || fieldValue.trim().length === 0;
  });

  if (missingField) {
    return { ok: false, message: `欄位 ${missingField} 為必填。` };
  }

  const startDate = new Date(payload.startDate);
  const endDate = new Date(payload.endDate);

  if (Number.isNaN(startDate.valueOf()) || Number.isNaN(endDate.valueOf())) {
    return { ok: false, message: "請填寫有效的開始與結束日期。" };
  }

  if (startDate > endDate) {
    return { ok: false, message: "開始日期不可晚於結束日期。" };
  }

  return { ok: true };
}

function mapToApplicationPayload(leaveRequestPayload) {
  const totalDays = calculateTotalDays(leaveRequestPayload.startDate, leaveRequestPayload.endDate);
  return {
    employeeCode: leaveRequestPayload.employeeId,
    employeeName: leaveRequestPayload.employeeName,
    leaveType: leaveRequestPayload.leaveType,
    startDate: leaveRequestPayload.startDate,
    endDate: leaveRequestPayload.endDate,
    reason: leaveRequestPayload.reason,
    totalDays,
  };
}

function calculateTotalDays(startDateText, endDateText) {
  const MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000;
  const startDate = new Date(startDateText);
  const endDate = new Date(endDateText);
  const timeDifference = endDate.valueOf() - startDate.valueOf();
  return Math.floor(timeDifference / MILLISECONDS_PER_DAY) + 1;
}

async function fetchApplicationSystem(applicationPayload, request) {
  const appSystemBaseUrl = process.env.APPLICATION_API_BASE_URL || `${request.protocol}://${request.get("host")}`;
  const apiResponse = await fetch(`${appSystemBaseUrl}/mock-application-system/leave-requests`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(applicationPayload),
  });

  if (!apiResponse.ok) {
    throw new Error("application system failed");
  }

  return apiResponse.json();
}
