const reviewState = {
  initialized: false,
  initializing: true,
  dirty: false,
  dirtyFields: new Set(),
  session: null,
  review: null,
  target: null,
  targets: [],
  lookupToken: 0,
  selectionToken: 0,
  lookupPending: false,
  selectionPending: false,
  saving: false,
  lookupTimer: null,
  mailGeneration: 0,
  recommendation: null,
};

const form = document.querySelector("#reviewForm");
const saveButton = document.querySelector("#saveButton");
const targetQuery = document.querySelector("#targetQuery");
const targetResults = document.querySelector("#targetResults");
const mailBody = document.querySelector("#mailBody");
const mailText = document.querySelector("#mailText");

const RECOMMENDATION_LABELS = Object.freeze({
  stage_advanced: "检测到申请节点推进",
  explicit_start: "识别到开始时间",
  explicit_end: "识别到结束时间",
  explicit_deadline: "识别到截止时间",
  action_link: "识别到考试 / 面试链接",
  existing_task: "该阶段已有待办，建议原位更新而不是新建",
});

const TASK_STATUS_LABELS = Object.freeze({
  new: "待确认",
  needs_review: "待补时间",
  confirmed: "已确认",
  planned: "已安排",
  done: "已完成",
  cancelled: "已取消",
  expired: "已过期",
});

function formatLocalMinute(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function taskChoiceLabel(task) {
  const stage = task.round && task.round !== task.stage
    ? `${task.stage}·${task.round}`
    : task.stage;
  const when = task.time ? formatLocalMinute(task.time) : "无时间";
  const status = TASK_STATUS_LABELS[task.status] || task.status;
  const action = String(task.action_summary || task.title || "").slice(0, 28);
  return `${stage}｜${status}｜${when}｜${action}`;
}

function renderTaskChoices(tasks, suggestedId = "") {
  const select = document.querySelector("#existingTaskSelect");
  const hint = document.querySelector("#existingTaskHint");
  const previous = select.value;
  const choices = Array.isArray(tasks) ? tasks : [];
  select.replaceChildren(
    ...choices.map((task) => {
      const option = new Option(taskChoiceLabel(task), task.task_id);
      option.dataset.revision = task.revision || "";
      option.title = taskChoiceLabel(task);
      return option;
    }),
  );
  const preferred = [suggestedId, previous].find(
    (value) => value && choices.some((task) => task.task_id === value),
  );
  select.value = preferred || (choices[0] ? choices[0].task_id : "");
  hint.textContent = choices.length
    ? (suggestedId && select.value === suggestedId
        ? "已按阶段匹配到建议更新的待办；确认前请核对。"
        : "请选择这封邮件对应的待办。")
    : "该申请链上还没有待办，只能新建。";
}

function apiReady() {
  return Boolean(window.pywebview?.api);
}

function field(name) {
  return form.elements[name];
}

function setSelectValue(select, value) {
  const normalized = String(value ?? "");
  if (normalized && ![...select.options].some((option) => option.value === normalized)) {
    select.add(new Option(normalized, normalized));
  }
  select.value = normalized;
}

function clockPart(value, limit) {
  const digits = String(value || "").replace(/[^\d]/g, "");
  if (!digits) return "";
  const number = Number(digits);
  if (!Number.isFinite(number) || number < 0 || number > limit) return null;
  return String(number).padStart(2, "0");
}

function localInputValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

function nextStageAfter(stage) {
  const sequence = ["网申", "测评", "笔试", "一面", "二面", "三面", "HR 面", "Offer"];
  const index = sequence.indexOf(stage);
  return index >= 0 && index < sequence.length - 1 ? sequence[index + 1] : "";
}

function updateSaveAvailability() {
  saveButton.disabled = reviewState.lookupPending ||
    reviewState.selectionPending ||
    reviewState.saving;
}

function setLookupPending(pending, message = "") {
  reviewState.lookupPending = pending;
  document.querySelector("#lookupStatus").textContent =
    message || (pending ? "正在查找候选申请链…" : "");
  updateSaveAvailability();
}

function setSelectionPending(pending) {
  reviewState.selectionPending = pending;
  updateSaveAvailability();
}

function setDirty(dirty, name = "") {
  if (reviewState.initializing) return;
  if (name) reviewState.dirtyFields.add(name);
  if (reviewState.dirty === dirty) return;
  reviewState.dirty = dirty;
  if (apiReady()) {
    window.pywebview.api.set_dirty(dirty).catch(() => {});
  }
}

function applySession(session) {
  reviewState.session = session;
  field("window_id").value = session.window_id;
  field("source_hash").value = session.source_hash;
  field("request_id").value = session.request_id;
  field("expected_review_revision").value = session.review_revision;
  field("expected_application_revision").value =
    session.application_revision ?? "";
  field("application_key").value = session.target_key || "";
  document.querySelector("#sessionBadge").textContent =
    `独立窗口 · r${session.review_revision}`;
}

function setIfClean(name, value, transform = (item) => item ?? "") {
  if (reviewState.dirtyFields.has(name)) return;
  const control = field(name);
  if (!control) return;
  const next = transform(value);
  if (control.type === "checkbox") {
    control.checked = Boolean(next);
  } else if (control.tagName === "SELECT") {
    setSelectValue(control, next);
  } else {
    control.value = next;
  }
}

function targetValueOrIncoming(targetValue, reviewName) {
  if (
    targetValue !== null &&
    targetValue !== undefined &&
    String(targetValue).trim()
  ) {
    return targetValue;
  }
  return reviewState.review?.[reviewName] ?? "";
}

function toggleTaskFields() {
  const updating = selectedMode() === "update_task";
  document.querySelector("#existingTaskPicker").hidden = !updating;
  document.querySelector("#createTaskToggle").hidden = updating;
  document.querySelector("#taskFields").hidden =
    !(updating || field("create_task").checked);
}

function renderRecommendation(recommendation) {
  const reasons = (recommendation.reasons || []).map(
    (reason) => RECOMMENDATION_LABELS[reason] || reason,
  );
  document.querySelector("#recommendation").textContent = reasons.length
    ? `自动建议：${reasons.join("；")}。建议只用于预填，请核对后确认。`
    : "已预填邮件中的结构化信息，请核对申请身份与当前进度。";
  setIfClean("duration_minutes", recommendation.duration_minutes);
  setIfClean("source_url", recommendation.source_url);
  setIfClean("create_task", recommendation.default_create_task, Boolean);
  reviewState.recommendation = recommendation;
  renderTaskChoices(
    reviewState.target?.tasks || [],
    recommendation.suggested_task_id || "",
  );
  toggleTaskFields();
}

function modeControl(mode) {
  return form.querySelector(`input[name="mode"][value="${mode}"]`);
}

function selectedMode() {
  return form.querySelector('input[name="mode"]:checked')?.value || "";
}

function updateModeAvailability(target, defaultMode, { initial = false } = {}) {
  const hasTarget = Boolean(target);
  const lifecycle = String(target?.status || "");
  modeControl("new_identity").disabled = false;
  modeControl("new_attempt").disabled = !hasTarget;
  modeControl("update_active").disabled = !hasTarget || lifecycle !== "active";
  modeControl("update_task").disabled =
    !hasTarget || lifecycle !== "active" || !(target?.tasks || []).length;
  modeControl("reactivate").disabled =
    !hasTarget || !["ended", "archived"].includes(lifecycle);

  const current = modeControl(selectedMode());
  if (
    initial ||
    !current ||
    current.disabled ||
    !reviewState.dirtyFields.has("mode")
  ) {
    const next = modeControl(defaultMode);
    (next && !next.disabled ? next : modeControl("new_identity")).checked = true;
  }
}

function renderTargetSummary(target) {
  const summary = document.querySelector("#targetSummary");
  if (!target) {
    summary.hidden = true;
    summary.textContent = "";
    return;
  }
  summary.hidden = false;
  summary.textContent = target.label;
}

function applyTarget(target, defaultMode, { initial = false } = {}) {
  reviewState.target = target;
  setIfClean(
    "company",
    targetValueOrIncoming(target?.company, "company"),
  );
  setIfClean(
    "role",
    targetValueOrIncoming(
      target?.role === "岗位待确认" ? "" : target?.role,
      "role_canonical",
    ) || reviewState.review?.role || "",
  );
  setIfClean(
    "recruiting_project",
    targetValueOrIncoming(target?.project, "recruiting_project"),
  );
  setIfClean(
    "recruiting_year",
    targetValueOrIncoming(target?.recruiting_year, "recruiting_year"),
  );
  setIfClean(
    "business_unit",
    targetValueOrIncoming(target?.business_unit, "business_unit"),
  );
  setIfClean(
    "job_code",
    targetValueOrIncoming(target?.job_code, "job_code"),
  );
  setIfClean(
    "location",
    targetValueOrIncoming(target?.location, "location"),
  );
  updateModeAvailability(target, defaultMode, { initial });
  renderTargetSummary(target);
  renderTaskChoices(
    target?.tasks || [],
    reviewState.recommendation?.suggested_task_id || "",
  );
  toggleTaskFields();
}

function renderTargets(targets, selectedKey = "") {
  reviewState.targets = targets;
  const options = targets.map((target) => {
    const option = new Option(target.label, target.application_key);
    option.selected = target.application_key === selectedKey;
    option.title = target.label;
    return option;
  });
  targetResults.replaceChildren(...options);
  if (selectedKey && !targets.some((target) => target.application_key === selectedKey)) {
    const target = reviewState.target;
    if (target?.application_key === selectedKey) {
      const option = new Option(target.label, target.application_key);
      option.selected = true;
      targetResults.prepend(option);
    }
  }
}

async function runTargetLookup(query, token) {
  try {
    const response = await window.pywebview.api.search_targets(query, token);
    if (token !== reviewState.lookupToken || response.token !== token) return;
    renderTargets(response.targets || [], reviewState.session?.target_key || "");
    setLookupPending(
      false,
      response.targets?.length ? `找到 ${response.targets.length} 条候选` : "没有匹配的申请链",
    );
  } catch (error) {
    if (token !== reviewState.lookupToken) return;
    setLookupPending(false, error?.message || String(error));
  }
}

function scheduleTargetLookup() {
  clearTimeout(reviewState.lookupTimer);
  const token = ++reviewState.lookupToken;
  setLookupPending(true);
  reviewState.lookupTimer = setTimeout(
    () => runTargetLookup(targetQuery.value, token),
    280,
  );
}

async function selectTarget(applicationKey) {
  const token = ++reviewState.selectionToken;
  setSelectionPending(true);
  try {
    const response = await window.pywebview.api.select_target(applicationKey, token);
    if (token !== reviewState.selectionToken || response.token !== token) return;
    applySession(response.session);
    applyTarget(response.target, response.default_mode);
    renderRecommendation(response.recommendation);
    setDirty(true, "application_key");
  } catch (error) {
    if (token === reviewState.selectionToken) {
      document.querySelector("#reviewError").textContent =
        error?.message || String(error);
    }
  } finally {
    if (token === reviewState.selectionToken) setSelectionPending(false);
  }
}

function populateReview(review) {
  reviewState.review = review;
  document.querySelector("#identityExplanation").textContent = review.reason_label || "请核对申请归属后确认。";
  document.querySelector("#identitySources").textContent = review.identity_sources || "旧记录未保存识别来源。";
  setIfClean("company", review.company);
  setIfClean("role", review.role_canonical || review.role);
  setIfClean("role_raw", review.role_raw);
  setIfClean("role_canonical", review.role_canonical || review.role);
  setIfClean("recruiting_project", review.recruiting_project);
  setIfClean("recruiting_year", review.recruiting_year);
  setIfClean("business_unit", review.business_unit);
  setIfClean("job_code", review.job_code);
  setIfClean("location", review.location);
  setIfClean("location_confidence", review.location_confidence);
  setIfClean("location_source", review.location_source);
  const stage = review.stage || "网申";
  setIfClean("stage", stage);
  setIfClean("round", review.round);
  const completedStage = ["application", "offer", "rejection"].includes(
    review.event_type,
  ) || /通过|完成|未通过|拒绝|结束/.test(stage);
  setIfClean(
    "manual_stage_status",
    completedStage ? "completed" : "pending",
  );
  setIfClean(
    "next_stage",
    review.event_type === "rejection" ? "" : nextStageAfter(stage),
  );
  const localDeadline = localInputValue(
    review.deadline_at || review.start_at || review.end_at,
  );
  setIfClean("deadline_date", localDeadline.slice(0, 10));
  setIfClean("deadline_hour", localDeadline.slice(11, 13));
  setIfClean("deadline_minute", localDeadline.slice(14, 16));
  setIfClean("action_summary", review.action_summary || review.title);
}

function clearMail() {
  mailBody.replaceChildren();
  mailText.textContent = "";
  mailText.hidden = true;
}

async function loadOriginalMail(loadRemoteImages = false) {
  const generation = ++reviewState.mailGeneration;
  clearMail();
  const status = document.querySelector("#mailStatus");
  status.classList.remove("error");
  status.textContent = "正在通过只读 IMAP 按定位符读取…";
  document.querySelector("#remoteImagesButton").disabled = true;
  try {
    const mail = await window.pywebview.api.load_original_mail(loadRemoteImages);
    if (generation !== reviewState.mailGeneration) return;
    document.querySelector("#mailSubject").textContent = mail.subject || "（无主题）";
    document.querySelector("#mailMeta").textContent =
      `${mail.sender || "发件人未知"} · ${mail.received_at || "时间未知"}`;
    status.textContent = loadRemoteImages
      ? "已显式加载允许的 HTTPS 图片；不安全或非 HTTPS 图片仍保持阻止。"
      : mail.remote_images_blocked
        ? "远程图片已阻止；正文仅在当前窗口显示。"
        : "原邮件已通过只读 IMAP 加载；正文不会写入本地。";
    if (mail.html) {
      mailBody.innerHTML = mail.html;
    } else {
      mailText.textContent = mail.text || "（邮件正文为空）";
      mailText.hidden = false;
    }
    document.querySelector("#remoteImagesButton").hidden =
      !mail.remote_images_blocked || loadRemoteImages;
  } catch (error) {
    if (generation !== reviewState.mailGeneration) return;
    status.classList.add("error");
    status.textContent = error?.message || String(error);
  } finally {
    if (generation === reviewState.mailGeneration) {
      document.querySelector("#remoteImagesButton").disabled = false;
    }
  }
}

function formPayload() {
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.create_task = field("create_task").checked;
  payload.remember_correction = field("remember_correction").checked;
  payload.expected_review_revision = Number(payload.expected_review_revision);
  payload.expected_application_revision = payload.expected_application_revision
    ? Number(payload.expected_application_revision)
    : null;
  payload.duration_minutes = payload.duration_minutes
    ? Number(payload.duration_minutes)
    : null;
  payload.recruiting_year = payload.recruiting_year
    ? Number(payload.recruiting_year)
    : null;
  payload.location_confidence = payload.location_confidence
    ? Number(payload.location_confidence)
    : 0;
  // A single hard deadline, assembled from the date box and the two clock
  // boxes; the legacy window fields are always cleared.
  const hour = clockPart(payload.deadline_hour, 23);
  const minute = clockPart(payload.deadline_minute, 59);
  const date = String(payload.deadline_date || "").trim();
  if (hour === null || minute === null) throw new RangeError("时间填写不合法");
  payload.deadline_at = date
    ? new Date(`${date}T${hour || "00"}:${minute || "00"}`).toISOString()
    : null;
  payload.start_at = null;
  payload.end_at = null;
  delete payload.deadline_date;
  delete payload.deadline_hour;
  delete payload.deadline_minute;
  if (payload.mode === "update_task") {
    const select = document.querySelector("#existingTaskSelect");
    const option = select.selectedOptions[0];
    if (!option) throw new RangeError("请选择要更新的待办");
    payload.task_id = option.value;
    payload.expected_task_revision = option.dataset.revision || "";
    payload.create_task = false;
  } else {
    delete payload.task_id;
    delete payload.expected_task_revision;
  }
  return payload;
}

async function saveReview(event) {
  event.preventDefault();
  if (reviewState.lookupPending || reviewState.selectionPending || reviewState.saving) return;
  reviewState.saving = true;
  updateSaveAvailability();
  document.querySelector("#reviewError").textContent = "";
  document.querySelector("#saveStatus").textContent = "正在提交原子确认…";
  try {
    await window.pywebview.api.confirm(formPayload());
    reviewState.dirty = false;
    reviewState.dirtyFields.clear();
    document.querySelector("#saveStatus").textContent = "已保存，正在关闭窗口…";
  } catch (error) {
    document.querySelector("#reviewError").textContent =
      error?.message || String(error);
    document.querySelector("#saveStatus").textContent = "";
    reviewState.saving = false;
    updateSaveAvailability();
  }
}

async function requestClose() {
  if (reviewState.dirty && !window.confirm("有尚未保存的复核修改，确定关闭吗？")) {
    return;
  }
  reviewState.dirty = false;
  if (apiReady()) {
    await window.pywebview.api.set_dirty(false);
    await window.pywebview.api.close_window(true);
  }
}

async function initializeReview() {
  if (reviewState.initialized || !apiReady()) return;
  reviewState.initialized = true;
  try {
    const bootstrap = await window.pywebview.api.get_bootstrap();
    applySession(bootstrap.session);
    populateReview(bootstrap.review);
    applyTarget(bootstrap.target, bootstrap.default_mode, { initial: true });
    renderRecommendation(bootstrap.recommendation);
    renderTargets(
      bootstrap.initial_targets || [],
      bootstrap.session.target_key || "",
    );
    // Start with company-only lookup so a parser role mismatch never hides
    // manually created chains for the same employer. Users can narrow by role.
    targetQuery.value = bootstrap.review.company || "";
    reviewState.initializing = false;
    reviewState.dirty = false;
    toggleTaskFields();
    loadOriginalMail(false);
    scheduleTargetLookup();
  } catch (error) {
    reviewState.initializing = false;
    document.querySelector("#reviewError").textContent =
      error?.message || String(error);
    saveButton.disabled = true;
  }
}

form.addEventListener("input", (event) => {
  const name = event.target.name;
  if (!name || event.target.readOnly || event.target.type === "hidden") return;
  setDirty(true, name);
});
form.addEventListener("change", (event) => {
  const name = event.target.name;
  if (!name || event.target.readOnly || event.target.type === "hidden") return;
  setDirty(true, name);
  if (name === "create_task" || name === "mode") toggleTaskFields();
});
form.addEventListener("submit", saveReview);
targetQuery.addEventListener("input", scheduleTargetLookup);
targetResults.addEventListener("change", () => selectTarget(targetResults.value));
document.querySelector("#remoteImagesButton").addEventListener(
  "click",
  () => loadOriginalMail(true),
);
document.querySelector("#cancelButton").addEventListener("click", requestClose);
window.addEventListener("pywebviewready", initializeReview);
setTimeout(initializeReview, 600);
