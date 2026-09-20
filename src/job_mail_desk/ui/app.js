const state = {
  view: "today",
  payload: { tasks: [], counts: {}, health: {} },
  calendarAnchor: new Date(),
  selectedDate: new Date(),
  compact: false,
  calendarInitialized: false,
  saving: false,
  expandedCompanies: new Set(),
  expandedApplications: new Set(),
  setupInitialized: false,
  settingsFirstRun: false,
  reviewFilter: "all",
  dashboardRequestSeq: 0,
  mutationVersion: 0,
  navigationGeneration: 0,
  unreadAckGeneration: 0,
  settingsGeneration: 0,
  capsuleGeneration: 0,
  writeCount: 0,
  refreshDeferred: false,
  pendingDashboardPayload: null,
  cardSaving: new Set(),
  dirtyProgressCards: new Set(),
  applicationMode: "edit",
  materializingApplicationId: "",
  applicationChoices: [],
  ownershipChoices: [],
  ownershipRecord: null,
  mergeChoices: [],
  mergeSource: null,
  mergePreviewToken: "",
  persistedFontScale: 108,
};

const cards = document.querySelector("#cards");
const reviewTools = document.querySelector("#reviewTools");
const template = document.querySelector("#taskTemplate");
const healthText = document.querySelector("#healthText");
const healthDot = document.querySelector("#healthDot");
const refreshStatus = document.querySelector("#refreshStatus");
const urgentStrip = document.querySelector("#urgentStrip");
const taskDialog = document.querySelector("#taskDialog");
const taskForm = document.querySelector("#taskForm");
const ownershipDialog = document.querySelector("#ownershipDialog");
const ownershipForm = document.querySelector("#ownershipForm");
const applicationDialog = document.querySelector("#applicationDialog");
const applicationForm = document.querySelector("#applicationForm");
const applicationActionDialog = document.querySelector("#applicationActionDialog");
const applicationActionForm = document.querySelector("#applicationActionForm");
const settingsDialog = document.querySelector("#settingsDialog");
const settingsForm = document.querySelector("#settingsForm");
const originalMailDialog = document.querySelector("#originalMailDialog");
const originalMailBody = document.querySelector("#originalMailBody");
const originalMailText = document.querySelector("#originalMailText");
const dialogOpeners = new WeakMap();
const formWriteSnapshots = new WeakMap();

const APPLICATION_STAGES = Object.freeze([
  "网申", "测评", "笔试", "一面", "二面", "三面", "HR 面",
  "Offer", "已拒绝", "已结束",
]);
const STAGE_STATUSES = Object.freeze([
  ["pending", "未完成"],
  ["completed", "已完成"],
]);
const TERMINAL_STAGE_LABELS = Object.freeze([
  "已拒绝", "未通过", "已结束", "拒绝", "应聘终止", "流程终止", "流程结束", "撤回", "关闭",
]);

function createPinyinCollator() {
  try {
    return new Intl.Collator("zh-CN-u-co-pinyin", {
      usage: "sort",
      sensitivity: "base",
      numeric: true,
    });
  } catch (_error) {
    try {
      return new Intl.Collator("zh-CN", {
        usage: "sort",
        sensitivity: "base",
        numeric: true,
      });
    } catch (_fallbackError) {
      return { compare: (left, right) => String(left).localeCompare(String(right)) };
    }
  }
}

const PINYIN_COLLATOR = createPinyinCollator();

function isTerminalStage(value) {
  const text = String(value || "");
  return TERMINAL_STAGE_LABELS.some((label) => text.includes(label));
}

function applicationStage(application) {
  return String(
    application.manual_stage || application.current_stage || application.status_label || "",
  );
}

function stageDepth(value) {
  const stage = String(value || "").replace(/\s+/g, "");
  if (!stage || isTerminalStage(stage)) return 0;
  if (/offer|录用/i.test(stage)) return 8;
  if (/HR面|人力面|HR面试/i.test(stage)) return 7;
  if (/三面|终面|最终面|决赛面/.test(stage)) return 6;
  if (/二面|第二轮面/.test(stage)) return 5;
  if (/群面|AI面试|一面|第一轮面|面试/.test(stage)) return 4;
  if (/笔试|编程测试|机试/.test(stage)) return 3;
  if (/测评|性格测试|能力测试/.test(stage)) return 2;
  if (/网申|投递|简历筛选|申请/.test(stage)) return 1;
  return 0;
}

function isTerminalApplication(application) {
  return application.active === false ||
    ["ended", "archived"].includes(application.status) ||
    isTerminalStage(applicationStage(application));
}

function stageStatusKey(application) {
  return application.manual_stage_status ||
    (application.history?.[0]?.status === "done" ? "completed" : "pending");
}

function applicationUpdatedAt(application) {
  const candidates = [
    application.updated_at,
    application.completed_at,
    application.received_at,
    application.history?.[0]?.event_at,
  ];
  for (const value of candidates) {
    const timestamp = Date.parse(value || "");
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return 0;
}

function compareApplications(left, right) {
  const leftTerminal = isTerminalApplication(left);
  const rightTerminal = isTerminalApplication(right);
  const terminalOrder = Number(leftTerminal) - Number(rightTerminal);
  if (terminalOrder) return terminalOrder;
  if (leftTerminal && rightTerminal) {
    return applicationUpdatedAt(right) - applicationUpdatedAt(left) ||
      PINYIN_COLLATOR.compare(left.role || "", right.role || "") ||
      String(left.application_key || left.application_id || "").localeCompare(
        String(right.application_key || right.application_id || ""),
      );
  }
  return stageDepth(applicationStage(right)) - stageDepth(applicationStage(left)) ||
    Number(stageStatusKey(left) !== "pending") -
      Number(stageStatusKey(right) !== "pending") ||
    applicationUpdatedAt(right) - applicationUpdatedAt(left) ||
    PINYIN_COLLATOR.compare(left.role || "", right.role || "") ||
    String(left.application_key || left.application_id || "").localeCompare(
      String(right.application_key || right.application_id || ""),
    );
}

function sortApplications(applications) {
  return [...applications].sort(compareApplications);
}

function compareCompanyGroups(left, right) {
  const leftActive = left.items.filter((item) => !isTerminalApplication(item));
  const rightActive = right.items.filter((item) => !isTerminalApplication(item));
  const leftDepth = Math.max(0, ...leftActive.map((item) => stageDepth(applicationStage(item))));
  const rightDepth = Math.max(0, ...rightActive.map((item) => stageDepth(applicationStage(item))));
  return Number(!leftActive.length) - Number(!rightActive.length) ||
    rightDepth - leftDepth ||
    PINYIN_COLLATOR.compare(left.company, right.company) ||
    String(left.companyKey).localeCompare(String(right.companyKey));
}

function sortCompanyGroups(groups) {
  return [...groups].sort(compareCompanyGroups);
}

function captureScrollState() {
  return {
    cardsTop: cards.scrollTop,
    pageTop: document.scrollingElement?.scrollTop || 0,
  };
}

function restoreScrollState(snapshot) {
  requestAnimationFrame(() => {
    cards.scrollTop = snapshot.cardsTop;
    if (document.scrollingElement) {
      document.scrollingElement.scrollTop = snapshot.pageTop;
    }
  });
}

function showManagedDialog(dialog, initialFocus = null) {
  if (!dialog.open) {
    const opener = document.activeElement;
    if (opener instanceof HTMLElement) dialogOpeners.set(dialog, opener);
    dialog.showModal();
  }
  requestAnimationFrame(() => {
    const target = typeof initialFocus === "string"
      ? dialog.querySelector(initialFocus)
      : initialFocus;
    if (target instanceof HTMLElement && !target.disabled) target.focus();
  });
}

function setFormWriting(form, writing) {
  const dialog = form.closest("dialog");
  if (writing) {
    if (formWriteSnapshots.has(form)) return;
    const controls = [...form.querySelectorAll("button, input, select, textarea")];
    formWriteSnapshots.set(
      form,
      controls.map((control) => [control, control.disabled]),
    );
    controls.forEach((control) => { control.disabled = true; });
    form.setAttribute("aria-busy", "true");
    if (dialog) dialog.dataset.writing = "true";
    state.writeCount += 1;
    return;
  }
  const snapshot = formWriteSnapshots.get(form);
  if (!snapshot) return;
  snapshot.forEach(([control, wasDisabled]) => {
    if (control.isConnected) control.disabled = wasDisabled;
  });
  formWriteSnapshots.delete(form);
  form.removeAttribute("aria-busy");
  if (dialog) delete dialog.dataset.writing;
  state.writeCount = Math.max(0, state.writeCount - 1);
  scheduleDeferredRefresh();
}

function refreshBlockReason() {
  if (state.writeCount || state.saving || state.cardSaving.size) {
    return "正在保存，数据刷新已延后";
  }
  if (state.dirtyProgressCards.size) {
    return "有未保存的进展修改，数据刷新已延后";
  }
  if (document.querySelector("dialog[open]")) {
    return "窗口打开时，数据刷新已延后";
  }
  if (cards.querySelector("[data-armed]")) {
    // A two-click confirmation is in progress; re-rendering would drop it.
    return "等待二次确认，数据刷新已延后";
  }
  const active = document.activeElement;
  if (
    active instanceof HTMLElement &&
    active !== cards &&
    cards.contains(active) &&
    active.matches("input, select, textarea, [contenteditable='true']")
  ) {
    return "当前控件正在使用，数据刷新已延后";
  }
  return "";
}

function forEachProgressApplication(payload, callback) {
  for (const group of payload?.progress || []) {
    const entries = Array.isArray(group.applications) ? group.applications : [group];
    for (const application of entries) callback(application);
  }
}

function showDeferredRefresh(reason) {
  state.refreshDeferred = true;
  refreshStatus.textContent = reason;
  refreshStatus.hidden = false;
}

function focusFirstDirtyProgressCard() {
  const firstIdentity = state.dirtyProgressCards.values().next().value;
  if (!firstIdentity) return;
  const card = [...cards.querySelectorAll(".progress-application")].find(
    (item) => item.dataset.applicationKey === firstIdentity,
  );
  card?.querySelector("input, select, .save-progress")?.focus();
}

function clearDeferredRefresh() {
  state.refreshDeferred = false;
  refreshStatus.textContent = "";
  refreshStatus.hidden = true;
}

function installDashboardPayload(payload, scroll = captureScrollState()) {
  state.payload = payload;
  state.unreadAckGeneration += 1;
  initializeCalendarAnchor();
  render();
  restoreScrollState(scroll);
  clearDeferredRefresh();
}

function applyMutationPayload(payload) {
  state.mutationVersion += 1;
  state.dashboardRequestSeq += 1;
  if (state.dirtyProgressCards.size) {
    state.pendingDashboardPayload = payload;
    // The dirty card still posts expected_revision from the payload it was
    // rendered from; carry the new revisions over so its save cannot fail
    // on a conflict caused by our own successful mutation.
    const latestRevisions = new Map();
    forEachProgressApplication(payload, (application) => {
      const key = application.application_key ||
        application.legacy_application_id || application.application_id;
      if (key) latestRevisions.set(key, application.revision);
    });
    forEachProgressApplication(state.payload, (application) => {
      const key = application.application_key ||
        application.legacy_application_id || application.application_id;
      if (state.dirtyProgressCards.has(key) && latestRevisions.has(key)) {
        application.revision = latestRevisions.get(key);
      }
    });
    showDeferredRefresh("有未保存的进展修改，新数据将在保存或放弃后显示");
    return false;
  }
  state.pendingDashboardPayload = null;
  installDashboardPayload(payload);
  return true;
}

function scheduleDeferredRefresh() {
  if (!state.refreshDeferred && !state.pendingDashboardPayload) return;
  setTimeout(() => {
    if (refreshBlockReason()) return;
    if (state.pendingDashboardPayload) {
      const payload = state.pendingDashboardPayload;
      state.pendingDashboardPayload = null;
      installDashboardPayload(payload);
      return;
    }
    refresh({ reason: "deferred" });
  }, 0);
}

function acceptUnreadPayload(payload, generation) {
  if (generation !== state.unreadAckGeneration) return false;
  const currentSequence = Number(state.payload.unread?.snapshot_sequence || 0);
  const nextSequence = Number(payload?.snapshot_sequence || 0);
  if (nextSequence < currentSequence) return false;
  state.payload.unread = payload;
  renderCounts();
  return true;
}

function ensureSelectValue(select, value, label = value) {
  const normalized = String(value ?? "");
  if (!normalized && ![...select.options].some((option) => option.value === "")) {
    select.add(new Option("未设置", ""), 0);
  }
  if (normalized && ![...select.options].some((option) => option.value === normalized)) {
    select.add(new Option(label || normalized, normalized));
  }
  select.value = normalized;
}

function stageSelect(value, { allowEmpty = false, className = "" } = {}) {
  const select = document.createElement("select");
  select.className = className;
  if (allowEmpty) select.add(new Option("等待通知 / 暂未确定", ""));
  APPLICATION_STAGES.forEach((stage) => select.add(new Option(stage, stage)));
  ensureSelectValue(select, value);
  return select;
}

function statusSelect(value, className = "") {
  const select = document.createElement("select");
  select.className = className;
  STAGE_STATUSES.forEach(([key, label]) => select.add(new Option(label, key)));
  ensureSelectValue(select, value || "pending");
  return select;
}

const MAIL_PROVIDER_PRESETS = Object.freeze({
  qq: { host: "imap.qq.com", port: 993, ssl: true },
  163: { host: "imap.163.com", port: 993, ssl: true },
  126: { host: "imap.126.com", port: 993, ssl: true },
  yeah: { host: "imap.yeah.net", port: 993, ssl: true },
  gmail: { host: "imap.gmail.com", port: 993, ssl: true },
  outlook: { host: "outlook.office365.com", port: 993, ssl: true },
  custom: { host: "", port: 993, ssl: true },
});

function applyMailProviderPreset() {
  const provider = settingsForm.elements.mail_provider.value;
  const preset = MAIL_PROVIDER_PRESETS[provider];
  // Custom deliberately leaves all manually entered values untouched.
  if (!preset || provider === "custom") return;
  settingsForm.elements.mail_host.value = preset.host;
  settingsForm.elements.mail_port.value = preset.port;
  settingsForm.elements.mail_ssl.checked = preset.ssl;
}

function apiReady() {
  return window.pywebview && window.pywebview.api;
}

function escapeText(value) {
  return String(value ?? "");
}

function escapeHtml(value) {
  return escapeText(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function unreadScope(value) {
  return String(value || "")
    .normalize("NFKC")
    .trim()
    .replace(/\s+/g, " ")
    .toLocaleLowerCase("zh-CN");
}

const researchLabels = {
  not_queued: "",
  queued: "备战：待整理",
  running: "备战：整理中",
  completed: "备战：已就绪",
  blocked: "备战：需处理",
  closed: "",
};

const confirmationTimers = new WeakMap();
const guardedActions = new Set([
  "toggle_done",
  "snooze",
  "ignore",
  "trash",
  "restore_deleted",
  "permanent_delete",
]);

function requestAction(button, task, action) {
  if (!guardedActions.has(action)) {
    handleAction(task, action);
    return;
  }
  if (button.dataset.armed === action) {
    clearTimeout(confirmationTimers.get(button));
    confirmationTimers.delete(button);
    button.disabled = true;
    handleAction(task, action).finally(() => {
      if (button.isConnected) button.disabled = false;
      scheduleDeferredRefresh();
    });
    return;
  }
  const originalLabel = button.textContent;
  button.dataset.armed = action;
  button.classList.add("confirming");
  button.textContent = "再点确认";
  button.title = "3 秒内再次点击才会执行";
  const timer = setTimeout(() => {
    delete button.dataset.armed;
    button.classList.remove("confirming");
    button.textContent = originalLabel;
  }, 3000);
  confirmationTimers.set(button, timer);
}

function renderHealth() {
  const health = state.payload.health || {};
  const details = health.scan_details;
  const first = health.first_scan;
  const scanDetails = document.querySelector("#scanDetails");
  if (scanDetails) {
    scanDetails.textContent = details
      ? `最近扫描：${details.lookback_days ? `回看 ${details.lookback_days} 天` : "旧记录未保存回看天数"}，` +
        `读取 ${details.fetched || 0} 封，识别候选 ${details.candidates || 0} 封` +
        (details.skipped == null ? "" : `，去重跳过 ${details.skipped} 封`) +
        (details.filtered == null ? "" : `，宣传过滤 ${details.filtered} 封`) +
        `，读取失败 ${details.fetch_failed || 0} 封。` +
        (first ? ` 首次完成扫描：读取 ${first.fetched} 封，候选 ${first.candidates} 封。` : "")
      : "尚无扫描记录";
  }
  if (health.last_error) {
    healthText.textContent = `最近错误：${health.last_error}`;
    healthDot.className = "health-dot error";
    return;
  }
  if (health.last_scan_at) {
    const stamp = new Date(health.last_scan_at).toLocaleString("zh-CN", {
      hour12: false,
    });
    healthText.textContent = `最近扫描 ${stamp}` +
      (details ? ` · ${details.lookback_days ? `${details.lookback_days} 天 · ` : ""}读取 ${details.fetched || 0} 封 · 候选 ${details.candidates || 0} 封` : "");
    healthDot.className = "health-dot ok";
    return;
  }
  healthText.textContent = "尚未完成扫描";
  healthDot.className = "health-dot";
}

function renderCapsule() {
  const now = new Date();
  const actionable = state.payload.tasks.filter((task) =>
    task.actionable &&
    !task.deleted_at &&
    !["done", "cancelled", "expired", "irrelevant"].includes(task.status) &&
    (!task.snoozed_until || new Date(task.snoozed_until) <= now)
  );
  const urgent = actionable.filter((task) => task.priority === "urgent");
  const count = urgent.length || actionable.length;
  document.querySelector("#capsuleCount").textContent = count > 9 ? "9+" : String(count);
  document.querySelector("#capsuleCount").classList.toggle("urgent", urgent.length > 0);
}

function renderCounts() {
  const counts = state.payload.counts || {};
  document.querySelector("#countToday").textContent = counts.today || 0;
  document.querySelector("#countProgress").textContent = counts.progress || 0;
  const anchor = state.calendarAnchor;
  const weekStart = beginningOfWeek(anchor);
  const weekEnd = new Date(weekStart);
  weekEnd.setDate(weekEnd.getDate() + 7);
  const openCalendarTasks = calendarTasks().filter(
    (task) => !["done", "cancelled", "expired", "irrelevant"].includes(task.status),
  );
  document.querySelector("#countWeek").textContent = openCalendarTasks.filter(
    (task) => {
      const date = new Date(task.time);
      return date >= weekStart && date < weekEnd;
    },
  ).length;
  document.querySelector("#countMonth").textContent = openCalendarTasks.filter(
    (task) => {
      const date = new Date(task.time);
      return (
        date.getFullYear() === anchor.getFullYear() &&
        date.getMonth() === anchor.getMonth()
      );
    },
  ).length;
  document.querySelector("#countReview").textContent = counts.review || 0;
  document.querySelector("#countList").textContent = counts.list || 0;
  const unread = state.payload.unread || {};
  const unreadCounts = unread.counts || {};
  document.querySelectorAll(".tab[data-view]").forEach((tab) => {
    const count = Number(unreadCounts[tab.dataset.view] || 0);
    const badge = tab.querySelector(".unread-badge");
    if (badge) {
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.hidden = count <= 0;
    }
    tab.classList.toggle("has-unread", count > 0);
  });
  const urgent = state.payload.tasks.filter(
    (task) =>
      task.priority === "urgent" &&
      task.actionable &&
      !task.deleted_at &&
      !["done", "cancelled", "expired", "irrelevant"].includes(task.status) &&
      (!task.snoozed_until || new Date(task.snoozed_until) <= new Date()),
  );
  urgentStrip.classList.toggle("hidden", urgent.length === 0);
  urgentStrip.textContent = urgent.length
    ? `⚠ ${urgent.length} 个 24 小时内硬截止，请先核对官方通知`
    : "";
}

function dateKey(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function calendarTasks() {
  return state.payload.tasks.filter((task) => {
    if (!task.time || task.deleted_at || !task.actionable) return false;
    return Number.isFinite(new Date(task.time).getTime());
  });
}

function tasksOn(date) {
  const key = dateKey(date);
  return calendarTasks().filter(
    (task) => dateKey(new Date(task.time)) === key,
  );
}

function beginningOfWeek(date) {
  const value = new Date(date);
  const day = value.getDay() || 7;
  value.setDate(value.getDate() - day + 1);
  value.setHours(0, 0, 0, 0);
  return value;
}

function calendarToolbar(title, onPrevious, onNext, label) {
  const toolbar = document.createElement("div");
  toolbar.className = "calendar-toolbar";
  const previous = document.createElement("button");
  previous.type = "button";
  previous.textContent = "‹";
  previous.setAttribute("aria-label", `上一${label}`);
  previous.addEventListener("click", onPrevious);
  const heading = document.createElement("strong");
  heading.textContent = title;
  heading.setAttribute("aria-live", "polite");
  const next = document.createElement("button");
  next.type = "button";
  next.textContent = "›";
  next.setAttribute("aria-label", `下一${label}`);
  next.addEventListener("click", onNext);
  toolbar.append(previous, heading, next);
  return toolbar;
}

function eventNode(task) {
  const event = document.createElement("div");
  event.className = [
    "calendar-event",
    task.priority === "urgent" ? "urgent" : "",
    task.status === "done" ? "done" : "",
  ].join(" ");
  const time = document.createElement("time");
  time.textContent = new Date(task.time).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const label = document.createElement("span");
  label.textContent = `${task.company} · ${task.stage}`;
  const open = document.createElement("button");
  open.type = "button";
  open.className = "calendar-event-open";
  open.setAttribute("aria-label", `${task.company}，${task.stage}，打开待办详情`);
  open.addEventListener("click", () => showTaskDialog(task));
  open.append(time, label);
  const done = document.createElement("button");
  done.type = "button";
  done.className = "calendar-event-toggle";
  done.textContent = task.status === "done" ? "↶" : "✓";
  done.setAttribute(
    "aria-label",
    task.status === "done" ? `恢复 ${task.company} ${task.stage}` : `完成 ${task.company} ${task.stage}`,
  );
  done.title = task.status === "done" ? "恢复为待办" : "标记完成";
  done.addEventListener("click", (eventObject) => {
    eventObject.stopPropagation();
    requestAction(done, task, "toggle_done");
  });
  event.append(open, done);
  return event;
}

function renderWeek() {
  cards.replaceChildren();
  const start = beginningOfWeek(state.calendarAnchor);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  const title = `${start.getMonth() + 1}月${start.getDate()}日 — ${end.getMonth() + 1}月${end.getDate()}日`;
  cards.append(
    calendarToolbar(
      title,
      () => {
        state.calendarAnchor.setDate(state.calendarAnchor.getDate() - 7);
        render();
      },
      () => {
        state.calendarAnchor.setDate(state.calendarAnchor.getDate() + 7);
        render();
      },
      "周",
    ),
  );
  const agenda = document.createElement("div");
  agenda.className = "week-agenda";
  const weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
  for (let offset = 0; offset < 7; offset += 1) {
    const date = new Date(start);
    date.setDate(date.getDate() + offset);
    const row = document.createElement("section");
    const items = tasksOn(date);
    row.className = [
      "week-day",
      dateKey(date) === dateKey(new Date()) ? "today" : "",
      items.length ? "has-events" : "",
      items.some((item) => item.priority === "urgent") ? "urgent-day" : "",
    ].join(" ");
    const label = document.createElement("div");
    label.className = "week-date";
    label.innerHTML = `<span>${weekdays[offset]}</span><strong>${date.getDate()}</strong>`;
    if (items.length) {
      const count = document.createElement("em");
      count.className = "day-task-count";
      count.textContent = String(items.length);
      count.title = `${items.length} 条待办`;
      label.append(count);
    }
    const events = document.createElement("div");
    events.className = "week-events";
    if (items.length) {
      items.forEach((item) => events.append(eventNode(item)));
    } else {
      const empty = document.createElement("span");
      empty.className = "no-events";
      empty.textContent = "无安排";
      events.append(empty);
    }
    row.append(label, events);
    agenda.append(row);
  }
  cards.append(agenda);
}

function renderSelectedDay(container) {
  const section = document.createElement("section");
  section.className = "selected-day";
  const heading = document.createElement("h2");
  heading.textContent = `${state.selectedDate.getMonth() + 1}月${state.selectedDate.getDate()}日安排`;
  section.append(heading);
  const items = tasksOn(state.selectedDate);
  if (items.length) {
    items.forEach((item) => section.append(eventNode(item)));
  } else {
    const empty = document.createElement("span");
    empty.className = "no-events";
    empty.textContent = "这一天没有已确认时间的任务";
    section.append(empty);
  }
  container.append(section);
}

function renderMonth() {
  cards.replaceChildren();
  const anchor = state.calendarAnchor;
  const year = anchor.getFullYear();
  const month = anchor.getMonth();
  cards.append(
    calendarToolbar(
      `${year} 年 ${month + 1} 月`,
      () => {
        state.calendarAnchor = new Date(year, month - 1, 1);
        state.selectedDate = new Date(state.calendarAnchor);
        render();
      },
      () => {
        state.calendarAnchor = new Date(year, month + 1, 1);
        state.selectedDate = new Date(state.calendarAnchor);
        render();
      },
      "月",
    ),
  );
  const weekdays = document.createElement("div");
  weekdays.className = "month-weekdays";
  ["一", "二", "三", "四", "五", "六", "日"].forEach((day) => {
    const label = document.createElement("span");
    label.textContent = day;
    weekdays.append(label);
  });
  cards.append(weekdays);
  const grid = document.createElement("div");
  grid.className = "month-grid";
  grid.setAttribute("role", "grid");
  grid.setAttribute("aria-label", `${year} 年 ${month + 1} 月日历`);
  const first = beginningOfWeek(new Date(year, month, 1));
  for (let offset = 0; offset < 42; offset += 1) {
    const date = new Date(first);
    date.setDate(date.getDate() + offset);
    const cell = document.createElement("button");
    cell.type = "button";
    const items = tasksOn(date);
    cell.className = [
      "month-cell",
      date.getMonth() !== month ? "outside" : "",
      dateKey(date) === dateKey(new Date()) ? "today" : "",
      dateKey(date) === dateKey(state.selectedDate) ? "selected" : "",
      items.length ? "has-events" : "",
      items.some((item) => item.priority === "urgent") ? "urgent-day" : "",
    ].join(" ");
    cell.textContent = date.getDate();
    cell.dataset.calendarDate = dateKey(date);
    cell.tabIndex = dateKey(date) === dateKey(state.selectedDate) ? 0 : -1;
    cell.setAttribute("role", "gridcell");
    cell.setAttribute(
      "aria-label",
      `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日，${items.length} 条待办`,
    );
    cell.setAttribute(
      "aria-selected",
      String(dateKey(date) === dateKey(state.selectedDate)),
    );
    if (items.length) {
      const count = document.createElement("strong");
      count.className = "month-task-count";
      count.textContent = String(items.length);
      count.title = `${items.length} 条待办`;
      cell.append(count);
    }
    const dots = document.createElement("div");
    dots.className = "event-dots";
    items.slice(0, 5).forEach((item) => {
      const dot = document.createElement("i");
      dot.className = `event-dot ${item.priority === "urgent" ? "urgent" : ""}`;
      dots.append(dot);
    });
    cell.append(dots);
    const selectDate = (focus = false) => {
      state.selectedDate = date;
      if (date.getMonth() !== month || date.getFullYear() !== year) {
        state.calendarAnchor = new Date(date.getFullYear(), date.getMonth(), 1);
      }
      renderMonth();
      renderCounts();
      if (focus) {
        requestAnimationFrame(() => {
          cards.querySelector(`[data-calendar-date="${dateKey(date)}"]`)?.focus();
        });
      }
    };
    cell.addEventListener("click", () => selectDate());
    cell.addEventListener("keydown", (event) => {
      const nextDate = new Date(date);
      if (event.key === "ArrowLeft") nextDate.setDate(nextDate.getDate() - 1);
      else if (event.key === "ArrowRight") nextDate.setDate(nextDate.getDate() + 1);
      else if (event.key === "ArrowUp") nextDate.setDate(nextDate.getDate() - 7);
      else if (event.key === "ArrowDown") nextDate.setDate(nextDate.getDate() + 7);
      else if (event.key === "Home") {
        nextDate.setDate(nextDate.getDate() - ((nextDate.getDay() || 7) - 1));
      } else if (event.key === "End") {
        nextDate.setDate(nextDate.getDate() + (7 - (nextDate.getDay() || 7)));
      } else if (event.key === "PageUp") nextDate.setMonth(nextDate.getMonth() - 1);
      else if (event.key === "PageDown") nextDate.setMonth(nextDate.getMonth() + 1);
      else return;
      event.preventDefault();
      state.selectedDate = nextDate;
      state.calendarAnchor = new Date(nextDate.getFullYear(), nextDate.getMonth(), 1);
      renderMonth();
      renderCounts();
      requestAnimationFrame(() => {
        cards.querySelector(`[data-calendar-date="${dateKey(nextDate)}"]`)?.focus();
      });
    });
    grid.append(cell);
  }
  cards.append(grid);
  renderSelectedDay(cards);
}

async function handleAction(task, action) {
  if (!apiReady()) return;
  try {
    let mutationPayload = null;
    if (action === "toggle_done") {
      const restored = task.time ? "planned" : "needs_review";
      const status = task.status === "done" ? restored : "done";
      mutationPayload = await window.pywebview.api.update_status(
        task.id,
        status,
        task.revision || "",
      );
    } else if (action === "ignore") {
      mutationPayload = await window.pywebview.api.update_status(
        task.id,
        "irrelevant",
        task.revision || "",
      );
    } else if (action === "source") {
      await window.pywebview.api.open_source(task.id);
    } else if (action === "original_mail") {
      await showOriginalMail(task.id);
    } else if (action === "obsidian") {
      await window.pywebview.api.open_obsidian(task.id);
    } else if (action === "research") {
      await window.pywebview.api.open_research(task.id);
    } else if (action === "edit" || action === "edit_time") {
      showTaskDialog(task, action === "edit_time");
      return;
    } else if (action === "snooze") {
      const until = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();
      mutationPayload = await window.pywebview.api.snooze(
        task.id,
        until,
        task.revision || "",
      );
    } else if (action === "trash") {
      mutationPayload = await window.pywebview.api.trash_task(
        task.id,
        task.revision || "",
      );
      if (taskDialog.open) taskDialog.close();
    } else if (action === "restore_deleted") {
      mutationPayload = await window.pywebview.api.restore_task(
        task.id,
        task.revision || "",
      );
    } else if (action === "permanent_delete") {
      mutationPayload = await window.pywebview.api.permanently_delete_task(
        task.id,
        task.revision || "",
      );
    }
    if (mutationPayload) applyMutationPayload(mutationPayload);
  } catch (error) {
    healthText.textContent = error?.message || "待办操作失败，请刷新后重试";
    healthDot.className = "health-dot error";
    await refresh({ reason: "task-action-conflict" });
  }
}

function unresolvedMatches(record, filter) {
  if (filter === "recent") {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    return new Date(record.received_at).getTime() >= cutoff;
  }
  if (filter === "application") return record.event_type === "application";
  if (filter === "assessment") return /测评|笔试|作答/.test(record.stage || "");
  if (filter === "interview") {
    return /面试|一面|二面|三面|终面|HR\s*面|群面/.test(record.stage || "");
  }
  return true;
}

async function handleUnresolved(record, action, applicationKey = "") {
  if (!apiReady()) return;
  let payload = null;
  if (action === "ignore") {
    payload = await window.pywebview.api.ignore_unresolved(record.id);
  } else if (action === "resolve" && applicationKey) {
    payload = await window.pywebview.api.resolve_unresolved(
      record.id,
      applicationKey,
    );
  }
  if (payload) applyMutationPayload(payload);
}

function localInputValue(value) {
  if (!value) return "";
  const date = new Date(value);
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return shifted.toISOString().slice(0, 16);
}

function stageOption(value) {
  return String(value || "").trim();
}

function nextStageAfter(stage) {
  const sequence = ["网申", "测评", "笔试", "一面", "二面", "三面", "HR 面", "Offer"];
  const index = sequence.indexOf(stage);
  return index >= 0 && index < sequence.length - 1 ? sequence[index + 1] : "";
}

function confirmationRequestId() {
  const value = globalThis.crypto?.randomUUID?.().replaceAll("-", "");
  return `op1_${value || `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(32, "0").slice(0, 32)}`;
}

function renderOwnershipRecommendation(recommendation) {
  const labels = {
    stage_advanced: "检测到申请节点推进",
    explicit_start: "识别到开始时间",
    explicit_end: "识别到结束时间",
    explicit_deadline: "识别到截止时间",
    action_link: "识别到考试/面试链接",
    existing_task: "该阶段已有待办，建议原位更新",
  };
  const reasons = (recommendation.reasons || []).map((item) => labels[item] || item);
  const node = document.querySelector("#ownershipRecommendation");
  node.textContent = reasons.length
    ? `自动建议：${reasons.join("；")}。请核对后确认。`
    : "已自动预填邮件信息，请核对公司、岗位和进度。";
  node.classList.toggle("recommended", reasons.length > 0);
}

const OWNERSHIP_TASK_STATUS_LABELS = Object.freeze({
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

function ownershipTaskLabel(task) {
  const when = task.time ? formatLocalMinute(task.time) : "无时间";
  const status = OWNERSHIP_TASK_STATUS_LABELS[task.status] || task.status;
  return `${task.stage}${task.round && task.round !== task.stage ? `·${task.round}` : ""}｜${status}｜${when}`;
}

async function loadOwnershipTasks(suggestedId = "", { force = false } = {}) {
  const select = ownershipForm.elements.task_id;
  const applicationKey = ownershipForm.elements.application_key.value;
  const updateOption = ownershipForm.elements.mode.querySelector('option[value="update_task"]');
  if (!applicationKey) {
    select.replaceChildren();
    state.ownershipTasksKey = "";
    updateOption.disabled = true;
    return [];
  }
  if (!force && state.ownershipTasksKey === applicationKey && select.options.length) {
    return [...select.options].map((option) => ({ task_id: option.value }));
  }
  select.replaceChildren();
  const tasks = await window.pywebview.api.list_application_tasks(applicationKey);
  state.ownershipTasksKey = applicationKey;
  updateOption.disabled = tasks.length === 0;
  if (!tasks.length && ownershipForm.elements.mode.value === "update_task") {
    ownershipForm.elements.mode.value = "existing";
    applyOwnershipMode();
  }
  select.replaceChildren(
    ...tasks.map((task) => {
      const option = new Option(ownershipTaskLabel(task), task.task_id);
      option.dataset.revision = task.revision || "";
      return option;
    }),
  );
  const preferred = suggestedId && tasks.some((task) => task.task_id === suggestedId)
    ? suggestedId
    : (tasks[0] ? tasks[0].task_id : "");
  select.value = preferred;
  ownershipForm.elements.expected_task_revision.value =
    select.selectedOptions[0]?.dataset.revision || "";
  return tasks;
}

function applyOwnershipMode() {
  const mode = ownershipForm.elements.mode.value;
  const usesApplication = mode === "existing" || mode === "update_task";
  document.querySelector("#ownershipExistingRow").hidden = !usesApplication;
  document.querySelector("#ownershipTaskRow").hidden = mode !== "update_task";
  document.querySelector("#ownershipCreateTaskRow").hidden = mode === "update_task";
  const details = document.querySelector("#ownershipTaskDetails");
  if (mode === "update_task") {
    details.hidden = false;
    details.open = true;
  }
}

async function refreshOwnershipRecommendation() {
  const mode = ownershipForm.elements.mode.value;
  const existing = mode === "existing" || mode === "update_task";
  const applicationKey = existing
    ? ownershipForm.elements.application_key.value
    : "";
  const recommendation = await window.pywebview.api.get_review_recommendation(
    ownershipForm.elements.source_hash.value,
    applicationKey,
  );
  ownershipForm.elements.expected_review_revision.value =
    recommendation.review_revision;
  ownershipForm.elements.expected_application_revision.value =
    recommendation.application_revision ?? "";
  ownershipForm.elements.duration_minutes.value =
    recommendation.duration_minutes || "";
  ownershipForm.elements.source_url.value = recommendation.source_url || "";
  ownershipForm.elements.create_task.checked =
    Boolean(recommendation.default_create_task);
  const taskDetails = document.querySelector("#ownershipTaskDetails");
  taskDetails.hidden = !ownershipForm.elements.create_task.checked;
  taskDetails.open = ownershipForm.elements.create_task.checked;
  // A follow-up mail about a stage the chain already tracks defaults to
  // updating that task in place instead of adding a second one - once per
  // dialog, and never over a mode the user picked by hand.
  if (
    recommendation.suggest_update_task &&
    recommendation.suggested_task_id &&
    ownershipForm.elements.mode.value === "existing" &&
    !state.ownershipModeTouched &&
    !state.ownershipAutoSwitched
  ) {
    state.ownershipAutoSwitched = true;
    ownershipForm.elements.mode.value = "update_task";
  }
  applyOwnershipMode();
  if (ownershipForm.elements.mode.value === "update_task") {
    await loadOwnershipTasks(recommendation.suggested_task_id || "");
  }
  renderOwnershipRecommendation(recommendation);
}

async function showOwnershipDialog(record, preferredKey = "") {
  ownershipForm.reset();
  state.ownershipRecord = record;
  state.ownershipModeTouched = false;
  state.ownershipAutoSwitched = false;
  state.ownershipTasksKey = "";
  ownershipForm.elements.source_hash.value = record.id;
  ownershipForm.elements.request_id.value = confirmationRequestId();
  ownershipForm.elements.company.value = record.company || "";
  ownershipForm.elements.role.value = record.role || "";
  ownershipForm.elements.location.value = record.location || "";
  ownershipForm.elements.recruiting_project.value = record.recruiting_project || "";
  const currentStage = stageOption(record.stage) || "网申";
  ensureSelectValue(ownershipForm.elements.stage, currentStage);
  const completedStage = ["application", "offer", "rejection"].includes(
    record.event_type,
  ) || /通过|完成|未通过|拒绝|结束/.test(currentStage);
  ownershipForm.elements.manual_stage_status.value = completedStage
    ? "completed"
    : "pending";
  ensureSelectValue(
    ownershipForm.elements.next_stage,
    record.event_type === "rejection" ? "" : nextStageAfter(currentStage),
  );
  writeClockFields(
    ownershipForm,
    localInputValue(
      record.deadline_at || record.start_at || record.end_at,
    ),
  );
  ownershipForm.elements.action_summary.value =
    record.action_summary || record.title || "";
  await loadOwnershipChoices(
    record.company || "",
    record.role || "",
    preferredKey || record.recommended_application_key || "",
  );
  await refreshOwnershipRecommendation();
  document.querySelector("#ownershipError").textContent = "";
  showManagedDialog(ownershipDialog, ownershipForm.elements.company);
}

function renderOwnershipChoices(preferredKey = "") {
  const sameCompany = state.ownershipChoices.filter((item) => item.same_company);
  const select = ownershipForm.elements.application_key;
  const selected =
    state.ownershipChoices.find((item) => item.application_key === preferredKey) ||
    sameCompany.find((item) => item.recommended) ||
    sameCompany[0] ||
    null;
  select.replaceChildren(
    ...sameCompany.map((item) => {
      const option = document.createElement("option");
      option.value = item.application_key;
      option.textContent = `${item.company}｜${item.role}${item.location ? `｜${item.location}` : ""}`;
      option.selected = item.application_key === selected?.application_key;
      return option;
    }),
  );
  const existingOption = ownershipForm.elements.mode.querySelector('option[value="existing"]');
  existingOption.disabled = sameCompany.length === 0;
  return selected;
}

async function loadOwnershipChoices(company, role, preferredKey = "") {
  const choices = await window.pywebview.api.list_application_choices(company, role);
  state.ownershipChoices = choices;
  const selected = renderOwnershipChoices(preferredKey);
  const recommended = choices.filter((item) => item.recommended);
  const shouldUpdate = Boolean(
    selected && (
      selected.application_key === preferredKey ||
      (recommended.length === 1 &&
        selected.application_key === recommended[0].application_key)
    )
  );
  ownershipForm.elements.mode.value = shouldUpdate ? "existing" : "new";
  document.querySelector("#ownershipExistingRow").hidden = !shouldUpdate;
  return selected;
}

function applyOwnershipChoice() {
  const key = ownershipForm.elements.application_key.value;
  const choice = state.ownershipChoices.find((item) => item.application_key === key);
  if (!choice) return;
  ownershipForm.elements.company.value = choice.company || "";
  ownershipForm.elements.role.value =
    choice.role === "岗位待确认" ? "" : choice.role || "";
  ownershipForm.elements.location.value = choice.location || "";
  ownershipForm.elements.recruiting_project.value = choice.project || "";
}

function ownershipPayload() {
  const data = new FormData(ownershipForm);
  const payload = Object.fromEntries(data.entries());
  payload.create_task = ownershipForm.elements.create_task.checked;
  if (payload.mode === "update_task") {
    const option = ownershipForm.elements.task_id.selectedOptions[0];
    if (!option) throw new RangeError("请选择要更新的待办");
    payload.task_id = option.value;
    payload.expected_task_revision = option.dataset.revision || "";
    payload.create_task = false;
  } else {
    delete payload.task_id;
    delete payload.expected_task_revision;
  }
  payload.expected_review_revision = Number(payload.expected_review_revision);
  payload.expected_application_revision = payload.expected_application_revision
    ? Number(payload.expected_application_revision)
    : null;
  payload.duration_minutes = payload.duration_minutes
    ? Number(payload.duration_minutes)
    : null;
  // The ownership dialog now records the same single moment as the editor.
  const deadline = deadlineFieldValue(ownershipForm);
  payload.deadline_at = deadline.iso
    ? new Date(deadline.iso).toISOString()
    : null;
  payload.start_at = null;
  payload.end_at = null;
  delete payload.deadline_date;
  delete payload.deadline_hour;
  delete payload.deadline_minute;
  return payload;
}

async function saveOwnership(event) {
  event.preventDefault();
  let payload;
  try {
    payload = ownershipPayload();
  } catch (error) {
    document.querySelector("#ownershipError").textContent =
      error?.message || String(error);
    return;
  }
  setFormWriting(ownershipForm, true);
  try {
    const result = await window.pywebview.api.resolve_unresolved_workflow(
      payload.source_hash,
      payload,
    );
    ownershipDialog.close();
    applyMutationPayload(result);
  } catch (error) {
    document.querySelector("#ownershipError").textContent =
      error?.message || String(error);
  } finally {
    setFormWriting(ownershipForm, false);
  }
}

async function openReviewWindow(record, preferredKey = "") {
  if (!apiReady()) return;
  try {
    await window.pywebview.api.open_review_window(record.id, preferredKey);
  } catch (error) {
    healthText.textContent = error?.message || String(error);
    healthDot.className = "health-dot error";
  }
}

function unresolvedCard(record) {
  const card = document.createElement("article");
  card.className = "task-card unresolved-card";
  card.dataset.reviewSource = record.id;
  card.dataset.priority = "high";
  card.innerHTML = `
    <div class="card-head">
      <div><div class="company">${escapeHtml(record.company || "公司待确认")}</div>
      <div class="role">${escapeHtml(record.role || "岗位待确认")}</div></div>
      <div class="time-box"><div class="time-label">待归属</div>
      <div class="remaining">${escapeHtml(record.time_label || "时间待确认")}</div></div>
    </div>
    <div class="meta"><span class="stage">${escapeHtml(record.stage || "招聘通知")}</span>
      <span class="round">身份需要确认</span></div>
    <p class="action">${escapeHtml(record.action_summary || record.title || "请确认这封邮件属于哪条申请链")}</p>
    <p class="identity-reason">${escapeHtml(record.reason_label || "请核对申请归属后确认。")}</p>
    <details class="identity-evidence"><summary>识别依据</summary><p>${escapeHtml(record.identity_sources || "旧记录未保存识别来源。")}</p></details>
    <div class="unresolved-candidates"></div>
    <div class="actions">
      <button data-unresolved-assign="1">确认归属</button>
      <button data-unresolved-ignore="1" class="muted">忽略</button>
    </div>
  `;
  const candidateBox = card.querySelector(".unresolved-candidates");
  const candidates = (record.candidates || []).map((candidate) => {
    const button = document.createElement("button");
    button.className = "secondary-action open-review-window";
    button.type = "button";
    button.textContent = candidate.label ||
      `${candidate.company}｜${candidate.role}`;
    button.title = "在独立原生窗口中复核这封邮件";
    button.addEventListener("click", () =>
      openReviewWindow(record, candidate.application_key),
    );
    return button;
  });
  if (candidates.length) {
    const label = document.createElement("small");
    label.textContent = "归入申请链：";
    candidateBox.append(label, ...candidates);
  } else {
    candidateBox.textContent = "可选择已有申请，或直接创建新的公司/岗位申请。";
  }
  card
    .querySelector("[data-unresolved-assign]")
    .addEventListener("click", () => openReviewWindow(record));
  const ignore = card.querySelector("[data-unresolved-ignore]");
  ignore.addEventListener("click", () => {
    if (ignore.dataset.armed === "1") {
      handleUnresolved(record, "ignore");
      return;
    }
    ignore.dataset.armed = "1";
    ignore.textContent = "再点确认";
    setTimeout(() => {
      delete ignore.dataset.armed;
      ignore.textContent = "忽略";
    }, 3000);
  });
  return card;
}

function renderUnresolvedCards(records) {
  records.forEach((record) => cards.append(unresolvedCard(record)));
}

function createTaskCard(task, { compact = false } = {}) {
  const node = template.content.firstElementChild.cloneNode(true);
  node.dataset.priority = task.priority;
  node.classList.toggle("done", task.status === "done");
  node.classList.toggle("compact-task-card", compact);
  node.title = "点击卡片查看和修改详情";
  node.querySelector(".company").textContent = escapeText(task.company);
  node.querySelector(".role").textContent = escapeText(task.role);
  node.querySelector(".time-label").textContent = escapeText(task.time_label);
  node.querySelector(".remaining").textContent = escapeText(task.remaining);
  node.querySelector(".stage").textContent = escapeText(task.stage);
  const round = node.querySelector(".round");
  round.textContent = escapeText(task.round);
  round.hidden = !task.round;
  const research = node.querySelector(".research");
  research.textContent = researchLabels[task.research_status] || "";
  research.hidden = !research.textContent;
  research.title = "根据公司、岗位和阶段整理公开流程、题型与准备建议；不会再次读取或公开邮件正文。";
  const researchButton = node.querySelector(".research-open");
  researchButton.hidden = !task.research_result_path;
  const snoozed = node.querySelector(".snoozed");
  const snoozedUntil = task.snoozed_until ? new Date(task.snoozed_until) : null;
  snoozed.hidden = !snoozedUntil || snoozedUntil <= new Date();
  snoozed.textContent = snoozed.hidden
    ? ""
    : `已延后至 ${snoozedUntil.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })}`;
  node.querySelector(".action").textContent = escapeText(task.action);
  const doneButton = node.querySelector('[data-action="toggle_done"]');
  doneButton.textContent = task.status === "done" ? "恢复" : "完成";
  doneButton.title = task.status === "done"
    ? "首次点击准备恢复，再点一次执行"
    : "首次点击准备完成，再点一次执行";
  const timeButton = node.querySelector('[data-action="edit_time"]');
  timeButton.hidden = Boolean(task.time);
  timeButton.title = "打开详情并补充开始、结束或截止时间";
  const snoozeButton = node.querySelector('[data-action="snooze"]');
  snoozeButton.title = "两次点击后暂时移出提醒区 24 小时；不会修改活动时间";
  const sourceButton = node.querySelector('[data-action="source"]');
  sourceButton.hidden = !task.has_source;
  sourceButton.title = "打开邮件中提取到的通知或操作链接；不是打开邮箱原文";
  const originalButton = node.querySelector('[data-action="original_mail"]');
  originalButton.disabled = !task.has_mail_locator;
  originalButton.textContent = task.has_mail_locator
    ? "查看原邮件"
    : "原邮件定位不可用";
  originalButton.title = task.has_mail_locator
    ? "通过只读 IMAP 按需读取；正文不会保存到本地"
    : "原邮件定位不可用；重新扫描近期邮件可回填";
  node.querySelector('[data-action="ignore"]').title =
    "两次点击后永久忽略此本地任务；不会修改邮件";
  node.querySelector('[data-action="trash"]').title =
    "两次点击后移入待办回收站，并停止通知、移出系统日历";
  node.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", (eventObject) => {
      eventObject.stopPropagation();
      requestAction(button, task, button.dataset.action);
    });
  });
  node.addEventListener("click", () => showTaskDialog(task));
  return node;
}

function overviewSection(title, hint, items, className = "") {
  if (!items.length) return null;
  const section = document.createElement("section");
  section.className = `overview-section ${className}`.trim();
  const heading = document.createElement("header");
  heading.innerHTML = `<div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(hint)}</p></div><strong>${items.length}</strong>`;
  section.append(heading);
  const list = document.createElement("div");
  list.className = "overview-list";
  items.forEach((task) => list.append(createTaskCard(task, { compact: true })));
  section.append(list);
  return section;
}

function renderOverview() {
  const overview = state.payload.overview || {};
  const urgent = overview.urgent || [];
  const today = overview.today || [];
  const week = overview.week || [];
  const unresolvedLimit = window.innerWidth <= 480 ? 2 : 3;
  const unresolved = (overview.latest_unresolved || []).slice(0, unresolvedLimit);
  const sections = [
    overviewSection("三日内重点", "开始或截止时间已进入 72 小时提醒窗口", urgent, "overview-urgent"),
    overviewSection("本日待办", "今天需要完成的求职事项", today),
    overviewSection("本周待办", "本周其余已确认时间的事项", week),
  ].filter(Boolean);
  sections.forEach((section) => cards.append(section));
  if (unresolved.length) {
    const section = document.createElement("section");
    section.className = "overview-section overview-unresolved";
    const heading = document.createElement("header");
    heading.innerHTML = `<div><h2>最新待归属邮件</h2><p>确认后会自动从首页移除</p></div><strong>${unresolved.length}</strong>`;
    section.append(heading);
    const list = document.createElement("div");
    list.className = "overview-list";
    unresolved.forEach((record) => {
      const received = new Date(record.received_at);
      const receivedLabel = Number.isNaN(received.getTime())
        ? "收件时间待确认"
        : `收件 ${received.toLocaleString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          })}`;
      list.append(unresolvedCard({ ...record, time_label: receivedLabel }));
    });
    section.append(list);
    cards.append(section);
  }
  if (!sections.length && !unresolved.length) {
    const empty = document.createElement("div");
    empty.className = "empty overview-empty";
    empty.textContent = "近期没有需要处理的待办或待归属邮件。";
    cards.append(empty);
  }
}

function renderTaskTrash() {
  const deleted = state.payload.task_trash || [];
  const details = document.createElement("details");
  details.className = "task-trash";
  const summary = document.createElement("summary");
  summary.textContent = `待办回收站 ${deleted.length}`;
  details.append(summary);
  if (!deleted.length) {
    const empty = document.createElement("p");
    empty.className = "no-events";
    empty.textContent = "回收站为空";
    details.append(empty);
  }
  deleted.forEach((task) => {
    const row = document.createElement("article");
    row.className = "task-trash-row";
    row.innerHTML = `
      <div><strong>${escapeHtml(task.company)}</strong><span>${escapeHtml(task.role || task.stage)}</span></div>
      <div class="task-trash-actions">
        <button type="button" data-restore>恢复</button>
        <button type="button" data-delete class="danger-action">永久删除</button>
      </div>`;
    row.querySelector("[data-restore]").addEventListener("click", () =>
      handleAction(task, "restore_deleted"),
    );
    const remove = row.querySelector("[data-delete]");
    remove.addEventListener("click", () =>
      requestAction(remove, task, "permanent_delete"),
    );
    details.append(row);
  });
  cards.append(details);
}

function renderCards() {
  reviewTools.hidden = state.view !== "review";
  cards.replaceChildren(reviewTools);
  if (state.view === "today") {
    renderOverview();
    return;
  }
  if (state.view === "progress") {
    renderProgress();
    return;
  }
  if (state.view === "week") {
    renderWeek();
    return;
  }
  if (state.view === "month") {
    renderMonth();
    return;
  }
  let tasks = state.view === "list"
    ? state.payload.tasks.filter((task) => task.actionable)
    : state.payload.tasks.filter((task) => task.view === state.view);
  let unresolved = [];
  if (state.view === "review") {
    renderReviewFilterBar(tasks);
    tasks = filterReviewTasks(tasks, state.reviewFilter);
    unresolved = (state.payload.unresolved || []).filter((record) =>
      unresolvedMatches(record, state.reviewFilter),
    );
  }
  const hasTaskTrash = state.view === "list" && (state.payload.task_trash || []).length;
  if (!tasks.length && !unresolved.length && !hasTaskTrash) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "这一栏暂时没有任务。";
    cards.append(empty);
    return;
  }
  if (state.view === "review") renderUnresolvedCards(unresolved);
  for (const task of tasks) {
    cards.append(createTaskCard(task));
  }
  if (state.view === "list") renderTaskTrash();
}

function filterReviewTasks(tasks, filter) {
  if (filter === "recent") {
    const cutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
    return tasks.filter((task) => new Date(task.received_at).getTime() >= cutoff);
  }
  if (filter === "application") {
    return tasks.filter((task) =>
      task.event_type === "application" || /简历|网申/.test(task.stage),
    );
  }
  if (filter === "assessment") {
    return tasks.filter((task) => /测评|笔试|作答/.test(task.stage));
  }
  if (filter === "interview") {
    return tasks.filter((task) =>
      /面试|一面|二面|三面|终面|HR\s*面|群面/.test(task.stage)
    );
  }
  return tasks;
}

function renderReviewFilterBar(tasks) {
  const filters = [
    ["all", "全部"],
    ["recent", "近 7 天"],
    ["application", "简历筛选"],
    ["assessment", "测评/笔试"],
    ["interview", "面试"],
  ];
  const bar = document.createElement("nav");
  bar.className = "filter-bar";
  filters.forEach(([key, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = key === state.reviewFilter ? "active" : "";
    const unresolvedCount = (state.payload.unresolved || []).filter((record) =>
      unresolvedMatches(record, key),
    ).length;
    button.textContent = `${label} ${filterReviewTasks(tasks, key).length + unresolvedCount}`;
    button.addEventListener("click", () => {
      state.reviewFilter = key;
      renderCards();
    });
    bar.append(button);
  });
  cards.append(bar);
}

async function saveProgressCard(application, card) {
  const cardIdentity = application.application_key ||
    application.legacy_application_id || application.application_id;
  if (!apiReady() || state.cardSaving.has(cardIdentity)) return;
  const error = card.querySelector(".progress-save-error");
  const controls = card.querySelectorAll("input, select, button");
  const saveButton = card.querySelector(".save-progress");
  const saveStatus = card.querySelector(".progress-save-status");
  const idleText = saveButton.textContent;
  const roleInput = card.querySelector('[data-field="role"]');
  const role = roleInput.value.trim() || String(application.role || "").trim();
  if (!role) {
    error.textContent = "请填写岗位。";
    roleInput.focus();
    return;
  }
  state.cardSaving.add(cardIdentity);
  controls.forEach((control) => { control.disabled = true; });
  error.textContent = "";
  saveButton.textContent = "保存中…";
  saveStatus.textContent = "正在保存进度";
  try {
    const progressUpdate = {
      role,
      location: card.querySelector('[data-field="location"]').value.trim(),
      manual_stage: card.querySelector('[data-field="manual_stage"]').value,
      manual_stage_status: card.querySelector('[data-field="manual_stage_status"]').value,
      expected_revision: application.revision ?? null,
    };
    const payload = application.application_key
      ? await window.pywebview.api.edit_application(
          application.application_key,
          progressUpdate,
        )
      : await window.pywebview.api.materialize_progress_application({
          ...progressUpdate,
          legacy_application_id: cardIdentity,
          company: application.company,
          recruiting_project: application.recruiting_project || application.project || "",
        });
    state.dirtyProgressCards.delete(cardIdentity);
    card.querySelector(".discard-progress").hidden = true;
    applyMutationPayload(payload);
    requestAnimationFrame(() => {
      const applicationCard = [...cards.querySelectorAll(".progress-application")]
        .find((item) => item.dataset.applicationKey === cardIdentity);
      const renderedStatus = applicationCard?.querySelector(".progress-save-status");
      if (renderedStatus) renderedStatus.textContent = "进度已保存";
      applicationCard?.querySelector(".save-progress")?.focus();
    });
  } catch (exception) {
    error.textContent = exception?.message || String(exception);
    saveStatus.textContent = "保存失败";
    saveButton.textContent = idleText;
  } finally {
    state.cardSaving.delete(cardIdentity);
    controls.forEach((control) => { control.disabled = false; });
    scheduleDeferredRefresh();
  }
}

function progressSummaryRow(application, { terminal = false } = {}) {
  const statusKey = terminal ? "terminal" : stageStatusKey(application);
  const statusLabel = terminal
    ? "流程终止"
    : statusKey === "completed" ? "已完成" : "未完成";
  const role = application.role || "岗位待确认";
  const location = application.location || "";
  const lastNonTerminalEvent = terminal
    ? (application.history || []).find((event) =>
        event.stage && !isTerminalStage(event.stage)
      )
    : null;
  const stage = lastNonTerminalEvent?.stage ||
    applicationStage(application) || "节点待确认";
  return `<span class="progress-company-application">
    <span class="progress-company-field progress-company-role"
      aria-label="岗位：${escapeHtml(role)}" title="${escapeHtml(role)}">${escapeHtml(role)}</span>
    ${location
      ? `<span class="progress-company-field progress-company-location"
          aria-label="岗位地点：${escapeHtml(location)}" title="${escapeHtml(location)}">${escapeHtml(location)}</span>`
      : '<span class="progress-company-field progress-company-location-placeholder" aria-hidden="true"></span>'}
    <span class="progress-company-field progress-company-stage"
      aria-label="节点：${escapeHtml(stage)}" title="${escapeHtml(stage)}">${escapeHtml(stage)}</span>
    <span class="progress-company-field progress-company-status progress-company-pill ${escapeHtml(statusKey)}"
      aria-label="节点状态：${escapeHtml(statusLabel)}" title="${escapeHtml(statusLabel)}">${escapeHtml(statusLabel)}</span>
  </span>`;
}

function buildApplicationBody(application, card, { terminal = false } = {}) {
  const project = application.recruiting_project || application.project || "";
  const cardIdentity = application.application_key ||
    application.legacy_application_id || application.application_id;
  const body = document.createElement("div");
  body.className = "progress-application-body";
  body.innerHTML = `
    ${project ? `<div class="progress-project">招聘项目：${escapeHtml(project)}</div>` : ""}
    <div class="progress-quick-edit">
      <label>岗位<span data-control="role"></span></label>
      <label>地点<span data-control="location"></span></label>
      <label>节点<span data-control="stage"></span></label>
      <label>状态<span data-control="status"></span></label>
    </div>
    <div class="progress-card-actions progress-card-actions-main">
      <button type="button" class="primary-card save-progress">保存进度</button>
      <button type="button" class="secondary-action create-application-task">新建待办</button>
      <button type="button" class="secondary-action edit-application">编辑申请</button>
    </div>
    <div class="progress-card-actions progress-card-actions-manage">
      <button type="button" class="secondary-action merge-application">合并申请链</button>
      <button type="button" class="secondary-action danger-link delete-application">删除申请链</button>
    </div>
    <div class="progress-save-feedback">
      <span class="progress-save-status" role="status" aria-live="polite"></span>
      <span class="form-error progress-save-error" role="alert" aria-live="assertive"></span>
      <button type="button" class="discard-progress" hidden>放弃修改并刷新</button>
    </div>
    <section class="progress-timeline" aria-label="申请时间线">
      <h4>申请时间线</h4>
      <div class="progress-timeline-list"></div>
    </section>
  `;

  const roleInput = document.createElement("input");
  roleInput.type = "text";
  roleInput.className = "compact-input";
  roleInput.value = application.role || "";
  roleInput.setAttribute("list", "applicationRoleChoices");
  roleInput.dataset.field = "role";
  roleInput.required = true;
  roleInput.title = application.role || "";
  const locationInput = document.createElement("input");
  locationInput.type = "text";
  locationInput.className = "compact-input";
  locationInput.value = application.location || "";
  locationInput.dataset.field = "location";
  locationInput.title = application.location || "";
  const currentSelect = stageSelect(
    application.manual_stage || application.current_stage || "",
    { className: "compact-select" },
  );
  currentSelect.dataset.field = "manual_stage";
  const currentStatus = statusSelect(stageStatusKey(application), "compact-select");
  currentStatus.dataset.field = "manual_stage_status";
  body.querySelector('[data-control="role"]').append(roleInput);
  body.querySelector('[data-control="location"]').append(locationInput);
  body.querySelector('[data-control="stage"]').append(currentSelect);
  body.querySelector('[data-control="status"]').append(currentStatus);
  for (const control of [roleInput, locationInput, currentSelect, currentStatus]) {
    control.addEventListener("input", () => {
      state.dirtyProgressCards.add(cardIdentity);
      body.querySelector(".progress-save-status").textContent = "有未保存修改";
      body.querySelector(".discard-progress").hidden = false;
    });
    control.addEventListener("change", () => {
      state.dirtyProgressCards.add(cardIdentity);
      body.querySelector(".progress-save-status").textContent = "有未保存修改";
      body.querySelector(".discard-progress").hidden = false;
    });
  }
  body.querySelector(".save-progress").addEventListener("click", () =>
    saveProgressCard(application, card)
  );
  body.querySelector(".edit-application").addEventListener("click", () =>
    showApplicationDialog(application.application_key, application)
  );
  body.querySelector(".create-application-task").addEventListener("click", () =>
    showTaskDialog(null, false, application)
  );
  body.querySelector(".discard-progress").addEventListener("click", () => {
    state.dirtyProgressCards.delete(cardIdentity);
    if (state.dirtyProgressCards.size) {
      showDeferredRefresh("其他申请仍有未保存修改");
      focusFirstDirtyProgressCard();
      return;
    }
    document.querySelector(".tab.active")?.focus();
    if (state.pendingDashboardPayload) {
      const payload = state.pendingDashboardPayload;
      state.pendingDashboardPayload = null;
      installDashboardPayload(payload);
    } else {
      clearDeferredRefresh();
      refresh({ reason: "discard-progress-draft" });
    }
  });
  if (application.application_key) {
    body.querySelector(".merge-application").addEventListener("click", () =>
      showApplicationAction(application, "merge")
    );
    body.querySelector(".delete-application").addEventListener("click", () =>
      showApplicationAction(application, "delete")
    );
  } else {
    body.querySelector(".merge-application").hidden = true;
    body.querySelector(".delete-application").hidden = true;
  }
  if (terminal) {
    for (const control of [roleInput, locationInput, currentSelect, currentStatus]) {
      control.disabled = true;
    }
    body.querySelector(".save-progress").hidden = true;
    body.querySelector(".create-application-task").hidden = true;
    body.querySelector(".progress-save-status").textContent =
      "已结束申请只能在详情页明确重新激活，或从新邮件创建新批次。";
  }
  const timeline = body.querySelector(".progress-timeline-list");
  (application.history || []).forEach((event) => {
    const row = document.createElement("div");
    row.className = `progress-event ${event.status}`;
    const time = event.event_at
      ? new Date(event.event_at).toLocaleString("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })
      : "时间待确认";
    row.innerHTML = `
      <time>${event.time_inferred ? "约 " : ""}${escapeHtml(time)}</time>
      <span class="progress-event-stage">${escapeHtml(event.stage)}${event.round ? ` · ${escapeHtml(event.round)}` : ""}</span>
      <em class="progress-event-status">${escapeHtml(event.status_label || "状态待确认")}</em>
    `;
    appendTimelineSources(row, event);
    timeline.append(row);
  });
  if (!(application.history || []).length) {
    const emptyTimeline = document.createElement("p");
    emptyTimeline.className = "progress-timeline-empty";
    emptyTimeline.textContent = "暂无时间线记录";
    timeline.append(emptyTimeline);
  }
  return body;
}

function buildApplicationCard(application, { direct = false } = {}) {
  const terminal = isTerminalApplication(application);
  const card = document.createElement(direct ? "div" : "details");
  card.className = [
    "progress-application",
    "progress-card",
    direct ? "direct" : "",
    terminal ? "terminal closed" : "active",
  ].filter(Boolean).join(" ");
  const cardIdentity = application.application_key ||
    application.legacy_application_id || application.application_id;
  card.dataset.applicationKey = cardIdentity;
  card.dataset.revision = application.revision ?? "";
  if (!direct) {
    card.open = state.expandedApplications.has(cardIdentity);
    card.addEventListener("toggle", () => {
      if (card.open) state.expandedApplications.add(cardIdentity);
      else state.expandedApplications.delete(cardIdentity);
    });
    const summary = document.createElement("summary");
    summary.className = "progress-application-summary";
    summary.innerHTML = `
      <span class="progress-chevron" aria-hidden="true"></span>
      ${progressSummaryRow(application, { terminal })}
    `;
    card.append(summary);
  }
  card.append(buildApplicationBody(application, card, { terminal }));
  return card;
}

function applicationIdentity(record) {
  const project = record.recruiting_project || record.project || "";
  const location = record.location || "";
  return `${record.company || "公司待确认"}｜${record.role || "岗位待确认"}${location ? `｜${location}` : ""}${project ? `｜${project}` : ""}`;
}

function mergeApplicationIdentity(record) {
  const stage = applicationStage(record) || "节点待确认";
  const status = record.stage_status_label ||
    (stageStatusKey(record) === "completed" ? "已完成" : "未完成");
  return `${applicationIdentity(record)}｜${stage}｜${status}`;
}

function mergeTargetsFor(source, choices = []) {
  const duplicateKeys = new Set(
    (state.payload.duplicate_candidates || [])
      .filter((group) => group.applications.some((item) => item.application_key === source.application_key))
      .flatMap((group) => group.applications.map((item) => item.application_key)),
  );
  const records = new Map();
  (state.payload.progress || []).forEach((item) => {
    if (item.application_key) records.set(item.application_key, item);
  });
  choices.forEach((item) => {
    if (!item.application_key) return;
    records.set(item.application_key, {
      ...(records.get(item.application_key) || {}),
      ...item,
    });
  });
  return [...records.values()]
    .filter((item) => item.application_key !== source.application_key)
    .sort((left, right) =>
      Number(Boolean(right.same_company)) - Number(Boolean(left.same_company)) ||
      Number(duplicateKeys.has(right.application_key)) -
        Number(duplicateKeys.has(left.application_key)) ||
      PINYIN_COLLATOR.compare(left.company || "", right.company || "") ||
      PINYIN_COLLATOR.compare(left.role || "", right.role || "")
    );
}

function renderMergeTargets(query = "") {
  const target = applicationActionForm.elements.target;
  const normalized = query.trim().toLocaleLowerCase("zh-CN");
  const previous = target.value;
  const matches = state.mergeChoices.filter((item) =>
    !normalized || mergeApplicationIdentity(item).toLocaleLowerCase("zh-CN").includes(normalized)
  );
  target.replaceChildren();
  const grouped = new Map();
  matches.forEach((item) => {
    const company = item.company || "公司待确认";
    if (!grouped.has(company)) grouped.set(company, []);
    grouped.get(company).push(item);
  });
  grouped.forEach((items, company) => {
    const group = document.createElement("optgroup");
    group.label = company;
    items.forEach((item) => {
      const option = new Option(mergeApplicationIdentity(item), item.application_key);
      option.dataset.sameCompany = item.same_company ? "1" : "0";
      group.append(option);
    });
    target.append(group);
  });
  const preferred = matches.find((item) => item.application_key === previous) ||
    matches.find((item) => item.same_company) ||
    matches[0];
  target.value = preferred?.application_key || "";
  updateCrossCompanyConfirmation();
  document.querySelector("#applicationActionSubmit").disabled = !matches.length;
}

function updateCrossCompanyConfirmation() {
  const selected = state.mergeChoices.find(
    (item) => item.application_key === applicationActionForm.elements.target.value,
  );
  const row = document.querySelector("#crossCompanyConfirmRow");
  const crossCompany = Boolean(selected && !selected.same_company);
  row.hidden = !crossCompany;
  if (!crossCompany) {
    applicationActionForm.elements.cross_company_confirmed.checked = false;
  }
}

function invalidateMergePreview() {
  const hadPreview = applicationActionForm.elements.action.value === "confirm_merge" ||
    Boolean(state.mergePreviewToken);
  state.mergePreviewToken = "";
  if (!hadPreview) return;
  applicationActionForm.elements.action.value = "merge";
  document.querySelector("#applicationActionPreview").textContent =
    "目标已变化，请重新查看合并影响。";
  document.querySelector("#mergeConflictFields").hidden = true;
  document.querySelector("#applicationActionSubmit").textContent = "查看影响";
  applicationActionForm.elements.cross_company_confirmed.checked = false;
}

async function showApplicationAction(application, action) {
  applicationActionForm.reset();
  applicationActionForm.elements.source.value = application.application_key;
  applicationActionForm.elements.action.value = action;
  const isMerge = action === "merge";
  document.querySelector("#applicationActionTitle").textContent =
    isMerge ? "合并申请链" : "删除申请链";
  document.querySelector("#mergeTargetRow").hidden = !isMerge;
  document.querySelector("#crossCompanyConfirmRow").hidden = true;
  const target = applicationActionForm.elements.target;
  const choices = isMerge
    ? await window.pywebview.api.list_application_choices(application.company || "")
    : [];
  state.mergeSource = isMerge ? application : null;
  state.mergePreviewToken = "";
  state.mergeChoices = isMerge ? mergeTargetsFor(application, choices) : [];
  renderMergeTargets();
  target.disabled = !isMerge;
  document.querySelector("#applicationActionPreview").textContent = isMerge
    ? (target.options.length ? "已优先选择同公司申请；也可搜索并人工选择其他公司。" : "没有可合并的目标申请链。")
    : "将先读取任务与待归属影响，不会立即删除。";
  const conflictFields = document.querySelector("#mergeConflictFields");
  conflictFields.replaceChildren();
  conflictFields.hidden = true;
  document.querySelector("#applicationActionError").textContent = "";
  const submit = document.querySelector("#applicationActionSubmit");
  submit.textContent = "查看影响";
  submit.disabled = isMerge && !target.options.length;
  showManagedDialog(
    applicationActionDialog,
    isMerge ? applicationActionForm.elements.target_query : submit,
  );
}

function previewImpact(preview, permanent = false) {
  const record = preview.application || preview.source || {};
  const target = preview.target;
  return `
    <strong>${escapeHtml(mergeApplicationIdentity(record))}</strong>
    ${target ? `<span>合并到：${escapeHtml(mergeApplicationIdentity(target))}</span>` : ""}
    <span>关联任务：${preview.task_count || 0} · 待归属：${preview.unresolved_count || 0}</span>
    ${preview.conflicts?.length ? `<span class="warning">身份冲突：${escapeHtml(preview.conflicts.join("、"))}</span>` : ""}
    ${permanent ? '<span class="warning">永久删除会清理关联任务，且无法恢复。</span>' : ""}
  `;
}

function renderMergeConflicts(preview) {
  const container = document.querySelector("#mergeConflictFields");
  const labels = {
    company: "公司",
    role: "岗位",
    recruiting_project: "招聘项目",
    job_code: "职位编号",
    location: "岗位地点",
    recruiting_year: "招聘年份",
    business_unit: "业务单元",
  };
  container.replaceChildren(
    ...(preview.conflicts || []).map((field) => {
      const label = document.createElement("label");
      label.textContent = `${labels[field] || field}存在冲突`;
      const select = document.createElement("select");
      select.dataset.conflictField = field;
      select.add(new Option("请选择要保留的值", ""));
      select.add(new Option(`保留目标：${preview.target[field]}`, "target"));
      select.add(new Option(`使用来源：${preview.source[field]}`, "source"));
      label.append(select);
      return label;
    }),
  );
  container.hidden = !(preview.conflicts || []).length;
}

function mergeConflictOverrides() {
  const overrides = {};
  for (const select of document.querySelectorAll("[data-conflict-field]")) {
    if (!select.value) throw new Error("请先选择每个冲突字段要保留的值。");
    const field = select.dataset.conflictField;
    overrides[field] = select.value === "source"
      ? select.dataset.sourceValue
      : select.dataset.targetValue;
  }
  return overrides;
}

async function submitApplicationAction(event) {
  event.preventDefault();
  const source = applicationActionForm.elements.source.value;
  const target = applicationActionForm.elements.target.value;
  const action = applicationActionForm.elements.action.value;
  const submit = document.querySelector("#applicationActionSubmit");
  const error = document.querySelector("#applicationActionError");
  error.textContent = "";
  setFormWriting(applicationActionForm, true);
  try {
    if (action === "merge") {
      if (!target || source === target) throw new Error("请选择不同的目标申请。");
      const preview = await window.pywebview.api.get_application_merge_preview(source, target);
      if (preview.cross_company &&
          !applicationActionForm.elements.cross_company_confirmed.checked) {
        throw new Error("跨公司合并必须先勾选明确确认。");
      }
      document.querySelector("#applicationActionPreview").innerHTML = previewImpact(preview);
      renderMergeConflicts(preview);
      state.mergePreviewToken = preview.preview_token || "";
      document.querySelectorAll("[data-conflict-field]").forEach((select) => {
        const field = select.dataset.conflictField;
        select.dataset.sourceValue = preview.source[field] ?? "";
        select.dataset.targetValue = preview.target[field] ?? "";
      });
      applicationActionForm.elements.action.value = "confirm_merge";
      submit.textContent = "再次确认合并";
    } else if (action === "delete" || action === "permanent") {
      const preview = await window.pywebview.api.get_application_delete_preview(source);
      document.querySelector("#applicationActionPreview").innerHTML =
        previewImpact(preview, action === "permanent");
      applicationActionForm.elements.action.value =
        action === "permanent" ? "confirm_permanent" : "confirm_delete";
      submit.textContent = action === "permanent" ? "再次确认永久删除" : "再次确认移入回收站";
    } else {
      const payload = action === "confirm_merge"
        ? await window.pywebview.api.merge_applications(
            source,
            target,
            mergeConflictOverrides(),
            applicationActionForm.elements.cross_company_confirmed.checked,
            state.mergePreviewToken,
          )
        : action === "confirm_permanent"
          ? await window.pywebview.api.permanently_delete_application(source)
          : await window.pywebview.api.trash_application(source);
      applicationActionDialog.close();
      applyMutationPayload(payload);
    }
  } catch (exception) {
    error.textContent = exception?.message || String(exception);
    if (action === "confirm_merge" && /预览.*失效/.test(error.textContent)) {
      invalidateMergePreview();
    }
  } finally {
    setFormWriting(applicationActionForm, false);
  }
}

async function restoreApplication(applicationKey, button) {
  button.disabled = true;
  try {
    applyMutationPayload(await window.pywebview.api.restore_application(applicationKey));
  } finally {
    button.disabled = false;
  }
}

function appendTimelineSources(container, event) {
  const sources = event.sources || [];
  const details = document.createElement("details");
  details.className = "timeline-sources";
  const summary = document.createElement("summary");
  summary.textContent = `${event.source_count || sources.length || 1} 个来源`;
  details.append(summary);
  sources.forEach((source, index) => {
    const sourceRow = document.createElement("div");
    sourceRow.className = "timeline-source";
    const label = document.createElement("span");
    label.textContent = source.source_type === "manual"
      ? "手动更新"
      : (source.title || `通知来源 ${index + 1}`);
    sourceRow.append(label);
    if (source.task_id) {
      if (source.has_mail_locator) {
        const original = document.createElement("button");
        original.type = "button";
        original.textContent = "查看原邮件";
        original.addEventListener("click", (clickEvent) => {
          clickEvent.stopPropagation();
          showOriginalMail(source.task_id);
        });
        sourceRow.append(original);
      } else if (source.source_type === "mail") {
        const unavailable = document.createElement("small");
        unavailable.textContent = "原邮件定位不可用；重新扫描近期邮件可回填";
        sourceRow.append(unavailable);
      }
      if (source.source_url) {
        const link = document.createElement("button");
        link.type = "button";
        link.textContent = "打开通知链接";
        link.addEventListener("click", async (clickEvent) => {
          clickEvent.stopPropagation();
          if (apiReady()) await window.pywebview.api.open_source(source.task_id);
        });
        sourceRow.append(link);
      }
    } else if (source.source_hash && source.has_mail_locator) {
      const original = document.createElement("button");
      original.type = "button";
      original.textContent = "查看原邮件";
      original.addEventListener("click", (clickEvent) => {
        clickEvent.stopPropagation();
        showOriginalMailBySource(source.source_hash);
      });
      sourceRow.append(original);
    }
    details.append(sourceRow);
  });
  container.append(details);
}

function clearOriginalMail() {
  originalMailDialog.dataset.taskId = "";
  originalMailDialog.dataset.sourceHash = "";
  document.querySelector("#originalMailSubject").textContent = "原邮件";
  document.querySelector("#originalMailMeta").textContent = "";
  document.querySelector("#originalMailStatus").textContent = "";
  originalMailBody.replaceChildren();
  originalMailText.textContent = "";
  originalMailText.hidden = true;
  document.querySelector("#loadRemoteImages").hidden = true;
}

async function showOriginalMail(taskId, loadRemoteImages = false) {
  if (!apiReady()) return;
  clearOriginalMail();
  originalMailDialog.dataset.taskId = taskId;
  document.querySelector("#originalMailStatus").textContent = "正在通过只读 IMAP 读取…";
  if (!originalMailDialog.open) {
    showManagedDialog(originalMailDialog, document.querySelector("#dismissOriginalMail"));
  }
  try {
    const mail = await window.pywebview.api.get_original_mail(
      taskId,
      loadRemoteImages,
    );
    if (originalMailDialog.dataset.taskId !== taskId) return;
    document.querySelector("#originalMailSubject").textContent =
      mail.subject || "（无主题）";
    document.querySelector("#originalMailMeta").textContent =
      `${mail.sender || "发件人未知"} · ${mail.received_at || "时间未知"}`;
    document.querySelector("#originalMailStatus").textContent =
      mail.remote_images_blocked ? "远程图片已阻止，正文仅在当前窗口显示。" : "";
    if (mail.html) {
      originalMailBody.innerHTML = mail.html;
    } else {
      originalMailText.textContent = mail.text || "（邮件正文为空）";
      originalMailText.hidden = false;
    }
    document.querySelector("#loadRemoteImages").hidden =
      !mail.remote_images_blocked || loadRemoteImages;
  } catch (error) {
    if (originalMailDialog.dataset.taskId === taskId) {
      document.querySelector("#originalMailStatus").textContent =
        error?.message || String(error);
    }
  }
}

async function showOriginalMailBySource(sourceHash, loadRemoteImages = false) {
  if (!apiReady()) return;
  clearOriginalMail();
  originalMailDialog.dataset.sourceHash = sourceHash;
  document.querySelector("#originalMailStatus").textContent =
    "正在通过只读 IMAP 读取…";
  if (!originalMailDialog.open) {
    showManagedDialog(originalMailDialog, document.querySelector("#dismissOriginalMail"));
  }
  try {
    const mail = await window.pywebview.api.get_original_mail_by_source(
      sourceHash,
      loadRemoteImages,
    );
    if (originalMailDialog.dataset.sourceHash !== sourceHash) return;
    document.querySelector("#originalMailSubject").textContent =
      mail.subject || "（无主题）";
    document.querySelector("#originalMailMeta").textContent =
      `${mail.sender || "发件人未知"} · ${mail.received_at || "时间未知"}`;
    document.querySelector("#originalMailStatus").textContent =
      mail.remote_images_blocked ? "远程图片已阻止，正文仅在当前窗口显示。" : "";
    if (mail.html) {
      originalMailBody.innerHTML = mail.html;
    } else {
      originalMailText.textContent = mail.text || "（邮件正文为空）";
      originalMailText.hidden = false;
    }
    document.querySelector("#loadRemoteImages").hidden =
      !mail.remote_images_blocked || loadRemoteImages;
  } catch (error) {
    if (originalMailDialog.dataset.sourceHash === sourceHash) {
      document.querySelector("#originalMailStatus").textContent =
        error?.message || String(error);
    }
  }
}

function renderProgress() {
  cards.replaceChildren();
  const applications = state.payload.progress || [];
  const trash = state.payload.trash || [];
  if (!applications.length && !trash.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂时没有可汇总的求职流程。";
    cards.append(empty);
    return;
  }
  const companies = new Map();
  applications.forEach((application) => {
    const companyKey = application.company_key || application.company || "unknown-company";
    const group = companies.get(companyKey) || [];
    group.push(application);
    companies.set(companyKey, group);
  });
  const companyGroups = sortCompanyGroups(
    [...companies.entries()].map(([companyKey, items]) => ({
      companyKey,
      company: items.find((item) => item.company)?.company || "公司待确认",
      items: sortApplications(items),
    })),
  );

  const overview = document.createElement("div");
  overview.className = "progress-overview";
  const activeCount = applications.filter(
    (application) => !isTerminalApplication(application),
  ).length;
  const overviewText = document.createElement("span");
  overviewText.textContent = `${companies.size} 家企业 · ${activeCount} 条申请进行中`;
  const toggleAll = document.createElement("button");
  const allExpanded = companyGroups.every(({ companyKey }) =>
    state.expandedCompanies.has(companyKey),
  );
  toggleAll.textContent = allExpanded ? "收起所有公司" : "展开所有公司";
  toggleAll.addEventListener("click", () => {
    if (state.dirtyProgressCards.size) {
      showDeferredRefresh("请先保存当前进展修改，再调整展开状态");
      focusFirstDirtyProgressCard();
      return;
    }
    state.expandedCompanies = allExpanded
      ? new Set()
      : new Set(companyGroups.map(({ companyKey }) => companyKey));
    renderProgress();
  });
  overview.append(overviewText, toggleAll);
  cards.append(overview);

  companyGroups.forEach(({ items, companyKey, company }) => {
    const activeItems = items.filter(
      (application) => !isTerminalApplication(application),
    );
    const allTerminal = activeItems.length === 0;
    const hasPending = activeItems.some(
      (application) => stageStatusKey(application) === "pending",
    );
    const group = document.createElement("details");
    const progressUnreadCompanies = new Set(
      (state.payload.unread?.progress_unread_companies || []).map(unreadScope),
    );
    const progressScopesByKey = new Map();
    [
        ...items.map((item) => item.progress_scope || item.company_key),
        companyKey,
        company,
      ]
      .filter(Boolean)
      .forEach((scope) => {
        const key = unreadScope(scope);
        if (key && !progressScopesByKey.has(key)) {
          progressScopesByKey.set(key, String(scope));
        }
      });
    const progressScopes = [...progressScopesByKey.values()];
    let companyUnreadCount = progressScopes.filter((scope) =>
      progressUnreadCompanies.has(unreadScope(scope))
    ).length;
    let companyUnread = companyUnreadCount > 0;
    group.className = [
      "progress-company",
      hasPending ? "attention" : "",
      allTerminal ? "all-terminal" : "",
      companyUnread ? "has-unread" : "",
    ].filter(Boolean).join(" ");
    group.dataset.companyKey = companyKey;
    group.open = state.expandedCompanies.has(companyKey);
    group.addEventListener("toggle", () => {
      if (group.open) state.expandedCompanies.add(companyKey);
      else state.expandedCompanies.delete(companyKey);
    });

    const summary = document.createElement("summary");
    summary.className = "progress-company-summary";
    summary.title = `${company}｜${items.map((item) => item.role || "岗位待确认").join("、")}`;
    const summaryItems = allTerminal ? items.slice(0, 1) : activeItems.slice(0, 2);
    const summaryApplications = `
      <span class="progress-company-applications">
        ${summaryItems.map((application) =>
          progressSummaryRow(application, { terminal: allTerminal })
        ).join("")}
        ${!allTerminal && activeItems.length > 2
          ? `<span class="progress-company-more">+${activeItems.length - 2}</span>`
          : ""}
      </span>
    `;
    summary.innerHTML = `
      <span class="progress-chevron" aria-hidden="true"></span>
      <span class="progress-company-identity">
        <strong>${escapeHtml(company)}</strong>
        <small>${items.length} 条申请</small>
      </span>
      ${summaryApplications}
      ${companyUnread
        ? `<b class="company-unread-badge" aria-label="${companyUnreadCount} 个未读进展">${companyUnreadCount > 99 ? "99+" : companyUnreadCount}</b>`
        : ""}
    `;
    summary.addEventListener("click", async () => {
      if (!companyUnread || !apiReady()) return;
      const snapshotSequence = state.payload.unread?.snapshot_sequence || 0;
      const generation = ++state.unreadAckGeneration;
      try {
        const unread = await window.pywebview.api.acknowledge_progress_update(
          progressScopes,
          snapshotSequence,
        );
        if (!acceptUnreadPayload(unread, generation)) return;
        group.classList.remove("has-unread");
        summary.querySelector(".company-unread-badge")?.remove();
        companyUnread = false;
        companyUnreadCount = 0;
      } catch (error) {
        healthText.textContent = error?.message || "进展未读状态更新失败";
        healthDot.className = "health-dot error";
      }
    });
    const body = document.createElement("div");
    body.className = "progress-company-body";
    if (items.length === 1) {
      body.append(buildApplicationCard(items[0], { direct: true }));
    } else {
      items.forEach((application) => {
        body.append(buildApplicationCard(application));
      });
    }
    group.append(summary, body);
    cards.append(group);
  });

  if (trash.length) {
    const recycle = document.createElement("details");
    recycle.className = "trash-section";
    recycle.innerHTML = `<summary>回收站 <span>${trash.length}</span></summary>`;
    const body = document.createElement("div");
    body.className = "trash-list";
    trash.forEach((application) => {
      const row = document.createElement("article");
      row.className = "trash-card";
      row.innerHTML = `
        <div><strong>${escapeHtml(applicationIdentity(application))}</strong>
        <small>${application.task_count || 0} 个关联任务</small></div>
        <div class="trash-actions">
          <button type="button" class="secondary-action restore-application">恢复</button>
          <button type="button" class="secondary-action danger-link permanent-delete">永久删除</button>
        </div>`;
      row.querySelector(".restore-application").addEventListener("click", (event) =>
        restoreApplication(application.application_key, event.currentTarget)
      );
      row.querySelector(".permanent-delete").addEventListener("click", () =>
        showApplicationAction(application, "permanent")
      );
      body.append(row);
    });
    recycle.append(body);
    cards.append(recycle);
  }
}

function render() {
  renderCounts();
  renderCards();
  renderHealth();
  renderCapsule();
}

function inputDate(value) {
  return value ? value.slice(0, 16) : "";
}

function renderApplicationChoices(choices, company = "") {
  state.applicationChoices = choices;
  const companyList = document.querySelector("#applicationCompanyChoices");
  const roleList = document.querySelector("#applicationRoleChoices");
  companyList.replaceChildren(
    ...[...new Set(choices.flatMap((item) =>
      [item.company, item.company_suggestion].filter(Boolean)
    ))].map((value) => {
      const option = document.createElement("option");
      option.value = value;
      return option;
    }),
  );
  const normalized = company.trim().toLocaleLowerCase();
  const matching = choices.filter((item) =>
    !normalized ||
    item.same_company ||
    String(item.company).trim().toLocaleLowerCase() === normalized
  );
  roleList.replaceChildren(
    ...[...new Set(matching.map((item) => item.role).filter(Boolean))].map((value) => {
      const option = document.createElement("option");
      option.value = value;
      return option;
    }),
  );
}

async function loadApplicationChoices(company = "") {
  const choices = await window.pywebview.api.list_application_choices(company);
  renderApplicationChoices(choices, company);
}

async function showApplicationDialog(applicationKey = "", cardFallback = null) {
  if (!apiReady()) return;
  const materializing = !applicationKey && Boolean(cardFallback);
  const creating = !applicationKey && !materializing;
  const detail = creating
    ? {
        application_key: "",
        company: "",
        role: "",
        location: "",
        recruiting_project: "",
        job_code: "",
        status: "active",
        manual_stage: "网申",
        manual_stage_status: "pending",
        next_stage: nextStageAfter("网申"),
        manual_notes: "",
        revision: null,
        timeline: [],
      }
    : materializing
      ? {
          application_key: "",
          company: cardFallback.company || "",
          role: cardFallback.role || "",
          location: cardFallback.location || "",
          recruiting_project: cardFallback.recruiting_project || cardFallback.project || "",
          job_code: cardFallback.job_code || "",
          status: cardFallback.active === false ? "ended" : "active",
          manual_stage: cardFallback.manual_stage || cardFallback.current_stage || "",
          manual_stage_status: cardFallback.manual_stage_status ||
            (cardFallback.current_status === "done" ? "completed" : "pending"),
          next_stage: cardFallback.next_stage || "",
          manual_notes: cardFallback.manual_notes || "",
          revision: cardFallback.revision ?? null,
          timeline: cardFallback.history || [],
        }
      : await window.pywebview.api.get_application_detail(applicationKey);
  state.applicationMode = creating ? "create" : (materializing ? "materialize" : "edit");
  state.materializingApplicationId = materializing
    ? (cardFallback.legacy_application_id || cardFallback.application_id || "")
    : "";
  applicationForm.reset();
  applicationForm.elements.expected_revision.value = detail.revision ?? "";
  for (const name of [
    "application_key",
    "company",
    "role",
    "location",
    "recruiting_project",
    "job_code",
    "status",
    "manual_stage_status",
    "next_stage",
    "manual_notes",
  ]) {
    if (applicationForm.elements[name]) {
      applicationForm.elements[name].value = detail[name] ?? "";
    }
  }
  const latestTask = (detail.timeline || [])[0];
  const currentStage = stageOption(detail.manual_stage || latestTask?.stage) ||
    (creating ? "网申" : "");
  ensureSelectValue(applicationForm.elements.manual_stage, currentStage);
  ensureSelectValue(applicationForm.elements.manual_stage_status,
    detail.manual_stage_status ||
    (latestTask?.status === "done" ? "completed" : "pending"));
  ensureSelectValue(
    applicationForm.elements.next_stage,
    creating ? (detail.next_stage || nextStageAfter(currentStage)) : (detail.next_stage ?? ""),
  );
  document.querySelector("#applicationDialogTitle").textContent =
    creating ? "新建申请" : "申请详情";
  document.querySelector("#applicationSubmit").textContent =
    creating ? "创建申请" : "保存申请";
  const timeline = document.querySelector("#applicationTimeline");
  timeline.replaceChildren(
    ...(detail.timeline || []).map((item) => {
      const row = document.createElement("div");
      row.className = "application-timeline-row";
      const time = item.event_at
        ? new Date(item.event_at).toLocaleString("zh-CN", { hour12: false })
        : "时间待确认";
      const label = document.createElement("span");
      label.textContent = `${item.time_inferred ? "约 " : ""}${time}｜${item.stage}${item.round ? ` · ${item.round}` : ""}｜${item.status_label}`;
      row.append(label);
      appendTimelineSources(row, item);
      return row;
    }),
  );
  await loadApplicationChoices(detail.company || "");
  document.querySelector("#applicationError").textContent = "";
  showManagedDialog(applicationDialog, applicationForm.elements.company);
}

async function saveApplication(event) {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(applicationForm).entries());
  data.expected_revision = data.expected_revision
    ? Number(data.expected_revision)
    : null;
  state.saving = true;
  setFormWriting(applicationForm, true);
  try {
    const payload = state.applicationMode === "create"
      ? await window.pywebview.api.create_application(data)
      : state.applicationMode === "materialize"
        ? await window.pywebview.api.materialize_progress_application({
            ...data,
            legacy_application_id: state.materializingApplicationId,
          })
        : await window.pywebview.api.edit_application(data.application_key, data);
    applicationDialog.close();
    applyMutationPayload(payload);
  } catch (error) {
    document.querySelector("#applicationError").textContent =
      error?.message || String(error);
  } finally {
    state.saving = false;
    setFormWriting(applicationForm, false);
  }
}

async function loadTaskApplicationChoices(preferredKey = "", company = "") {
  const choices = await window.pywebview.api.list_application_choices(company, "");
  state.applicationChoices = choices;
  const select = taskForm.elements.application_key;
  select.replaceChildren(
    new Option("不关联申请链", ""),
    ...choices.map((item) =>
      new Option(applicationIdentity(item), item.application_key)
    ),
  );
  select.value = choices.some((item) => item.application_key === preferredKey)
    ? preferredKey
    : "";
}

function applyTaskApplicationChoice() {
  const key = taskForm.elements.application_key.value;
  const choice = state.applicationChoices.find((item) => item.application_key === key);
  if (!choice) return;
  taskForm.elements.company.value = choice.company || "";
  taskForm.elements.role.value =
    choice.role === "岗位待确认" ? "" : choice.role || "";
  taskForm.elements.location.value = choice.location || "";
}

async function showTaskDialog(task = null, focusTime = false, application = null) {
  taskForm.reset();
  document.querySelector("#formError").textContent = "";
  document.querySelector("#dialogTitle").textContent = task
    ? "编辑待办"
    : "新建待办";
  taskForm.elements.task_id.value = task?.id || "";
  taskForm.elements.expected_revision.value = task?.revision || "";
  const deleteButton = document.querySelector("#deleteTask");
  deleteButton.hidden = !task;
  deleteButton.dataset.taskId = task?.id || "";
  const context = application || task || {};
  taskForm.elements.company.value = context.company || "";
  taskForm.elements.role.value =
    context.role === "岗位待确认" ? "" : context.role || "";
  taskForm.elements.location.value = context.location || "";
  taskForm.elements.stage.value = task?.stage ||
    applicationStage(application || {}) || "自定义待办";
  taskForm.elements.round.value = task?.round || "";
  // One moment to be ready by, taken from whichever legacy field carried it.
  // The payload is UTC, so it must be rendered as local wall-clock or the
  // editor would disagree with the card that opened it.
  writeClockFields(
    taskForm,
    localInputValue(
      task?.deadline_at || task?.time || task?.start_at || task?.end_at,
    ),
  );
  taskForm.elements.action_summary.value = task?.action || "";
  taskForm.elements.manual_notes.value = task?.manual_notes || "";
  await loadTaskApplicationChoices(
    context.application_key || "",
    context.company || "",
  );
  showManagedDialog(
    taskDialog,
    focusTime ? taskForm.elements.deadline_date : taskForm.elements.company,
  );
}

function formPayload() {
  const payload = Object.fromEntries(
    [
      "company",
      "application_key",
      "role",
      "location",
      "stage",
      "round",
      "action_summary",
      "manual_notes",
      "expected_revision",
    ].map((name) => [name, taskForm.elements[name].value.trim()]),
  );
  // The editor now keeps a single moment; the legacy window fields are
  // cleared so an old start/end pair cannot outrank what was just entered.
  payload.deadline_at = deadlineFieldValue().iso || "";
  payload.start_at = "";
  payload.end_at = "";
  return payload;
}

// Hour and minute are edited separately, so each box validates on its own.
function clockPart(value, limit) {
  const digits = String(value || "").replace(/[^\d]/g, "");
  if (!digits) return "";
  const number = Number(digits);
  if (!Number.isFinite(number) || number < 0 || number > limit) return null;
  return String(number).padStart(2, "0");
}

function readClockFields(form) {
  const hour = clockPart(form.elements.deadline_hour.value, 23);
  const minute = clockPart(form.elements.deadline_minute.value, 59);
  if (hour === null || minute === null) return null;
  if (!hour && !minute) return "";
  return `${hour || "00"}:${minute || "00"}`;
}

function writeClockFields(form, localValue) {
  form.elements.deadline_date.value = localValue.slice(0, 10);
  form.elements.deadline_hour.value = localValue.slice(11, 13);
  form.elements.deadline_minute.value = localValue.slice(14, 16);
}

function deadlineFieldValue(form = taskForm) {
  const date = form.elements.deadline_date.value;
  const time = readClockFields(form);
  if (time === null) return { iso: null, time: null };
  if (!date) return { iso: "", time };
  return { iso: `${date}T${time || "09:00"}`, time };
}

function calendarDraftDate() {
  const { iso } = deadlineFieldValue();
  return iso ? new Date(iso) : null;
}

async function saveTask(event) {
  event.preventDefault();
  if (!apiReady() || state.saving) return;
  const errorNode = document.querySelector("#formError");
  if (!taskForm.elements.company.value.trim()) {
    errorNode.textContent = "请填写公司或事项名称。";
    taskForm.elements.company.focus();
    return;
  }
  const deadline = deadlineFieldValue();
  if (deadline.iso === null) {
    errorNode.textContent = "时间请按 24 小时制填写：小时 0–23，分钟 0–59。";
    taskForm.elements.deadline_hour.focus();
    return;
  }
  if (deadline.time) {
    const [hour, minute] = deadline.time.split(":");
    taskForm.elements.deadline_hour.value = hour;
    taskForm.elements.deadline_minute.value = minute;
  }
  const taskId = taskForm.elements.task_id.value;
  const draftDate = calendarDraftDate();
  state.saving = true;
  setFormWriting(taskForm, true);
  try {
    const payload = taskId
      ? await window.pywebview.api.edit_task(taskId, formPayload())
      : await window.pywebview.api.create_task(formPayload());
    taskDialog.close();
    if (draftDate && Number.isFinite(draftDate.getTime())) {
      state.calendarAnchor = new Date(draftDate);
      state.selectedDate = new Date(draftDate);
      state.calendarInitialized = true;
    }
    applyMutationPayload(payload);
  } catch (error) {
    errorNode.textContent = error?.message || String(error);
  } finally {
    state.saving = false;
    setFormWriting(taskForm, false);
  }
}

async function showSettingsDialog(firstRun = false, payload = null) {
  if (!apiReady()) return;
  const generation = ++state.settingsGeneration;
  const settings = payload || await window.pywebview.api.get_app_settings();
  if (generation !== state.settingsGeneration) return;
  state.settingsFirstRun = firstRun || !settings.credential_configured;
  document.querySelector("#settingsTitle").textContent = state.settingsFirstRun
    ? "首次设置"
    : "设置";
  document.querySelector("#firstRunHint").classList.toggle(
    "hidden",
    !state.settingsFirstRun,
  );
  document.querySelector("#closeSettings").hidden = false;
  document.querySelector("#cancelSettings").hidden = false;
  document.querySelector("#cancelSettings").textContent =
    state.settingsFirstRun ? "稍后配置" : "取消";
  document.querySelector("#settingsStatus").textContent = settings.privacy_reset_completed
    ? "个人信息已清除。请重新配置邮箱；外部导出文档和备份需另行清理。" : "";
  document.querySelector("#privacyResetSection").classList.toggle("hidden", !settings.privacy_reset_supported);
  document.querySelector("#privacyResetDirectory").textContent =
    `本机应用数据目录：${settings.local_data_directory || ""}`;
  document.querySelector("#storageLocationPath").textContent = settings.local_data_directory || "";
  document.querySelector("#chooseStorageLocation").disabled = !settings.storage_change_supported;
  document.querySelector("#storageLocationHint").textContent = settings.storage_move_completed
    ? "数据迁移已完成，内部路径已自动更新。"
    : settings.storage_change_supported
      ? "更换位置时会迁移已保存的数据和设置，并重新打开程序。"
      : "当前使用隔离测试目录或此平台暂不支持更换位置。";
  settingsForm.elements.email.value = settings.email || "";
  settingsForm.elements.authorization_code.value = "";
  const configuredProvider = settings.mail_provider || settings.provider || "custom";
  settingsForm.elements.mail_provider.value =
    MAIL_PROVIDER_PRESETS[configuredProvider] ? configuredProvider : "custom";
  settingsForm.elements.mail_host.value = settings.mail_host || "";
  settingsForm.elements.mail_port.value = settings.mail_port || 993;
  settingsForm.elements.mail_ssl.checked =
    settings.mail_ssl === undefined
      ? settings.ssl !== false
      : Boolean(settings.mail_ssl);
  settingsForm.elements.poll_minutes.value = settings.poll_minutes || 10;
  settingsForm.elements.lookback_days.value = settings.lookback_days || 3;
  settingsForm.elements.include_onsite_sessions.checked = Boolean(settings.include_onsite_sessions);
  settingsForm.elements.github_updates_enabled.checked = Boolean(settings.github_updates_enabled);
  settingsForm.elements.update_channel.value = settings.update_channel || "preview";
  document.querySelector("#githubUpdateSection").classList.toggle("hidden", !settings.github_update_supported);
  if (settings.github_update_supported) refreshGithubUpdate();
  settingsForm.elements.obsidian_enabled.checked = Boolean(settings.obsidian_enabled);
  settingsForm.elements.obsidian_output.value = settings.obsidian_output || "";
  settingsForm.elements.progress_enabled.checked = Boolean(settings.progress_enabled);
  settingsForm.elements.progress_output.value = settings.progress_output || "";
  settingsForm.elements.progress_source.value = settings.progress_source || "";
  settingsForm.elements.reminders_enabled.checked = Boolean(settings.reminders_enabled);
  settingsForm.elements.notify_new_mail.checked = settings.notify_new_mail !== false;
  settingsForm.elements.taskbar_button.checked = settings.taskbar_button !== false;
  document.querySelector("#taskbarButtonRow").hidden = !settings.taskbar_button_supported;
  document.querySelector("#closeHidesHint").classList.toggle(
    "hidden",
    !settings.close_hides_to_tray,
  );
  settingsForm.elements.reminder_offsets_minutes.value =
    (settings.reminder_offsets_minutes || [1440, 120, 30]).join(", ");
  settingsForm.elements.calendar_sync_enabled.checked =
    Boolean(settings.calendar_sync_enabled);
  settingsForm.elements.calendar_name.value = settings.calendar_name || "JobMailDesk";
  renderCalendarBackend(settings);
  settingsForm.elements.ui_font_scale.value = settings.ui_font_scale || 108;
  state.persistedFontScale = Number(settingsForm.elements.ui_font_scale.value);
  applyFontScale(settingsForm.elements.ui_font_scale.value);
  settingsForm.elements.dictionary_workbook.value = "";
  showManagedDialog(settingsDialog, settingsForm.elements.email);
  const [dictionaryResult, learningResult, calendarResult] =
    await Promise.allSettled([
      window.pywebview.api.get_dictionary_status(),
      window.pywebview.api.list_identity_learning_rules(),
      window.pywebview.api.get_calendar_status(),
    ]);
  if (generation !== state.settingsGeneration || !settingsDialog.open) return;
  if (dictionaryResult.status === "fulfilled") {
    renderDictionaryStatus(dictionaryResult.value);
  }
  if (learningResult.status === "fulfilled") {
    renderIdentityLearningRules(learningResult.value);
  }
  document.querySelector("#calendarStatus").textContent =
    calendarResult.status === "fulfilled"
      ? `系统日历状态：${calendarResult.value.status}`
      : "系统日历状态暂时不可用";
  const failed = [dictionaryResult, learningResult, calendarResult]
    .filter((result) => result.status === "rejected").length;
  if (failed) {
    document.querySelector("#settingsStatus").textContent =
      `${failed} 项辅助状态读取失败；仍可修改并保存设置。`;
  }
}

window.openSettingsDialog = showSettingsDialog;

function settingsPayload() {
  return {
    email: settingsForm.elements.email.value.trim(),
    authorization_code: settingsForm.elements.authorization_code.value.trim(),
    mail_provider: settingsForm.elements.mail_provider.value,
    provider: settingsForm.elements.mail_provider.value,
    mail_host: settingsForm.elements.mail_host.value.trim(),
    mail_port: Number(settingsForm.elements.mail_port.value),
    mail_ssl: settingsForm.elements.mail_ssl.checked,
    ssl: settingsForm.elements.mail_ssl.checked,
    poll_minutes: Number(settingsForm.elements.poll_minutes.value),
    lookback_days: Number(settingsForm.elements.lookback_days.value),
    include_onsite_sessions: settingsForm.elements.include_onsite_sessions.checked,
    github_updates_enabled: settingsForm.elements.github_updates_enabled.checked,
    update_channel: settingsForm.elements.update_channel.value,
    obsidian_enabled: settingsForm.elements.obsidian_enabled.checked,
    obsidian_output: settingsForm.elements.obsidian_output.value.trim(),
    progress_enabled: settingsForm.elements.progress_enabled.checked,
    progress_output: settingsForm.elements.progress_output.value.trim(),
    progress_source: settingsForm.elements.progress_source.value.trim(),
    reminders_enabled: settingsForm.elements.reminders_enabled.checked,
    notify_new_mail: settingsForm.elements.notify_new_mail.checked,
    taskbar_button: settingsForm.elements.taskbar_button.checked,
    reminder_offsets_minutes:
      settingsForm.elements.reminder_offsets_minutes.value.trim(),
    calendar_sync_enabled: settingsForm.elements.calendar_sync_enabled.checked,
    calendar_name: settingsForm.elements.calendar_name.value.trim(),
    ui_font_scale: Number(settingsForm.elements.ui_font_scale.value),
  };
}

function applyFontScale(value) {
  const scale = Math.max(90, Math.min(125, Number(value) || 108));
  document.documentElement.style.setProperty("--font-scale", String(scale / 100));
  document.querySelector("#fontScaleValue").textContent = `${scale}%`;
}

function renderCalendarBackend(settings) {
  // macOS writes straight into Calendar.app; Windows exports one ICS file
  // that Outlook / Windows Calendar opens or subscribes to.
  const hint = document.querySelector("#calendarBackendHint");
  const openButton = document.querySelector("#openCalendarExportFolder");
  const usesIcsFile = settings.calendar_backend === "ics_file";
  hint.classList.toggle("hidden", !usesIcsFile);
  openButton.classList.toggle("hidden", !usesIcsFile);
  hint.textContent = usesIcsFile
    ? `Windows 上会把活跃待办写入日历文件 ${settings.calendar_export_path || ""}；` +
      "在 Outlook 或 Windows 日历中打开或订阅该文件即可看到日程。"
    : "";
}

async function openCalendarExportFolder() {
  if (!apiReady()) return;
  try {
    await window.pywebview.api.open_calendar_export_folder();
  } catch (error) {
    document.querySelector("#calendarStatus").textContent = error?.message || String(error);
  }
}

async function syncCalendarNow() {
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  const generation = state.settingsGeneration;
  const status = document.querySelector("#calendarStatus");
  setFormWriting(settingsForm, true);
  status.textContent = "正在同步系统日历…";
  try {
    const result = await window.pywebview.api.sync_calendar_now(
      settingsForm.elements.calendar_name.value.trim(),
    );
    if (generation !== state.settingsGeneration) return;
    status.textContent =
      result.status === "ok"
        ? `同步完成：${result.synced} 条日程${result.detail ? `，文件：${result.detail}` : ""}`
        : `同步失败：${result.detail || result.status}`;
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    status.textContent = error?.message || String(error);
  } finally {
    setFormWriting(settingsForm, false);
  }
}

function renderDictionaryStatus(payload) {
  const counts = payload.counts || {};
  const badge = document.querySelector("#dictionaryBadge");
  const status = document.querySelector("#dictionaryStatus");
  badge.textContent = `${counts.companies || 0} 家企业`;
  const source = payload.user_dictionary_enabled
    ? `个人词典已启用${payload.source_filename ? ` · ${payload.source_filename}` : ""}`
    : "当前使用内置基础词典";
  status.textContent = `${source} · ${counts.programs || 0} 个项目 · ${counts.roles || 0} 个岗位`;
}

function renderIdentityLearningRules(rules) {
  const container = document.querySelector("#identityLearningRules");
  container.replaceChildren();
  if (!rules.length) {
    container.textContent = "暂无已学习规则";
    return;
  }
  rules.forEach((rule) => {
    const row = document.createElement("div");
    row.className = "identity-learning-rule";
    const description = document.createElement("span");
    const company = rule.corrected_company
      ? `${rule.original_company || "待确认"} → ${rule.corrected_company}`
      : "";
    const role = rule.corrected_role
      ? `${rule.original_role || "待确认"} → ${rule.corrected_role}`
      : "";
    description.textContent = [company, role].filter(Boolean).join(" · ");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.learningRuleId = rule.id;
    button.dataset.learningRuleEnabled = String(Boolean(rule.enabled));
    button.disabled = Boolean(rule.conflict);
    button.textContent = rule.conflict
      ? "规则冲突"
      : rule.enabled
        ? "停用"
        : "启用";
    row.append(description, button);
    container.append(row);
  });
}

async function toggleIdentityLearningRule(event) {
  const button = event.target.closest("[data-learning-rule-id]");
  if (!button || !apiReady() || formWriteSnapshots.has(settingsForm)) return;
  const generation = state.settingsGeneration;
  setFormWriting(settingsForm, true);
  try {
    const rules =
      await window.pywebview.api.set_identity_learning_rule_enabled(
        button.dataset.learningRuleId,
        button.dataset.learningRuleEnabled !== "true",
      );
    if (generation !== state.settingsGeneration) return;
    renderIdentityLearningRules(rules);
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    document.querySelector("#dictionaryStatus").textContent =
      error?.message || String(error);
  } finally {
    setFormWriting(settingsForm, false);
  }
}

async function rebuildIdentityLearning() {
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  const generation = state.settingsGeneration;
  const button = document.querySelector("#rebuildIdentityLearning");
  const status = document.querySelector("#dictionaryStatus");
  setFormWriting(settingsForm, true);
  status.classList.remove("error", "success");
  status.textContent = "正在只读扫描最近 30 天邮件并核对已确认申请…";
  try {
    const result = await window.pywebview.api.rebuild_identity_learning(30);
    if (generation !== state.settingsGeneration) return;
    renderIdentityLearningRules(result.rules || []);
    const summary = result.summary || {};
    status.textContent =
      `学习完成：核对 ${summary.eligible || 0} 封，新增 ${summary.learned || 0} 条` +
      (summary.conflicts ? `，发现 ${summary.conflicts} 条冲突` : "");
    status.classList.add("success");
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    status.textContent = error?.message || String(error);
    status.classList.add("error");
  } finally {
    setFormWriting(settingsForm, false);
  }
}

async function selectDictionaryWorkbook() {
  if (!apiReady()) return;
  const generation = state.settingsGeneration;
  const selected = await window.pywebview.api.select_dictionary_workbook();
  if (generation === state.settingsGeneration && selected) {
    settingsForm.elements.dictionary_workbook.value = selected;
  }
}

async function compileDictionaryWorkbook() {
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  const generation = state.settingsGeneration;
  const source = settingsForm.elements.dictionary_workbook.value.trim();
  const sheet = settingsForm.elements.dictionary_sheet.value.trim();
  const status = document.querySelector("#dictionaryStatus");
  if (!source) {
    status.textContent = "请先选择一个 .xlsx 秋招表。";
    status.classList.add("error");
    return;
  }
  status.classList.remove("error", "success");
  status.textContent = "正在本机编译词典…";
  setFormWriting(settingsForm, true);
  try {
    const result = await window.pywebview.api.compile_dictionary_workbook(source, sheet);
    if (generation !== state.settingsGeneration) return;
    renderDictionaryStatus({
      counts: result.counts,
      user_dictionary_enabled: true,
      source_filename: source.split(/[\\/]/).pop(),
    });
    status.classList.add("success");
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    status.textContent = error?.message || String(error);
    status.classList.add("error");
  } finally {
    setFormWriting(settingsForm, false);
  }
}

async function saveAppSettings(event) {
  event.preventDefault();
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  document.querySelector("#settingsStatus").textContent = "正在保存…";
  setFormWriting(settingsForm, true);
  try {
    const payload = settingsPayload();
    const saved = await window.pywebview.api.save_app_settings(payload);
    state.persistedFontScale = Number(saved.ui_font_scale || payload.ui_font_scale);
    applyFontScale(state.persistedFontScale);
    state.settingsFirstRun = false;
    settingsDialog.close();
    await refresh({ reason: "settings" });
  } catch (error) {
    document.querySelector("#settingsStatus").textContent =
      error?.message || String(error);
  } finally {
    setFormWriting(settingsForm, false);
  }
}

async function pickSettingsPath(kind) {
  if (!apiReady()) return;
  const generation = state.settingsGeneration;
  const selected = await window.pywebview.api.select_markdown_path(kind);
  if (generation === state.settingsGeneration && selected) {
    settingsForm.elements[kind].value = selected;
  }
}

async function createProgressTemplateFromSettings() {
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  const generation = state.settingsGeneration;
  let path = settingsForm.elements.progress_source.value.trim();
  if (!path) {
    path = await window.pywebview.api.select_markdown_path("progress_source");
    if (!path) return;
    settingsForm.elements.progress_source.value = path;
  }
  setFormWriting(settingsForm, true);
  try {
    const result = await window.pywebview.api.create_progress_source_template(path);
    if (generation !== state.settingsGeneration) return;
    document.querySelector("#settingsStatus").textContent = result.created
      ? `模板已创建：${result.path}`
      : "该文件已有内容，未覆盖。";
    settingsForm.elements.progress_enabled.checked = true;
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    document.querySelector("#settingsStatus").textContent =
      error?.message || String(error);
  } finally {
    setFormWriting(settingsForm, false);
  }
}

async function testMailSettings() {
  if (!apiReady()) return;
  const generation = state.settingsGeneration;
  const button = document.querySelector("#testMailSettings");
  if (button.disabled) return;
  const status = document.querySelector("#settingsStatus");
  button.disabled = true;
  status.textContent = "正在测试只读连接…";
  try {
    const result = await window.pywebview.api.test_mail_settings(settingsPayload());
    if (generation !== state.settingsGeneration) return;
    status.textContent = result.detail;
    status.classList.toggle("success", Boolean(result.ok));
  } catch (error) {
    if (generation !== state.settingsGeneration) return;
    status.textContent = error?.message || String(error);
    status.classList.remove("success");
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function setCapsule(compact) {
  if (!apiReady()) return;
  const generation = ++state.capsuleGeneration;
  const previous = state.compact;
  state.compact = compact;
  document.body.classList.toggle("capsule", compact);
  try {
    const applied = await window.pywebview.api.set_capsule(compact);
    if (generation !== state.capsuleGeneration) return;
    if (!applied) {
      state.compact = previous;
      document.body.classList.toggle("capsule", previous);
    }
  } catch (error) {
    if (generation !== state.capsuleGeneration) return;
    state.compact = previous;
    document.body.classList.toggle("capsule", previous);
    healthText.textContent = error?.message || "窗口模式切换失败";
    healthDot.className = "health-dot error";
  }
}

window.setBackgroundMode = function setBackgroundMode(hidden) {
  // Called from Python when the window is hidden to the tray or shown again;
  // a hidden window does no periodic work so the app stays cheap in the
  // background, and a fresh payload is pulled the moment it becomes visible.
  const wasHidden = Boolean(state.backgroundMode);
  state.backgroundMode = Boolean(hidden);
  if (wasHidden && !state.backgroundMode) refresh({ reason: "visible" });
  return true;
};
// The window can also be revealed without Python noticing (a second instance
// calls ShowWindow, the user restores it from the taskbar): any focus or
// visibility gain ends background mode.
window.addEventListener("focus", () => window.setBackgroundMode(false));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") window.setBackgroundMode(false);
});

async function refresh({ reason = "periodic" } = {}) {
  if (!apiReady()) return false;
  if (state.backgroundMode && reason === "periodic") return false;
  const blocked = refreshBlockReason();
  if (blocked) {
    showDeferredRefresh(blocked);
    return false;
  }
  const requestSeq = ++state.dashboardRequestSeq;
  const mutationVersion = state.mutationVersion;
  const scroll = captureScrollState();
  try {
    const nextPayload = await window.pywebview.api.get_dashboard();
    if (
      requestSeq !== state.dashboardRequestSeq ||
      mutationVersion !== state.mutationVersion
    ) return false;
    const responseBlock = refreshBlockReason();
    if (responseBlock) {
      state.pendingDashboardPayload = nextPayload;
      showDeferredRefresh(responseBlock);
      return false;
    }
    state.pendingDashboardPayload = null;
    installDashboardPayload(nextPayload, scroll);
    return true;
  } catch (error) {
    healthText.textContent = "本地快照读取失败，稍后自动重试";
    healthDot.className = "health-dot error";
    console.error("dashboard refresh failed", error);
    if (reason !== "periodic") clearDeferredRefresh();
    return false;
  }
}

async function reviewWindowCompleted(_sourceHash) {
  // The confirm button of the card that opened the review window still holds
  // focus; it must not count as "a control in use" and block this refresh.
  const active = document.activeElement;
  if (active instanceof HTMLElement && active !== cards && cards.contains(active)) {
    active.blur();
  }
  await refresh({ reason: "review-window" });
  if (state.view !== "review") return;
  requestAnimationFrame(() => {
    const nextReview = cards.querySelector(
      "[data-review-source] [data-unresolved-assign]",
    );
    nextReview?.focus();
  });
}

window.reviewWindowCompleted = reviewWindowCompleted;

async function initializeApp() {
  if (state.setupInitialized || !apiReady()) return;
  state.setupInitialized = true;
  pollScanProgress();
  await refresh();
  const settings = await window.pywebview.api.get_app_settings();
  state.persistedFontScale = settings.ui_font_scale || 108;
  if (settings.github_update_supported) {
    await window.pywebview.api.check_github_update(settings.update_channel || "preview", true);
    refreshGithubUpdate();
  }
  applyFontScale(state.persistedFontScale);
  if (!settings.credential_configured) await showSettingsDialog(true, settings);
}

function initializeCalendarAnchor() {
  if (state.calendarInitialized) return;
  const now = new Date();
  const next = state.payload.tasks
    .filter((task) =>
      task.actionable && task.time && new Date(task.time) >= now
    )
    .sort((a, b) => new Date(a.time) - new Date(b.time))[0];
  if (next) {
    state.calendarAnchor = new Date(next.time);
    state.selectedDate = new Date(next.time);
  }
  state.calendarInitialized = true;
}

async function activateView(view, { userInitiated = true } = {}) {
  const tab = document.querySelector(`.tab[data-view="${view}"]`);
  if (!tab) return;
  if (view !== state.view && state.dirtyProgressCards.size) {
    showDeferredRefresh("请先保存当前进展修改，再切换页签");
    focusFirstDirtyProgressCard();
    return;
  }
  const navigationGeneration = ++state.navigationGeneration;
  state.unreadAckGeneration += 1;
  document.querySelector(".tab.active")?.classList.remove("active");
  document.querySelectorAll(".tab[data-view]").forEach((item) => {
    item.setAttribute("aria-selected", String(item === tab));
    item.tabIndex = item === tab ? 0 : -1;
  });
  tab.classList.add("active");
  state.view = view;
  renderCards();
  if (
    userInitiated &&
    view !== "progress" &&
    Number(state.payload.unread?.counts?.[view] || 0) > 0 &&
    apiReady()
  ) {
    const snapshotSequence = state.payload.unread?.snapshot_sequence || 0;
    const ackGeneration = state.unreadAckGeneration;
    try {
      const unread = await window.pywebview.api.acknowledge_tab_updates(
        view,
        snapshotSequence,
      );
      if (
        navigationGeneration !== state.navigationGeneration ||
        state.view !== view
      ) return;
      acceptUnreadPayload(unread, ackGeneration);
    } catch (error) {
      healthText.textContent = error?.message || "未读状态更新失败";
      healthDot.className = "health-dot error";
    }
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateView(tab.dataset.view));
  tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...document.querySelectorAll(".tab[data-view]")];
    const current = tabs.indexOf(event.currentTarget);
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    tabs[nextIndex].focus();
    activateView(tabs[nextIndex].dataset.view);
  });
});

let scanRequested = false;
let scanStartRunId = 0;
let scanSnapshot = {revision: -1, run_id: 0, stage: "idle", running: false};
let scanFinishedAt = 0;

function updateScanButtons() {
  const busy = scanRequested || scanSnapshot.running;
  for (const [selector, idle] of [["#scanButton", "↻"], ["#refreshButton", "扫描邮箱"]]) {
    const button = document.querySelector(selector);
    button.disabled = busy;
    button.textContent = busy ? (selector === "#scanButton" ? "…" : "扫描中…") : idle;
    button.setAttribute("aria-busy", String(busy));
  }
  document.querySelector("#startHistoryScan").disabled = busy;
}

function showScanProgress(snapshot) {
  const pendingStart = scanRequested && !snapshot.running && snapshot.run_id <= scanStartRunId;
  const stage = pendingStart ? "preparing" : snapshot.stage;
  const labels = {
    preparing: "准备扫描…", connecting: "连接邮箱…", searching: "搜索邮件…",
    reading: "读取邮件", preparing_records: "核对本地记录…", parsing: "识别邮件",
    saving: "保存识别结果…", exporting: "刷新卡片和导出文件…",
    done: "扫描完成", partial: "扫描完成，部分邮件读取或解析失败，可稍后重试",
    error: "扫描失败，请检查邮箱设置或网络后重试", cancelled: "扫描已取消",
  };
  const counted = ["reading", "parsing"].includes(stage) && snapshot.total !== null;
  const text = (labels[stage] || "") +
    (counted ? ` ${snapshot.completed}/${snapshot.total}` : "") +
    (snapshot.running && snapshot.lookback_days ? ` · 近 ${snapshot.lookback_days} 天` : "");
  const visible = pendingStart || snapshot.running ||
    (stage !== "idle" && Date.now() - scanFinishedAt < 8000);
  document.querySelectorAll("[data-scan-progress]").forEach((panel) => {
    panel.hidden = !visible;
    const label = panel.querySelector("[data-scan-label]");
    if (label.textContent !== text) label.textContent = text;
    const bar = panel.querySelector("[data-scan-bar]");
    bar.hidden = !snapshot.running && !pendingStart;
    if (counted && snapshot.total > 0) {
      bar.max = snapshot.total;
      bar.value = snapshot.completed;
    } else {
      bar.removeAttribute("value");
    }
  });
}

async function readScanProgress() {
  if (!apiReady()) return;
  try {
    const next = await window.pywebview.api.get_scan_progress();
    if (next.revision < scanSnapshot.revision) return;
    const finished = !next.running && next.stage !== "idle" &&
      (next.run_id !== scanSnapshot.run_id || scanSnapshot.running);
    if (finished) scanFinishedAt = Date.now();
    scanSnapshot = next;
    showScanProgress(next);
    updateScanButtons();
    if (finished && !scanRequested) await refresh({reason: "scan-complete"});
  } catch (_error) {
    // A transient bridge failure must not stop the next poll or change scan state.
  }
}

async function pollScanProgress() {
  try {
    if (!state.backgroundMode) await readScanProgress();
  } finally {
    // Separate from dashboard refresh: dialogs and pending edits may defer that refresh.
    setTimeout(pollScanProgress, 600);
  }
}

async function scanMailbox(button, idleText, days = null) {
  if (!apiReady() || button.disabled) return;
  const navigationGeneration = state.navigationGeneration;
  scanStartRunId = scanSnapshot.run_id;
  scanRequested = true;
  document.querySelector("#scanResult").hidden = true;
  updateScanButtons();
  showScanProgress({stage: "preparing", running: true});
  try {
    const result = days === null
      ? await window.pywebview.api.trigger_scan()
      : await window.pywebview.api.trigger_scan(days);
    if (result?.status === "busy") {
      healthText.textContent = "邮箱扫描正在进行，请稍候";
      return;
    }
    if (result?.status === "stopping") return;
    await refresh({ reason: "scan" });
    // Scan completion only updates the current view. A tab chosen while the
    // scan was running must remain selected.
    const navigationWasChanged =
      navigationGeneration !== state.navigationGeneration;
    const summary = result?.summary || {};
    const unresolvedCount = (state.payload.unresolved || []).length;
    const changes = `；新增待处理 ${summary.reviews_created || 0} 条，更新 ${summary.reviews_updated || 0} 条` +
      `，过滤 ${summary.filtered || 0} 封，读取失败 ${summary.fetch_failed || 0} 封，识别失败 ${summary.parse_failed || 0} 封`;
    const scanResult = document.querySelector("#scanResult");
    scanResult.hidden = false;
    scanResult.open = false;
    document.querySelector("#scanResultLabel").textContent =
      `${days ? "补扫" : "扫描"}完成：新增 ${summary.reviews_created || 0} 条，更新 ${summary.reviews_updated || 0} 条` +
      ((summary.fetch_failed || summary.parse_failed) ? "（部分失败）" : "");
    document.querySelector("#scanResultDetail").textContent =
      `回看 ${summary.lookback_days || "—"} 天，读取 ${summary.fetched || 0} 封，候选 ${summary.candidates || 0} 封，去重跳过 ${summary.skipped || 0} 封` + changes + "。新增和更新均指待处理记录；重复扫描不会重复建卡。";
    if ((summary.candidates || 0) > 0) {
      healthText.textContent =
        `扫描完成：回看 ${summary.lookback_days || "—"} 天，读取 ${summary.fetched || 0} 封，识别候选 ${summary.candidates} 封，当前共 ${unresolvedCount} 封待处理` +
        changes + (navigationWasChanged ? "；已保留当前页签" : "");
      healthDot.className = "health-dot warning";
    } else {
      healthText.textContent = `扫描完成：回看 ${summary.lookback_days || "—"} 天，读取 ${summary.fetched || 0} 封，暂无新增候选` +
        changes + (navigationWasChanged ? "；已保留当前页签" : "");
      healthDot.className = "health-dot";
    }
  } catch (error) {
    healthText.textContent = error?.message || "邮箱扫描失败，请检查设置";
    healthDot.className = "health-dot error";
  } finally {
    scanRequested = false;
    await readScanProgress();
    updateScanButtons();
  }
}

async function syncLedger(button, idleText) {
  if (!apiReady() || button.disabled) return;
  button.disabled = true;
  button.textContent = "…";
  try {
    const result = await window.pywebview.api.sync_ledger();
    if (result?.dashboard) applyMutationPayload(result.dashboard);
    healthText.textContent = result.imported
      ? "已读取本地台账并刷新卡片"
      : "尚未设置手动台账，已刷新本地输出；可在设置的高级路径选项中选择台账";
  } catch (error) {
    healthText.textContent = error?.message || "台账导入失败，请检查文件路径";
  } finally {
    button.disabled = false;
    button.textContent = idleText;
  }
}

document.querySelector("#refreshButton").addEventListener("click", (event) => {
  scanMailbox(event.currentTarget, "扫描邮箱");
});
document.querySelector("#ledgerButton").addEventListener("click", (event) => {
  syncLedger(event.currentTarget, "导入台账修改");
});
document.querySelector("#addButton").addEventListener("click", () =>
  showManagedDialog(
    document.querySelector("#quickCreateDialog"),
    document.querySelector("#createApplicationChoice"),
  )
);
document.querySelector("#closeQuickCreate").addEventListener("click", () =>
  document.querySelector("#quickCreateDialog").close()
);
document.querySelector("#createApplicationChoice").addEventListener("click", () => {
  document.querySelector("#quickCreateDialog").close();
  showApplicationDialog();
});
document.querySelector("#createTaskChoice").addEventListener("click", () => {
  document.querySelector("#quickCreateDialog").close();
  showTaskDialog();
});
document.querySelector("#settingsButton").addEventListener("click", () => showSettingsDialog(false));
document.querySelector("#capsuleButton").addEventListener("click", () => setCapsule(true));
document.querySelector("#expandButton").addEventListener("click", () => setCapsule(false));
document.querySelector("#closeDialog").addEventListener("click", () => taskDialog.close());
document.querySelector("#cancelDialog").addEventListener("click", () => taskDialog.close());
document.querySelector("#deleteTask").addEventListener("click", (event) => {
  const task = state.payload.tasks.find(
    (item) => item.id === event.currentTarget.dataset.taskId,
  );
  if (task) requestAction(event.currentTarget, task, "trash");
});
// Two digits in the hour box means the minute box is what you want next.
taskForm.elements.deadline_hour.addEventListener("input", (event) => {
  if (event.target.value.replace(/[^\d]/g, "").length >= 2) {
    taskForm.elements.deadline_minute.focus();
    taskForm.elements.deadline_minute.select();
  }
});
for (const name of ["deadline_hour", "deadline_minute"]) {
  taskForm.elements[name].addEventListener("blur", (event) => {
    const limit = name === "deadline_hour" ? 23 : 59;
    const padded = clockPart(event.target.value, limit);
    if (padded) event.target.value = padded;
  });
}
taskForm.addEventListener("submit", saveTask);
taskForm.elements.application_key.addEventListener("change", applyTaskApplicationChoice);
document.querySelector("#closeOriginalMail").addEventListener("click", () =>
  originalMailDialog.close()
);
document.querySelector("#dismissOriginalMail").addEventListener("click", () =>
  originalMailDialog.close()
);
document.querySelector("#loadRemoteImages").addEventListener("click", () => {
  const taskId = originalMailDialog.dataset.taskId;
  const sourceHash = originalMailDialog.dataset.sourceHash;
  if (taskId) {
    showOriginalMail(taskId, true);
  } else if (sourceHash) {
    showOriginalMailBySource(sourceHash, true);
  }
});
originalMailDialog.addEventListener("close", clearOriginalMail);
ownershipForm.addEventListener("submit", saveOwnership);
ownershipForm.elements.mode.addEventListener("change", async () => {
  state.ownershipModeTouched = true;
  const mode = ownershipForm.elements.mode.value;
  const usesApplication = mode === "existing" || mode === "update_task";
  if (usesApplication) {
    const selected = renderOwnershipChoices(ownershipForm.elements.application_key.value);
    if (selected) applyOwnershipChoice();
  } else {
    ownershipForm.elements.application_key.selectedIndex = -1;
  }
  applyOwnershipMode();
  refreshOwnershipRecommendation();
});
ownershipForm.elements.application_key.addEventListener("change", async () => {
  applyOwnershipChoice();
  if (ownershipForm.elements.mode.value === "update_task") {
    await loadOwnershipTasks("", { force: true });
  }
  await refreshOwnershipRecommendation();
});
ownershipForm.elements.task_id.addEventListener("change", () => {
  ownershipForm.elements.expected_task_revision.value =
    ownershipForm.elements.task_id.selectedOptions[0]?.dataset.revision || "";
});
ownershipForm.elements.create_task.addEventListener("change", () => {
  const enabled = ownershipForm.elements.create_task.checked;
  const details = document.querySelector("#ownershipTaskDetails");
  details.hidden = !enabled;
  details.open = enabled;
});
for (const name of ["company", "role"]) {
  ownershipForm.elements[name].addEventListener("change", async () => {
    await loadOwnershipChoices(
      ownershipForm.elements.company.value,
      ownershipForm.elements.role.value,
    );
    await refreshOwnershipRecommendation();
  });
}
document.querySelector("#closeOwnership").addEventListener("click", () => ownershipDialog.close());
document.querySelector("#cancelOwnership").addEventListener("click", () => ownershipDialog.close());
applicationForm.addEventListener("submit", saveApplication);
applicationForm.elements.company.addEventListener("change", () =>
  loadApplicationChoices(applicationForm.elements.company.value)
);
applicationForm.elements.manual_stage.addEventListener("change", () => {
  if (state.applicationMode === "create") {
    ensureSelectValue(
      applicationForm.elements.next_stage,
      nextStageAfter(applicationForm.elements.manual_stage.value),
    );
  }
});
document.querySelector("#closeApplication").addEventListener("click", () => applicationDialog.close());
document.querySelector("#cancelApplication").addEventListener("click", () => applicationDialog.close());
applicationActionForm.addEventListener("submit", submitApplicationAction);
applicationActionForm.elements.target_query.addEventListener("input", (event) => {
  invalidateMergePreview();
  renderMergeTargets(event.currentTarget.value);
});
applicationActionForm.elements.target.addEventListener(
  "change",
  () => {
    invalidateMergePreview();
    updateCrossCompanyConfirmation();
  },
);
applicationActionForm.elements.cross_company_confirmed.addEventListener(
  "change",
  invalidateMergePreview,
);
document.querySelector("#closeApplicationAction").addEventListener("click", () =>
  applicationActionDialog.close()
);
document.querySelector("#cancelApplicationAction").addEventListener("click", () =>
  applicationActionDialog.close()
);
settingsForm.addEventListener("submit", saveAppSettings);
const storageMoveDialog = document.querySelector("#storageMoveDialog");
let storageMoveDestination = "";
let storageMovePending = false;
document.querySelector("#chooseStorageLocation").addEventListener("click", async () => {
  if (!apiReady() || formWriteSnapshots.has(settingsForm)) return;
  setFormWriting(settingsForm, true);
  try {
    const selected = await window.pywebview.api.select_storage_location();
    if (!selected) return;
    storageMoveDestination = selected;
    document.querySelector("#storageMoveTarget").textContent = `新数据目录：${selected}`;
    document.querySelector("#storageMoveStatus").textContent = "请先保存需要保留的设置；本次迁移使用已保存的设置。";
    storageMoveDialog.showModal();
  } catch (error) {
    document.querySelector("#settingsStatus").textContent = error?.message || String(error);
  } finally {
    setFormWriting(settingsForm, false);
  }
});
document.querySelector("#cancelStorageMove").addEventListener("click", () => {
  if (!storageMovePending) storageMoveDialog.close();
});
storageMoveDialog.addEventListener("cancel", (event) => {
  if (storageMovePending) event.preventDefault();
});
document.querySelector("#storageMoveForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!apiReady() || storageMovePending || !storageMoveDestination) return;
  storageMovePending = true;
  document.querySelector("#confirmStorageMove").disabled = true;
  document.querySelector("#cancelStorageMove").disabled = true;
  document.querySelector("#storageMoveStatus").textContent = "正在退出并迁移数据，请等待程序重新打开…";
  try {
    await window.pywebview.api.change_storage_location(storageMoveDestination, true);
  } catch (error) {
    storageMovePending = false;
    document.querySelector("#storageMoveStatus").textContent = error?.message || String(error);
    document.querySelector("#confirmStorageMove").disabled = false;
    document.querySelector("#cancelStorageMove").disabled = false;
  }
});
const privacyResetDialog = document.querySelector("#privacyResetDialog");
const privacyResetConfirmation = document.querySelector("#privacyResetConfirmation");
let privacyResetPending = false;
document.querySelector("#openPrivacyReset").addEventListener("click", () => {
  if (formWriteSnapshots.has(settingsForm)) return;
  privacyResetConfirmation.value = "";
  document.querySelector("#confirmPrivacyReset").disabled = true;
  document.querySelector("#privacyResetStatus").textContent = "";
  privacyResetDialog.showModal();
  privacyResetConfirmation.focus();
});
privacyResetConfirmation.addEventListener("input", () => {
  document.querySelector("#confirmPrivacyReset").disabled =
    privacyResetPending || privacyResetConfirmation.value !== "清除";
});
document.querySelector("#cancelPrivacyReset").addEventListener("click", () => {
  if (!privacyResetPending) privacyResetDialog.close();
});
privacyResetDialog.addEventListener("cancel", (event) => {
  if (privacyResetPending) event.preventDefault();
});
document.querySelector("#privacyResetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!apiReady() || privacyResetPending || privacyResetConfirmation.value !== "清除") return;
  privacyResetPending = true;
  document.querySelector("#confirmPrivacyReset").disabled = true;
  document.querySelector("#cancelPrivacyReset").disabled = true;
  privacyResetConfirmation.disabled = true;
  const status = document.querySelector("#privacyResetStatus");
  status.textContent = "正在退出并清除个人信息，请等待程序重新打开…";
  try {
    await window.pywebview.api.clear_personal_information(privacyResetConfirmation.value);
  } catch (error) {
    status.textContent = error?.message || "无法开始清除，请重试。";
    privacyResetPending = false;
    privacyResetConfirmation.disabled = false;
    document.querySelector("#cancelPrivacyReset").disabled = false;
    document.querySelector("#confirmPrivacyReset").disabled = false;
  }
});
document
  .querySelector("#identityLearningRules")
  .addEventListener("click", toggleIdentityLearningRule);
document
  .querySelector("#rebuildIdentityLearning")
  .addEventListener("click", rebuildIdentityLearning);
settingsForm.elements.ui_font_scale.addEventListener("input", (event) =>
  applyFontScale(event.currentTarget.value),
);
document.querySelector("#closeSettings").addEventListener("click", () => {
  applyFontScale(state.persistedFontScale);
  settingsDialog.close();
});
document.querySelector("#cancelSettings").addEventListener("click", () => {
  applyFontScale(state.persistedFontScale);
  settingsDialog.close();
});
settingsDialog.addEventListener("cancel", (event) => {
  if (state.settingsFirstRun) {
    event.preventDefault();
  } else {
    applyFontScale(state.persistedFontScale);
  }
});
settingsDialog.addEventListener("close", () => {
  // Settings is intentionally scrollable inside the normal card geometry.
  // Resizing the native window here caused a large intermediate flash.
});
document.querySelectorAll("[data-pick-path]").forEach((button) => {
  button.addEventListener("click", () => pickSettingsPath(button.dataset.pickPath));
});
document.querySelector("#createProgressTemplate").addEventListener(
  "click",
  createProgressTemplateFromSettings,
);
document.querySelector("#selectDictionaryWorkbook").addEventListener(
  "click",
  selectDictionaryWorkbook,
);
document.querySelector("#compileDictionaryWorkbook").addEventListener(
  "click",
  compileDictionaryWorkbook,
);
document.querySelector("#testMailSettings").addEventListener("click", testMailSettings);
document.querySelector("#syncCalendarNow").addEventListener("click", syncCalendarNow);
document
  .querySelector("#openCalendarExportFolder")
  .addEventListener("click", openCalendarExportFolder);
document.querySelector("#mailProvider").addEventListener("change", applyMailProviderPreset);
const historyScanDialog = document.querySelector("#historyScanDialog");
for (const selector of ["#historyScanButton", "#settingsHistoryScanButton"]) {
  document.querySelector(selector).addEventListener("click", () => {
    updateScanButtons();
    historyScanDialog.showModal();
  });
}
document.querySelector("#closeHistoryScan").addEventListener("click", () => historyScanDialog.close());
document.querySelector("#startHistoryScan").addEventListener("click", async (event) => {
  const days = Number(document.querySelector("#historyScanDays").value);
  historyScanDialog.close();
  await scanMailbox(event.currentTarget, "开始补扫", days);
});

const ignoredReviewsDialog = document.querySelector("#ignoredReviewsDialog");
let ignoredReviewsLoading = false;
async function loadIgnoredReviews() {
  const status = document.querySelector("#ignoredReviewsStatus");
  const list = document.querySelector("#ignoredReviewsList");
  ignoredReviewsLoading = true;
  list.replaceChildren();
  status.textContent = "正在读取…";
  try {
    const records = await window.pywebview.api.list_ignored_reviews();
    status.textContent = records.length ? `共 ${records.length} 封手动忽略的邮件` : "暂无手动忽略的邮件。";
    for (const record of records) {
      const row = document.createElement("article");
      row.className = "ignored-review-row";
      const title = document.createElement("strong");
      title.textContent = record.title || `${record.company || "公司待确认"}｜${record.role || "岗位待确认"}`;
      const meta = document.createElement("p");
      meta.textContent = `${record.company || "公司待确认"}｜${record.role || "岗位待确认"} · ${new Date(record.received_at).toLocaleDateString("zh-CN")}`;
      const restore = document.createElement("button");
      restore.type = "button";
      restore.textContent = "恢复到待处理";
      restore.addEventListener("click", async () => {
        if (ignoredReviewsLoading) return;
        ignoredReviewsLoading = true;
        restore.disabled = true;
        status.textContent = "正在恢复…";
        try {
          await window.pywebview.api.restore_ignored_review(record.id, record.revision);
        } catch (error) {
          await loadIgnoredReviews();
          status.textContent = error?.message || "恢复失败，请刷新后重试。";
          return;
        } finally {
          ignoredReviewsLoading = false;
        }
        await loadIgnoredReviews();
        status.textContent = "已恢复到待处理。请返回后确认归属。";
        await refresh({reason: "review-restored"});
      });
      row.append(title, meta, restore);
      list.append(row);
    }
  } catch (error) {
    status.textContent = error?.message || "读取失败，请关闭后重试。";
  } finally {
    ignoredReviewsLoading = false;
  }
}
document.querySelector("#ignoredReviewsButton").addEventListener("click", async () => {
  if (!apiReady()) return;
  ignoredReviewsDialog.showModal();
  await loadIgnoredReviews();
});
document.querySelector("#closeIgnoredReviews").addEventListener("click", () => ignoredReviewsDialog.close());

document.querySelector("#scanButton").addEventListener("click", async () => {
  const button = document.querySelector("#scanButton");
  await scanMailbox(button, "↻");
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("cancel", (event) => {
    if (dialog.dataset.writing === "true") event.preventDefault();
  });
  dialog.addEventListener("close", () => {
    if (dialog === settingsDialog) state.settingsGeneration += 1;
    const opener = dialogOpeners.get(dialog);
    dialogOpeners.delete(dialog);
    scheduleDeferredRefresh();
    requestAnimationFrame(() => {
      if (document.querySelector("dialog[open]")) return;
      if (opener instanceof HTMLElement && opener.isConnected && !opener.disabled) {
        opener.focus();
        return;
      }
      const activeTab = document.querySelector(".tab.active");
      if (activeTab instanceof HTMLElement) activeTab.focus();
    });
  });
});
cards.addEventListener("focusout", scheduleDeferredRefresh);

let githubUpdatePoll = null;
async function refreshGithubUpdate() {
  if (!apiReady()) return;
  clearTimeout(githubUpdatePoll);
  try {
    const result = await window.pywebview.api.github_update_status();
    const status = document.querySelector("#githubUpdateStatus");
    status.textContent = result.message;
    const progress = document.querySelector("#githubUpdateProgress");
    progress.hidden = result.status !== "downloading";
    if (result.total > 0) { progress.max = result.total; progress.value = result.downloaded; }
    else progress.removeAttribute("value");
    document.querySelector("#githubUpdateNotes").textContent = result.notes || "";
    document.querySelector("#githubUpdateNotesPanel").hidden = !result.notes;
    document.querySelector("#checkGithubUpdate").disabled = result.busy;
    document.querySelector("#downloadGithubUpdate").hidden = !["available", "error"].includes(result.status) || !result.latest_version;
    document.querySelector("#downloadGithubUpdate").disabled = result.busy;
    document.querySelector("#installGithubUpdate").hidden = result.status !== "ready";
    document.querySelector("#installGithubUpdate").disabled = result.busy;
    const notice = document.querySelector("#githubUpdateNotice");
    notice.hidden = !result.latest_version;
    notice.textContent = result.status === "ready" ? "新版已下载，查看更新" : `发现新版 ${result.latest_version}，查看更新`;
    if (result.busy) githubUpdatePoll = setTimeout(refreshGithubUpdate, 600);
  } catch (error) {
    document.querySelector("#githubUpdateStatus").textContent = error?.message || "读取更新状态失败";
  }
}
async function githubUpdateAction(action) {
  try { await action(); await refreshGithubUpdate(); }
  catch (error) { document.querySelector("#githubUpdateStatus").textContent = error?.message || "更新操作失败"; }
}
document.querySelector("#checkGithubUpdate").addEventListener("click", () => githubUpdateAction(
  () => window.pywebview.api.check_github_update(settingsForm.elements.update_channel.value, false)));
document.querySelector("#downloadGithubUpdate").addEventListener("click", () => githubUpdateAction(
  () => window.pywebview.api.download_github_update()));
document.querySelector("#openGithubReleases").addEventListener("click", () => githubUpdateAction(
  () => window.pywebview.api.open_github_releases()));
document.querySelector("#githubUpdateNotice").addEventListener("click", async () => {
  await showSettingsDialog();
  document.querySelector("#githubUpdateSection").scrollIntoView({block: "center"});
});
document.querySelector("#installGithubUpdate").addEventListener("click", () => {
  document.querySelector("#githubInstallStatus").textContent = "";
  document.querySelector("#githubUpdateDialog").showModal();
});
document.querySelector("#cancelGithubInstall").addEventListener("click", () => document.querySelector("#githubUpdateDialog").close());
document.querySelector("#confirmGithubInstall").addEventListener("click", async (event) => {
  event.target.disabled = true;
  document.querySelector("#githubInstallStatus").textContent = "正在准备替换程序文件…";
  try {
    await window.pywebview.api.install_github_update(true);
    document.querySelector("#githubInstallStatus").textContent = "正在重启更新…";
  } catch (error) {
    document.querySelector("#githubInstallStatus").textContent = error?.message || "无法自动更新，请打开发布页手动更新。";
    event.target.disabled = false;
  }
});
setInterval(async () => {
  if (!apiReady()) return;
  try {
    await window.pywebview.api.check_github_update("preview", true);
    refreshGithubUpdate();
  } catch (_) { /* Updates must never interrupt mail or task operations. */ }
}, 3600000);

window.addEventListener("pywebviewready", initializeApp);
setTimeout(initializeApp, 800);
setInterval(refresh, 60_000);
