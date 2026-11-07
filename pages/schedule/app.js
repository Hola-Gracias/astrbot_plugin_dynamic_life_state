const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const NATURAL_SLOTS = {
  "凌晨": [0, 360],
  "早上": [360, 540],
  "上午": [540, 720],
  "中午": [720, 840],
  "下午": [840, 1080],
  "傍晚": [1080, 1200],
  "晚上": [1200, 1380],
  "深夜": [1380, 1440],
};

const WEATHER_CONDITIONS = [
  "晴", "多云", "阴", "雾", "霾",
  "小雨", "中雨", "大雨", "阵雨", "雷阵雨",
  "小雪", "中雪", "大雪",
];
const PRECIPITATION_OPTIONS = [
  "无", "小雨", "中雨", "大雨", "阵雨", "雷阵雨",
  "小雪", "中雪", "大雪",
];
const WEATHER_ICONS = {
  "雷阵雨": "⛈️",
  "阵雨": "🌦️",
  "小雨": "🌦️",
  "中雨": "🌧️",
  "大雨": "🌧️",
  "小雪": "🌨️",
  "中雪": "❄️",
  "大雪": "❄️",
  "雾": "🌫️",
  "霾": "😷",
  "晴": "☀️",
  "多云": "⛅",
  "阴": "☁️",
};

function weatherIcon(condition) {
  const value = text(condition);
  for (const key of Object.keys(WEATHER_ICONS)) {
    if (value.includes(key)) {
      return WEATHER_ICONS[key];
    }
  }
  return "🌡️";
}

const state = {
  date: "",
  today: "",
  tomorrow: "",
  payload: null,
  draft: null,
  dirty: false,
  loading: false,
  saving: false,
  optionalFields: [],
  naturalSlots: [],
  invalid: new Map(),
  weather: [],
  weatherSummary: "",
  season: "",
  weatherEditable: false,
  weatherSource: "none",
  weatherOverride: false,
  weatherDirty: false,
  weatherSaving: false,
};

const els = {
  refreshBtn: document.getElementById("refreshBtn"),
  prevBtn: document.getElementById("prevBtn"),
  nextBtn: document.getElementById("nextBtn"),
  todayBtn: document.getElementById("todayBtn"),
  dateInput: document.getElementById("dateInput"),
  statusPill: document.getElementById("statusPill"),
  availableBar: document.getElementById("availableBar"),
  globalError: document.getElementById("globalError"),
  readonlyNotice: document.getElementById("readonlyNotice"),
  loadingState: document.getElementById("loadingState"),
  emptyState: document.getElementById("emptyState"),
  emptyTitle: document.getElementById("emptyTitle"),
  emptyDesc: document.getElementById("emptyDesc"),
  editor: document.getElementById("editor"),
  generatedMeta: document.getElementById("generatedMeta"),
  weatherPanel: document.getElementById("weatherPanel"),
  weatherTitle: document.getElementById("weatherTitle"),
  seasonBadge: document.getElementById("seasonBadge"),
  weatherSummary: document.getElementById("weatherSummary"),
  weatherRows: document.getElementById("weatherRows"),
  weatherNote: document.getElementById("weatherNote"),
  saveWeatherBtn: document.getElementById("saveWeatherBtn"),
  resetWeatherBtn: document.getElementById("resetWeatherBtn"),
  conditionOptions: document.getElementById("conditionOptions"),
  precipitationOptions: document.getElementById("precipitationOptions"),
  holidayInput: document.getElementById("holidayInput"),
  summaryInput: document.getElementById("summaryInput"),
  styleInput: document.getElementById("styleInput"),
  entryCount: document.getElementById("entryCount"),
  addBtn: document.getElementById("addBtn"),
  timeline: document.getElementById("timeline"),
  slotOptions: document.getElementById("slotOptions"),
  saveBar: document.getElementById("saveBar"),
  dirtyText: document.getElementById("dirtyText"),
  resetBtn: document.getElementById("resetBtn"),
  saveBtn: document.getElementById("saveBtn"),
  toast: document.getElementById("toast"),
  confirmDialog: document.getElementById("confirmDialog"),
  confirmMessage: document.getElementById("confirmMessage"),
  confirmOk: document.getElementById("confirmOk"),
  confirmCancel: document.getElementById("confirmCancel"),
};

let toastTimer = 0;
let confirmResolver = null;

function askConfirm(message, okLabel) {
  return new Promise((resolve) => {
    if (confirmResolver) {
      confirmResolver(false);
      confirmResolver = null;
    }
    confirmResolver = resolve;
    els.confirmMessage.textContent = message;
    els.confirmOk.textContent = okLabel || "确定";
    els.confirmDialog.hidden = false;
    window.setTimeout(() => els.confirmOk.focus(), 0);
  });
}

function settleConfirm(value) {
  const resolver = confirmResolver;
  confirmResolver = null;
  els.confirmDialog.hidden = true;
  if (resolver) {
    resolver(value);
  }
}

function text(value) {
  return value == null ? "" : String(value);
}

function clone(value) {
  if (value == null) {
    return null;
  }
  return JSON.parse(JSON.stringify(value));
}

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (value != null) {
    node.textContent = value;
  }
  return node;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function minutesLabel(minutes) {
  const hour = Math.floor(minutes / 60);
  const minute = minutes % 60;
  return pad2(hour) + ":" + pad2(minute);
}

function parseSlot(raw) {
  const value = text(raw).trim();
  if (!value) {
    return null;
  }
  for (const name of Object.keys(NATURAL_SLOTS)) {
    if (value.includes(name)) {
      const range = NATURAL_SLOTS[name];
      return { kind: "natural", label: name, start: range[0], end: range[1] };
    }
  }
  const match = /^(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})$/.exec(value);
  if (!match) {
    return null;
  }
  const startHour = Number(match[1]);
  const startMinute = Number(match[2]);
  const endHour = Number(match[3]);
  const endMinute = Number(match[4]);
  if (startHour > 23 || startMinute > 59 || endMinute > 59) {
    return null;
  }
  if (endHour > 24 || (endHour === 24 && endMinute !== 0)) {
    return null;
  }
  const start = startHour * 60 + startMinute;
  let end = endHour * 60 + endMinute;
  if (start === end) {
    return null;
  }
  if (end <= start) {
    end = 1440;
  }
  return { kind: "range", label: value, start: start, end: end };
}

function formatSlot(raw) {
  const value = text(raw).trim();
  if (!value) {
    return "—";
  }
  const slot = parseSlot(value);
  if (!slot) {
    return "格式待确认";
  }
  return "[" + minutesLabel(slot.start) + ", " + minutesLabel(slot.end) + ")";
}

function shiftDate(iso, delta) {
  const parts = String(iso || "").split("-").map(Number);
  if (parts.length !== 3 || parts.some((item) => Number.isNaN(item))) {
    return iso;
  }
  const stamp = Date.UTC(parts[0], parts[1] - 1, parts[2]) + delta * 86400000;
  const value = new Date(stamp);
  return (
    value.getUTCFullYear() +
    "-" +
    pad2(value.getUTCMonth() + 1) +
    "-" +
    pad2(value.getUTCDate())
  );
}

function normalizeDraft(source) {
  const timeline = Array.isArray(source && source.timeline)
    ? source.timeline.map((item) => {
        const entry = {
          time: text(item && item.time),
          schedule: text(item && item.schedule),
          outfit: text(item && item.outfit),
          weather: item && item.weather ? item.weather : null,
        };
        state.optionalFields.forEach((field) => {
          entry[field.key] = text(item && item[field.key]);
        });
        return entry;
      })
    : [];
  return {
    date: text(source && source.date),
    holiday: text(source && source.holiday) || "无",
    schedule_summary: text(source && source.schedule_summary),
    style_summary: text(source && source.style_summary),
    weather_summary: text(source && source.weather_summary),
    weather: Array.isArray(source && source.weather) ? source.weather : [],
    season: text(source && source.season),
    generated_at: text(source && source.generated_at),
    status: text(source && source.status) || "ok",
    timeline: timeline,
  };
}

function applyContext(context) {
  const dark = Boolean(context && context.isDark);
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
  if (context && context.pageTitle) {
    document.title = context.pageTitle;
  }
}

function showToast(message, isError) {
  if (!message) {
    return;
  }
  els.toast.textContent = message;
  els.toast.classList.toggle("toast--error", Boolean(isError));
  els.toast.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    els.toast.hidden = true;
  }, 2800);
}

function explainErrorMessage(message) {
  const value = text(message);
  if (value.includes("未找到该路由")) {
    return "插件后端接口还没有注册。通常是因为 AstrBot 仍在运行旧插件代码：请在「插件管理 → 动态生活状态」点击「重载」（不是刷新网页），然后刷新本页。";
  }
  return value || "读取日程失败，请稍后重试。";
}

function showError(message) {
  if (!message) {
    els.globalError.hidden = true;
    els.globalError.textContent = "";
    return;
  }
  els.globalError.textContent = message;
  els.globalError.hidden = false;
}

function isEditable() {
  return Boolean(
    !state.loading && state.payload && state.payload.editable && state.draft
  );
}

function markDirty() {
  if (!isEditable()) {
    return;
  }
  if (!state.dirty) {
    state.dirty = true;
    renderSaveBar();
  }
}

function renderSaveBar() {
  const editable = isEditable();
  els.saveBar.hidden = !editable;
  if (!editable) {
    return;
  }
  els.saveBar.classList.toggle("is-dirty", state.dirty);
  if (state.saving) {
    els.dirtyText.textContent = "正在保存…";
  } else if (state.dirty) {
    els.dirtyText.textContent = "有未保存的修改";
  } else {
    els.dirtyText.textContent = "尚未修改";
  }
  els.saveBtn.disabled = state.saving || !state.dirty;
  els.resetBtn.disabled = state.saving || !state.dirty;
  els.saveBtn.textContent = state.saving ? "保存中…" : "保存日程";
}

function renderStatus() {
  const payload = state.payload;
  const hasState = Boolean(payload && payload.state && state.draft);
  let label = "加载中…";
  let tone = "muted";
  if (state.loading) {
    label = "加载中…";
    tone = "muted";
  } else if (!payload) {
    label = "读取失败";
    tone = "danger";
  } else if (payload.generating) {
    label = "正在生成…";
    tone = "warn";
  } else if (payload.is_tomorrow) {
    label = payload.weather_override ? "明日天气预设" : "明天 · 可预设天气";
    tone = "warn";
  } else if (!hasState) {
    label = payload.is_today
      ? (payload.weather_editable ? "今日未生成 · 可预设天气" : "今日未生成")
      : "无记录";
    tone = "muted";
  } else if (state.draft.status === "failed") {
    label = "生成失败";
    tone = "danger";
  } else if (payload.is_today) {
    label = payload.editable ? "今日日程 · 可编辑" : "今日日程 · 只读";
    tone = payload.editable ? "ok" : "warn";
  } else {
    label = "历史日程";
    tone = "muted";
  }
  els.statusPill.textContent = label;
  els.statusPill.className = "pill pill--" + tone;
}

function renderDateControls() {
  const maxDate = state.tomorrow || state.today || "";
  els.dateInput.value = state.date || "";
  els.dateInput.max = maxDate;
  els.prevBtn.disabled = state.loading || !state.date;
  els.nextBtn.disabled =
    state.loading || !state.date || !maxDate || state.date >= maxDate;
  els.todayBtn.disabled = state.loading || !state.today || state.date === state.today;
  els.refreshBtn.disabled = state.loading;
}

function renderChips() {
  els.availableBar.textContent = "";
  if (!state.payload) {
    return;
  }
  const dates = new Set();
  if (state.today) {
    dates.add(state.today);
  }
  if (state.tomorrow) {
    dates.add(state.tomorrow);
  }
  (state.payload.available_dates || []).forEach((item) => {
    if (item) {
      dates.add(item);
    }
  });
  const ordered = Array.from(dates).sort().reverse();
  ordered.forEach((date) => {
    const label =
      date === state.today ? "今天" : date === state.tomorrow ? "明天" : date.slice(5);
    const chip = el("button", "chip", label);
    chip.type = "button";
    chip.title = date;
    if (date === state.date) {
      chip.classList.add("is-active");
    }
    chip.addEventListener("click", () => {
      if (date !== state.date) {
        load(date);
      }
    });
    els.availableBar.appendChild(chip);
  });
}

function renderSummary() {
  const draft = state.draft;
  const editable = isEditable();
  els.editor.hidden = !draft || state.loading;
  if (!draft) {
    return;
  }
  els.holidayInput.value = draft.holiday;
  els.summaryInput.value = draft.schedule_summary;
  els.styleInput.value = draft.style_summary;
  els.holidayInput.disabled = !editable;
  els.summaryInput.disabled = !editable;
  els.styleInput.disabled = !editable;
  els.addBtn.disabled = !editable;
  els.entryCount.textContent = draft.timeline.length + " 个时段";
  els.generatedMeta.textContent = draft.generated_at
    ? "生成于 " + draft.generated_at.replace("T", " ").slice(0, 19)
    : "生成时间未知";
}

function updateWeatherSummaryUI() {
  if (!state.weather.length) {
    els.weatherSummary.textContent = "";
    return;
  }
  const temperatures = state.weather
    .map((slot) => Number(slot.temperature))
    .filter((value) => Number.isFinite(value));
  const low = temperatures.length ? Math.min(...temperatures) : 0;
  const high = temperatures.length ? Math.max(...temperatures) : 0;
  const morning = state.weather.find((slot) => slot.time === "早上");
  const afternoon = state.weather.find((slot) => slot.time === "下午");
  const morningCondition = morning ? text(morning.condition) : "";
  const afternoonCondition = afternoon ? text(afternoon.condition) : "";
  const transition =
    morningCondition === afternoonCondition
      ? morningCondition
      : morningCondition + "转" + afternoonCondition;
  const wind = state.weather.find((slot) => text(slot.wind).trim());
  let summary = transition + "，" + low + "~" + high + "℃";
  if (wind) {
    summary += "，" + text(wind.wind);
  }
  if (state.weatherDirty) {
    summary += "（未保存）";
  }
  els.weatherSummary.textContent = summary;
}

function weatherNoteText() {
  if (state.payload && state.payload.is_history) {
    return "历史天气仅供查看。";
  }
  if (!state.weatherEditable) {
    return "当前天气不可编辑。";
  }
  if (state.payload && state.payload.is_tomorrow) {
    return "明天还没有日程。保存后，明天 00:00 生成日程时会优先使用这份天气。";
  }
  if (state.weatherSource === "override") {
    return "已保存为今天的手动天气；重新生成日程时会继续使用它。修改天气不会自动重排已有日程。";
  }
  if (state.weatherSource === "state") {
    return "天气已保存在今天的状态中；修改后点「保存天气」生效，日程不会自动重排。";
  }
  return "当前为自动生成的预览天气，尚未写入注入；修改后点「保存天气」才会生效。";
}

function fillWeatherSelect(select, values, current) {
  select.textContent = "";
  const list = values.slice();
  if (current && !list.includes(current)) {
    list.push(current);
  }
  list.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.value = current || list[0] || "";
}

function renderWeatherRows() {
  els.weatherRows.textContent = "";
  const editable = state.weatherEditable && !state.weatherSaving;
  state.weather.forEach((slot, index) => {
    const row = document.createElement("tr");

    const timeCell = el("td", "weather-table__time");
    const icon = el("span", "weather-table__icon", weatherIcon(slot.condition));
    timeCell.appendChild(icon);
    timeCell.appendChild(document.createTextNode(text(slot.time)));
    row.appendChild(timeCell);

    const conditionCell = document.createElement("td");
    const conditionSelect = document.createElement("select");
    fillWeatherSelect(conditionSelect, WEATHER_CONDITIONS, text(slot.condition));
    conditionSelect.disabled = !editable;
    conditionSelect.addEventListener("change", () => {
      slot.condition = conditionSelect.value;
      icon.textContent = weatherIcon(slot.condition);
      const wet = /雨|雪/.test(slot.condition);
      if (wet) {
        slot.precipitation = slot.condition;
        if (Number(slot.precipitation_probability) < 60) {
          slot.precipitation_probability = 70;
        }
        precipitationSelect.value = slot.precipitation;
        probabilityInput.value = String(slot.precipitation_probability);
      } else if (/雨|雪/.test(text(slot.precipitation))) {
        slot.precipitation = "无";
        precipitationSelect.value = "无";
      }
      markWeatherDirty();
    });
    conditionCell.appendChild(conditionSelect);
    row.appendChild(conditionCell);

    const tempCell = document.createElement("td");
    const tempInput = document.createElement("input");
    tempInput.type = "number";
    tempInput.min = "-50";
    tempInput.max = "60";
    tempInput.step = "1";
    tempInput.value = String(slot.temperature);
    tempInput.disabled = !editable;
    tempInput.addEventListener("input", () => {
      const value = Number(tempInput.value);
      if (Number.isFinite(value)) {
        slot.temperature = Math.round(value);
        markWeatherDirty();
      }
    });
    tempCell.appendChild(tempInput);
    row.appendChild(tempCell);

    const precipitationCell = document.createElement("td");
    const precipitationSelect = document.createElement("select");
    fillWeatherSelect(
      precipitationSelect,
      PRECIPITATION_OPTIONS,
      text(slot.precipitation) || "无"
    );
    precipitationSelect.disabled = !editable;
    precipitationSelect.addEventListener("change", () => {
      slot.precipitation = precipitationSelect.value;
      markWeatherDirty();
    });
    precipitationCell.appendChild(precipitationSelect);
    row.appendChild(precipitationCell);

    const probabilityCell = document.createElement("td");
    const probabilityInput = document.createElement("input");
    probabilityInput.type = "number";
    probabilityInput.min = "0";
    probabilityInput.max = "100";
    probabilityInput.step = "1";
    probabilityInput.value = String(slot.precipitation_probability || 0);
    probabilityInput.disabled = !editable;
    probabilityInput.addEventListener("input", () => {
      const value = Number(probabilityInput.value);
      if (Number.isFinite(value)) {
        slot.precipitation_probability = Math.max(0, Math.min(100, Math.round(value)));
        markWeatherDirty();
      }
    });
    probabilityCell.appendChild(probabilityInput);
    row.appendChild(probabilityCell);

    const windCell = document.createElement("td");
    const windInput = document.createElement("input");
    windInput.type = "text";
    windInput.maxLength = 40;
    windInput.value = text(slot.wind);
    windInput.disabled = !editable;
    windInput.addEventListener("input", () => {
      slot.wind = windInput.value;
      markWeatherDirty();
    });
    windCell.appendChild(windInput);
    row.appendChild(windCell);

    row.dataset.index = String(index);
    els.weatherRows.appendChild(row);
  });
}

function renderWeatherButtons() {
  const canSave = Boolean(
    state.weatherEditable &&
      !state.weatherSaving &&
      (state.weatherDirty ||
        (state.weatherSource !== "state" && state.weatherSource !== "override"))
  );
  els.saveWeatherBtn.disabled = !canSave;
  els.saveWeatherBtn.textContent = state.weatherSaving ? "保存中…" : "保存天气";
  const canReset = Boolean(
    state.weatherEditable && state.weatherOverride && !state.weatherSaving
  );
  els.resetWeatherBtn.hidden = !canReset;
  els.resetWeatherBtn.disabled = !canReset;
}

function markWeatherDirty() {
  if (!state.weatherEditable) {
    return;
  }
  state.weatherDirty = true;
  updateWeatherSummaryUI();
  renderWeatherButtons();
}

function renderWeather() {
  const payload = state.payload;
  const show = Boolean(payload && !state.loading && state.weather.length);
  els.weatherPanel.hidden = !show;
  if (!show) {
    return;
  }
  els.weatherTitle.textContent = payload.is_tomorrow
    ? "明天天气预设"
    : payload.is_history
      ? "当日天气"
      : "今日天气";
  els.seasonBadge.textContent = state.season || "";
  els.seasonBadge.hidden = !state.season;
  updateWeatherSummaryUI();
  renderWeatherRows();
  renderWeatherButtons();
  els.weatherNote.textContent = weatherNoteText();
}

function renderEmpty() {
  const hasState = Boolean(state.payload && state.payload.state && state.draft);
  els.emptyState.hidden = state.loading || hasState || !state.payload;
  if (els.emptyState.hidden) {
    return;
  }
  if (state.payload.is_tomorrow) {
    els.emptyTitle.textContent = "明天还没有日程";
    els.emptyDesc.textContent =
      "可以先在下方预设天气；明天 00:00 生成日程时会优先使用这份天气。";
  } else if (state.payload.is_today) {
    els.emptyTitle.textContent = "今天还没有日程";
    els.emptyDesc.textContent =
      "可以先在下方预设天气；生成日程时会优先使用这份天气。也可以发送 /dls regen 立即生成。";
  } else {
    els.emptyTitle.textContent = state.date + " 没有日程";
    els.emptyDesc.textContent = "该日期没有已生成或已归档的生活状态。";
  }
}

function renderNotice() {
  const payload = state.payload;
  const hasState = Boolean(payload && payload.state && state.draft);
  let message = "";
  if (payload && hasState && !payload.is_today) {
    message = "历史日程仅供查看。只有今天正在使用的日程可以修改。";
  } else if (payload && hasState && payload.is_today && !payload.editable) {
    message = payload.generating
      ? "今天的日程正在生成，生成完成前无法编辑；生成结束后刷新本页即可修改。"
      : "当前日程不可编辑，请先确认插件与生活状态存储正常。";
  }
  els.readonlyNotice.hidden = !message;
  els.readonlyNotice.textContent = message;
}

function fieldRow(labelText, control, wide) {
  const label = el("label", "field" + (wide ? " field--wide" : ""));
  label.appendChild(el("span", "", labelText));
  label.appendChild(control);
  return label;
}

function renderTimeline() {
  els.timeline.textContent = "";
  if (!state.draft) {
    return;
  }
  const editable = isEditable();
  state.draft.timeline.forEach((entry, index) => {
    const card = el("article", "slot");
    card.dataset.index = String(index);
    if (state.invalid.has(index)) {
      card.classList.add("is-invalid");
    }

    const head = el("div", "slot__head");
    head.appendChild(el("div", "slot__index", pad2(index + 1)));

    const timeLabel = el("label", "slot__time");
    timeLabel.appendChild(el("span", "", "时间"));
    const timeInput = document.createElement("input");
    timeInput.type = "text";
    timeInput.value = entry.time;
    timeInput.maxLength = 200;
    timeInput.placeholder = "上午 / 08:00-09:00";
    timeInput.setAttribute("list", "slotOptions");
    timeInput.disabled = !editable;
    timeInput.addEventListener("input", () => {
      entry.time = timeInput.value;
      range.textContent = formatSlot(entry.time);
      markDirty();
    });
    timeLabel.appendChild(timeInput);
    head.appendChild(timeLabel);

    const range = el("div", "slot__range", formatSlot(entry.time));
    head.appendChild(range);

    if (entry.weather) {
      head.appendChild(
        el(
          "div",
          "slot__weather",
          text(entry.weather.icon || "🌡️") +
            " " +
            text(entry.weather.condition) +
            " " +
            text(entry.weather.temperature) +
            "℃"
        )
      );
    }

    const tools = el("div", "slot__tools");
    const up = el("button", "", "↑");
    up.type = "button";
    up.title = "上移";
    up.disabled = !editable || index === 0;
    up.addEventListener("click", () => moveEntry(index, -1));
    const down = el("button", "", "↓");
    down.type = "button";
    down.title = "下移";
    down.disabled = !editable || index === state.draft.timeline.length - 1;
    down.addEventListener("click", () => moveEntry(index, 1));
    const remove = el("button", "", "✕");
    remove.type = "button";
    remove.title = "删除此时段";
    remove.dataset.act = "remove";
    remove.disabled = !editable;
    remove.addEventListener("click", () => removeEntry(index));
    tools.appendChild(up);
    tools.appendChild(down);
    tools.appendChild(remove);
    head.appendChild(tools);
    card.appendChild(head);

    const body = el("div", "slot__body");
    const schedule = document.createElement("textarea");
    schedule.rows = 2;
    schedule.maxLength = 4000;
    schedule.value = entry.schedule;
    schedule.placeholder = "这个时间段正在做的事情";
    schedule.disabled = !editable;
    schedule.addEventListener("input", () => {
      entry.schedule = schedule.value;
      markDirty();
    });
    body.appendChild(fieldRow("日程", schedule, true));

    const outfit = document.createElement("input");
    outfit.type = "text";
    outfit.maxLength = 4000;
    outfit.value = entry.outfit;
    outfit.placeholder = "这个时间段的穿搭";
    outfit.disabled = !editable;
    outfit.addEventListener("input", () => {
      entry.outfit = outfit.value;
      markDirty();
    });
    body.appendChild(fieldRow("穿搭", outfit, true));
    card.appendChild(body);

    if (state.optionalFields.length) {
      const optional = el("div", "slot__optional");
      state.optionalFields.forEach((field) => {
        const input = document.createElement("input");
        input.type = "text";
        input.maxLength = 4000;
        input.value = text(entry[field.key]);
        input.placeholder = "可选";
        input.disabled = !editable;
        input.addEventListener("input", () => {
          entry[field.key] = input.value;
          markDirty();
        });
        optional.appendChild(
          fieldRow(field.icon + " " + field.label, input, false)
        );
      });
      card.appendChild(optional);
    }

    if (state.invalid.has(index)) {
      card.appendChild(el("p", "slot__error", state.invalid.get(index)));
    }

    els.timeline.appendChild(card);
  });

  if (state.payload && state.payload.generating) {
    const notice = el("p", "hint", "正在生成日程，完成后刷新即可编辑。");
    els.timeline.appendChild(notice);
  }
}

function render() {
  els.loadingState.hidden = !state.loading;
  renderStatus();
  renderDateControls();
  renderChips();
  renderSummary();
  renderWeather();
  renderEmpty();
  renderNotice();
  renderTimeline();
  renderSaveBar();
}

function validateDraft() {
  const errors = [];
  const invalid = new Map();
  if (!state.draft || !state.draft.timeline.length) {
    errors.push("时间线至少需要一个时段。");
    return { errors: errors, invalid: invalid };
  }
  state.draft.timeline.forEach((entry, index) => {
    const missing = [];
    if (!text(entry.time).trim()) {
      missing.push("时间");
    } else if (!parseSlot(entry.time)) {
      missing.push("时间格式（使用自然时段或 HH:MM-HH:MM）");
    }
    if (!text(entry.schedule).trim()) {
      missing.push("日程");
    }
    if (!text(entry.outfit).trim()) {
      missing.push("穿搭");
    }
    if (missing.length) {
      const message = "缺少或无法识别：" + missing.join("、");
      errors.push("第 " + (index + 1) + " 个时段" + message + "。");
      invalid.set(index, message);
    }
  });
  return { errors: errors, invalid: invalid };
}

async function save() {
  if (!isEditable() || state.saving) {
    return;
  }
  const result = validateDraft();
  state.invalid = result.invalid;
  if (result.errors.length) {
    renderTimeline();
    showError(result.errors[0]);
    showToast(result.errors[0], true);
    return;
  }
  state.saving = true;
  renderSaveBar();
  try {
    const body = {
      date: state.draft.date,
      holiday: state.draft.holiday,
      schedule_summary: state.draft.schedule_summary,
      style_summary: state.draft.style_summary,
      timeline: state.draft.timeline.map((entry) => {
        const item = {
          time: entry.time,
          schedule: entry.schedule,
          outfit: entry.outfit,
        };
        state.optionalFields.forEach((field) => {
          item[field.key] = entry[field.key];
        });
        return item;
      }),
    };
    const saved = await bridge.apiPost("schedule/save", body);
    if (saved && saved.state) {
      state.payload.state = saved.state;
      state.payload.editable = true;
      state.draft = normalizeDraft(saved.state);
      if (!state.weatherDirty && Array.isArray(saved.state.weather)) {
        state.weather = clone(saved.state.weather);
        state.weatherSummary = text(saved.state.weather_summary);
        state.season = text(saved.state.season);
        state.weatherSource = "state";
      }
    }
    state.dirty = false;
    state.invalid = new Map();
    showError("");
    render();
    showToast((saved && saved.message) || "日程已保存。", false);
  } catch (error) {
    showError(explainErrorMessage(error && error.message));
  } finally {
    state.saving = false;
    renderSaveBar();
  }
}

function addEntry() {
  if (!isEditable()) {
    return;
  }
  const entry = { time: "", schedule: "", outfit: "" };
  state.optionalFields.forEach((field) => {
    entry[field.key] = "";
  });
  state.draft.timeline.push(entry);
  state.dirty = true;
  renderTimeline();
  renderSaveBar();
  const cards = els.timeline.querySelectorAll(".slot");
  const last = cards[cards.length - 1];
  if (last) {
    last.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const input = last.querySelector(".slot__time input");
    if (input) {
      input.focus();
    }
  }
}

async function removeEntry(index) {
  if (!isEditable()) {
    return;
  }
  if (state.draft.timeline.length <= 1) {
    showToast("时间线至少需要保留一个时段。", true);
    return;
  }
  const entry = state.draft.timeline[index];
  const hasContent =
    entry &&
    (text(entry.schedule).trim() || text(entry.outfit).trim() || text(entry.time).trim());
  if (
    hasContent &&
    !(await askConfirm("删除第 " + (index + 1) + " 个时段？", "删除"))
  ) {
    return;
  }
  state.draft.timeline.splice(index, 1);
  state.invalid.delete(index);
  state.dirty = true;
  renderTimeline();
  renderSaveBar();
}

function moveEntry(index, delta) {
  if (!isEditable()) {
    return;
  }
  const target = index + delta;
  const list = state.draft.timeline;
  if (target < 0 || target >= list.length) {
    return;
  }
  const temp = list[index];
  list[index] = list[target];
  list[target] = temp;
  state.dirty = true;
  renderTimeline();
  renderSaveBar();
}


function validateWeather() {
  if (!state.weather.length) {
    return "天气数据为空，请刷新后重试。";
  }
  for (let i = 0; i < state.weather.length; i += 1) {
    const slot = state.weather[i];
    if (!text(slot.condition).trim()) {
      return "第 " + (i + 1) + " 个时段缺少天气现象。";
    }
    const temperature = Number(slot.temperature);
    if (!Number.isFinite(temperature) || temperature < -50 || temperature > 60) {
      return "第 " + (i + 1) + " 个时段气温必须在 -50 到 60℃ 之间。";
    }
    const probability = Number(slot.precipitation_probability);
    if (!Number.isFinite(probability) || probability < 0 || probability > 100) {
      return "第 " + (i + 1) + " 个时段降水概率必须在 0 到 100 之间。";
    }
  }
  return "";
}

function applyWeatherResult(result) {
  if (!result) {
    return;
  }
  if (Array.isArray(result.weather)) {
    state.weather = clone(result.weather);
  }
  state.weatherSummary = text(result.weather_summary) || state.weatherSummary;
  state.season = text(result.season) || state.season;
  state.weatherSource = text(result.weather_source) || state.weatherSource;
  state.weatherOverride = Boolean(result.weather_override);
  if (state.payload) {
    state.payload.weather = clone(state.weather);
    state.payload.weather_summary = state.weatherSummary;
    state.payload.season = state.season;
    state.payload.weather_source = state.weatherSource;
    state.payload.weather_override = state.weatherOverride;
  }
  if (result.today) {
    state.today = text(result.today);
  }
  if (result.tomorrow) {
    state.tomorrow = text(result.tomorrow);
  }
  if (result.state) {
    state.payload.state = result.state;
    if (!state.dirty) {
      state.draft = normalizeDraft(result.state);
    }
  }
}

async function saveWeather() {
  if (!state.weatherEditable || state.weatherSaving) {
    return;
  }
  const error = validateWeather();
  if (error) {
    showError(error);
    showToast(error, true);
    return;
  }
  state.weatherSaving = true;
  renderWeatherRows();
  renderWeatherButtons();
  try {
    const result = await bridge.apiPost("schedule/weather/save", {
      date: state.date,
      weather: state.weather.map((slot) => ({
        time: slot.time,
        condition: slot.condition,
        temperature: Math.round(Number(slot.temperature)),
        precipitation: slot.precipitation || "无",
        precipitation_probability: Math.round(
          Number(slot.precipitation_probability)
        ),
        wind: slot.wind || "",
      })),
    });
    applyWeatherResult(result);
    state.weatherDirty = false;
    showError("");
    showToast((result && result.message) || "天气已保存。", false);
    if (!state.dirty) {
      await load(state.date, true);
    } else {
      state.weatherSaving = false;
      renderWeather();
    }
  } catch (error) {
    showError(explainErrorMessage(error && error.message));
  } finally {
    state.weatherSaving = false;
    renderWeatherButtons();
    renderWeatherRows();
  }
}

async function resetWeather() {
  if (!state.weatherEditable || state.weatherSaving) {
    return;
  }
  if (!(await askConfirm("恢复为自动生成的天气？", "恢复"))) {
    return;
  }
  state.weatherSaving = true;
  renderWeatherButtons();
  try {
    const result = await bridge.apiPost("schedule/weather/reset", {
      date: state.date,
    });
    applyWeatherResult(result);
    state.weatherDirty = false;
    showError("");
    showToast((result && result.message) || "天气已恢复为自动生成。", false);
    if (!state.dirty) {
      await load(state.date, true);
    } else {
      state.weatherSaving = false;
      renderWeather();
    }
  } catch (error) {
    showError(explainErrorMessage(error && error.message));
  } finally {
    state.weatherSaving = false;
    renderWeatherButtons();
    renderWeatherRows();
  }
}

async function load(date, skipConfirm) {
  if (state.loading || !bridge) {
    return;
  }
  if (
    !skipConfirm &&
    (state.dirty || state.weatherDirty) &&
    !(await askConfirm("有未保存的修改，确定要切换日期吗？", "切换日期"))
  ) {
    return;
  }
  state.loading = true;
  showError("");
  render();
  try {
    const params = date ? { date: date } : {};
    const data = await bridge.apiGet("schedule/state", params);
    state.payload = data;
    state.date = text(data.date);
    state.today = text(data.today);
    state.tomorrow = text(data.tomorrow) || shiftDate(state.today, 1);
    state.naturalSlots = Array.isArray(data.natural_slots)
      ? data.natural_slots
      : Object.keys(NATURAL_SLOTS);
    state.optionalFields = Array.isArray(data.optional_fields)
      ? data.optional_fields
      : [];
    state.weather = Array.isArray(data.weather) ? clone(data.weather) : [];
    state.weatherSummary = text(data.weather_summary);
    state.season = text(data.season);
    state.weatherEditable = Boolean(data.weather_editable);
    state.weatherSource = text(data.weather_source) || "none";
    state.weatherOverride = Boolean(data.weather_override);
    state.weatherDirty = false;
    state.weatherSaving = false;
    state.draft = data.state ? normalizeDraft(data.state) : null;
    state.dirty = false;
    state.invalid = new Map();
    renderSlotOptions();
  } catch (error) {
    state.payload = null;
    state.draft = null;
    state.dirty = false;
    state.weather = [];
    state.weatherSummary = "";
    state.season = "";
    state.weatherEditable = false;
    state.weatherDirty = false;
    showError(explainErrorMessage(error && error.message));
  } finally {
    state.loading = false;
    render();
  }
}

function renderSlotOptions() {
  els.slotOptions.textContent = "";
  (state.naturalSlots.length ? state.naturalSlots : Object.keys(NATURAL_SLOTS)).forEach(
    (name) => {
      const option = document.createElement("option");
      option.value = name;
      els.slotOptions.appendChild(option);
    }
  );
}

function bindEvents() {
  els.refreshBtn.addEventListener("click", () => load(state.date || null));
  els.prevBtn.addEventListener("click", () => load(shiftDate(state.date, -1)));
  els.nextBtn.addEventListener("click", () => {
    const next = shiftDate(state.date, 1);
    if (!state.tomorrow || next <= state.tomorrow) {
      load(next);
    }
  });
  els.todayBtn.addEventListener("click", () => load(state.today || null));
  els.dateInput.addEventListener("change", () => {
    let value = els.dateInput.value;
    if (!value) {
      return;
    }
    if (state.tomorrow && value > state.tomorrow) {
      value = state.tomorrow;
    }
    load(value);
  });
  els.addBtn.addEventListener("click", addEntry);
  els.saveBtn.addEventListener("click", save);
  els.saveWeatherBtn.addEventListener("click", saveWeather);
  els.resetWeatherBtn.addEventListener("click", resetWeather);
  els.resetBtn.addEventListener("click", async () => {
    if (state.saving) {
      return;
    }
    if (state.weatherDirty) {
      showToast("请先保存或放弃天气修改，再重置日程。", true);
      return;
    }
    if (
      state.dirty &&
      !(await askConfirm("放弃未保存的日程修改？", "放弃修改"))
    ) {
      return;
    }
    state.dirty = false;
    load(state.date || null, true);
  });
  els.confirmOk.addEventListener("click", () => settleConfirm(true));
  els.confirmCancel.addEventListener("click", () => settleConfirm(false));
  els.confirmDialog
    .querySelector(".modal__backdrop")
    .addEventListener("click", () => settleConfirm(false));
  els.holidayInput.addEventListener("input", () => {
    if (!isEditable()) return;
    state.draft.holiday = els.holidayInput.value;
    markDirty();
  });
  els.summaryInput.addEventListener("input", () => {
    if (!isEditable()) return;
    state.draft.schedule_summary = els.summaryInput.value;
    markDirty();
  });
  els.styleInput.addEventListener("input", () => {
    if (!isEditable()) return;
    state.draft.style_summary = els.styleInput.value;
    markDirty();
  });
  document.addEventListener("keydown", (event) => {
    if (!els.confirmDialog.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        settleConfirm(false);
      }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (state.weatherDirty && !state.dirty) {
        saveWeather();
      } else if (state.dirty) {
        save();
      }
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty && !state.weatherDirty) {
      return;
    }
    event.preventDefault();
    event.returnValue = "";
  });
}

async function boot() {
  bindEvents();
  if (!bridge) {
    showError("无法连接到 AstrBot WebUI 桥接，请从插件详情页或左侧导航打开本页面。");
    state.loading = false;
    render();
    return;
  }
  try {
    const context = await bridge.ready();
    applyContext(context);
    if (typeof bridge.onContext === "function") {
      bridge.onContext(applyContext);
    }
  } catch (error) {
    showError("初始化插件页面上下文失败，请刷新重试。");
  }
  await load(null);
}

boot();
