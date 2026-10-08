/**
 * Sqwakvox web presenter.
 *
 * The browser has no private API: every action is an MCP tool call against the
 * in-process `sqwakvox-presenter` server (POST /api/call), and every state
 * change arrives as a session event over SSE (/api/events). Adding a tool to
 * `sqwakvox.mcp_presenter` therefore adds it to this UI for free — the generic
 * tool console builds its form from the tool's JSON Schema.
 */

const state = {
  token: sessionStorage.getItem("sqwakvox_token") || "",
  session: null,
  tools: [],
  documents: [],
  skills: [],
  activeSource: null,
  currentJob: null,
  lastSeq: 0,
  pendingChat: 0,
  view: "document",
  eventSource: null,
};

/* ------------------------------------------------------------------ api --- */

function headers(json = false) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (state.token) h["Authorization"] = `Bearer ${state.token}`;
  return h;
}

/** Call an MCP presenter tool. Throws on transport failure. */
async function callTool(tool, args = {}) {
  const response = await fetch("/api/call", {
    method: "POST",
    headers: headers(true),
    body: JSON.stringify({ tool, arguments: args }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    showGate("Token rejected. Try again.");
    throw new Error("unauthorized");
  }
  if (body.ok === false && body.error) {
    throw new Error(body.error);
  }
  // Tools answer with {"ok": true, ...} inside `data`; a false `ok` there is a
  // tool-level failure the user should see, not a transport error.
  const data = body.data ?? {};
  if (data.ok === false) throw new Error(data.error || `Tool ${tool} failed`);
  return data;
}

/* ----------------------------------------------------------------- dom --- */

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...children) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const child of children.flat()) {
    if (child != null) node.append(child.nodeType ? child : String(child));
  }
  return node;
};

function toast(message, isError = false) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", isError);
  node.classList.remove("hidden");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => node.classList.add("hidden"), isError ? 8000 : 3500);
}

function setStatus(text) {
  $("status-text").textContent = text;
}

/* ---------------------------------------------------------------- boot --- */

async function boot() {
  try {
    const health = await (await fetch("/healthz", { headers: headers() })).json();
    if (health.auth_required && !state.token) {
      showGate();
      return;
    }
  } catch (error) {
    showGate("Cannot reach the presenter.");
    return;
  }
  $("gate").classList.add("hidden");
  $("app").classList.remove("hidden");
  wireEvents();
  await refreshCatalogs();
  await refreshSession();
  await connectEvents();
  $("chat-input").focus();
}

function showGate(message = "") {
  $("gate").classList.remove("hidden");
  $("app").classList.add("hidden");
  $("gate-error").textContent = message;
  $("gate-token").focus();
}

/* ------------------------------------------------------------ catalogs --- */

async function refreshCatalogs() {
  const [domains, models] = await Promise.all([
    callTool("sqwakvox_presenter_list_domains"),
    callTool("sqwakvox_presenter_list_models"),
  ]);
  fillSelect($("domain"), domains.domains, (d) => [d.domain_id, `${d.display_name} — ${d.description}`]);
  fillSelect($("model"), models.models, (m) => [
    m.model_id,
    m.configured ? `${m.friendly_name} (${m.env_var} ✓)` : m.friendly_name,
  ]);
  await loadTools();
  await refreshSkills();
}

function fillSelect(select, items, toEntry) {
  select.replaceChildren();
  for (const item of items) {
    const [value, label] = toEntry(item);
    select.append(el("option", { value, textContent: label }));
  }
}

async function loadTools() {
  try {
    const response = await fetch("/api/tools", { headers: headers() });
    const body = await response.json();
    state.tools = body.tools || [];
    state.session = body.session || state.session;
    buildToolConsole();
  } catch (error) {
    console.warn("tool catalog unavailable", error);
  }
}

async function refreshSkills() {
  try {
    const data = await callTool("sqwakvox_presenter_list_skills", {
      domain_id: $("domain").value,
    });
    state.skills = data.skills || [];
  } catch {
    state.skills = [];
  }
  const list = $("skill-list");
  list.replaceChildren();
  if (!state.skills.length) {
    list.append(el("li", { textContent: "No skills stored yet." }));
    return;
  }
  for (const skill of state.skills) {
    list.append(
      el(
        "li",
        {},
        el("strong", { textContent: skill.name }),
        el("span", { className: "meta", textContent: skill.description || "" }),
      ),
    );
  }
}

async function refreshSession() {
  const data = await callTool("sqwakvox_presenter_status");
  state.session = data.session;
  state.documents = data.session.documents || [];
  state.activeSource = data.session.active_source;
  renderDocuments();
  renderMcpServers(data.session.mcp_servers || []);
  renderPager();
}

/* ------------------------------------------------------------ documents --- */

function renderDocuments() {
  const list = $("doc-list");
  list.replaceChildren();
  if (!state.documents.length) {
    list.append(el("li", { textContent: "No documents loaded." }));
    return;
  }
  for (const doc of state.documents) {
    const item = el(
      "li",
      { className: doc.active ? "active" : "", title: doc.source },
      el("span", { textContent: doc.file_name }),
      el("span", {
        className: "meta",
        textContent: `${doc.domain_id} · ${doc.table_count} tables · ${doc.char_count} chars`,
      }),
    );
    item.onclick = () => activateDocument(doc.source);
    list.append(item);
  }
  renderTabs();
}

function renderTabs() {
  const tabs = $("tabs");
  tabs.replaceChildren();
  for (const doc of state.documents) {
    const button = el(
      "button",
      { className: doc.active ? "active" : "", title: doc.source },
      doc.file_name,
    );
    button.onclick = () => activateDocument(doc.source);
    const close = el("span", { className: "close", textContent: "✕" });
    close.onclick = async (event) => {
      event.stopPropagation();
      await callTool("sqwakvox_presenter_close_document", { source: doc.source }).catch(
        (error) => toast(error.message, true),
      );
    };
    button.append(close);
    tabs.append(button);
  }
}

function renderMcpServers(servers) {
  const list = $("mcp-list");
  list.replaceChildren();
  if (!servers.length) {
    list.append(el("li", { textContent: "No MCP servers configured (mcp_servers.json)." }));
    return;
  }
  for (const server of servers) {
    list.append(
      el(
        "li",
        {},
        el("span", { className: "dot", textContent: "● " }),
        el("strong", { textContent: server.name }),
        el("span", { className: "meta", textContent: server.command || server.url || "" }),
      ),
    );
  }
}

function renderPager() {
  const doc = state.documents.find((d) => d.source === state.activeSource);
  const pager = $("pager");
  pager.replaceChildren();
  const info = doc?.page_range;
  if (!info || !info.paged) {
    setStatus(
      state.documents.length
        ? `${state.session?.active_document_name || ""} — ${state.session?.context_chars || 0} chars of agent context`
        : "ready",
    );
    return;
  }
  pager.append(
    el("span", {
      textContent: info.total_pages
        ? `pages 1–${info.rendered_pages} of ${info.total_pages}`
        : `${info.rendered_pages} pages loaded`,
    }),
  );
  const more = el("button", { textContent: info.complete ? "All pages ✓" : "Load more ↓" });
  more.disabled = Boolean(info.complete);
  more.onclick = () => callTool("sqwakvox_presenter_load_more", { source: doc.source }).catch((e) => toast(e.message, true));
  pager.append(more);
}

async function activateDocument(source) {
  if (source === state.activeSource) return;
  await callTool("sqwakvox_presenter_activate_document", { source }).catch((e) =>
    toast(e.message, true),
  );
  await Promise.all([refreshSession(), renderDocument(), renderChat()]);
}

async function openDocument() {
  const source = $("source").value.trim();
  if (!source) return;
  const button = $("btn-open");
  button.disabled = true;
  setStatus(`ingesting ${source}…`);
  try {
    const job = await callTool("sqwakvox_presenter_open_document", {
      source,
      domain_id: $("domain").value,
      crawl: $("crawl").checked,
    });
    state.currentJob = job.job_id;
    $("btn-cancel").disabled = false;
  } catch (error) {
    toast(error.message, true);
    setStatus("ingest failed");
  } finally {
    button.disabled = false;
  }
}

async function renderDocument() {
  const view = $("document-view");
  if (!state.activeSource) {
    view.textContent = "Load a document to see it rendered here.";
    return;
  }
  try {
    const data = await callTool("sqwakvox_presenter_document", { source: state.activeSource });
    view.textContent = data.document.rendered || "(empty document)";
  } catch (error) {
    view.textContent = `Could not render document: ${error.message}`;
  }
}

/* ----------------------------------------------------------------- chat --- */

function renderChat() {
  const log = $("chat-log");
  log.replaceChildren();
  if (!state.activeSource) return;
  return callTool("sqwakvox_presenter_chat", { source: state.activeSource }).then((data) => {
    for (const message of data.messages || []) appendMessage(message);
    scrollChat();
  });
}

function appendMessage(message) {
  if (!message || !message.role) return;
  const bubble = el("div", { className: `msg ${message.role} level-${message.level || "info"}` });
  bubble.append(el("span", { className: "who", textContent: message.role }));
  bubble.append(document.createTextNode(message.text || ""));
  $("chat-log").append(bubble);
  scrollChat();
  if (message.role === "agent") {
    $("agent-view").append(document.createTextNode((message.text || "") + "\n\n"));
  }
}

function scrollChat() {
  const log = $("chat-log");
  log.scrollTop = log.scrollHeight;
}

function addPendingMessage(text) {
  const node = el("div", { className: "msg system pending", textContent: text });
  $("chat-log").append(node);
  scrollChat();
  return node;
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("chat-input");
  const query = input.value.trim();
  if (!query || !state.activeSource) return;
  input.value = "";
  appendMessage({ role: "user", text: query });
  const pending = addPendingMessage("Agent is thinking…");
  state.pendingChat += 1;
  try {
    const job = await callTool("sqwakvox_presenter_ask", {
      query,
      model_id: $("model").value,
      source: state.activeSource,
      api_key: $("api-key").value.trim(),
    });
    state.currentJob = job.job_id;
    $("btn-cancel").disabled = false;
  } catch (error) {
    pending.remove();
    toast(error.message, true);
    state.pendingChat -= 1;
  }
}

/* ----------------------------------------------------------------- sse --- */

function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  const url = new URL("/api/events", location.origin);
  url.searchParams.set("since_seq", state.lastSeq || 0);
  const source = new EventSource(url, { withCredentials: false });
  state.eventSource = source;

  source.onopen = () => setConn(true);
  source.onerror = () => setConn(false);

  for (const kind of ["hello", "job", "document", "chat", "progress", "log", "render", "error"]) {
    source.addEventListener(kind, (event) => handleEvent(kind, JSON.parse(event.data)));
  }
}

function setConn(live) {
  const node = $("conn");
  node.textContent = live ? "live" : "reconnecting…";
  node.className = `conn ${live ? "live" : "down"}`;
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    await refreshSession().catch(() => {});
    await renderChat().catch(() => {});
  }, 120);
}

async function handleEvent(kind, payload) {
  if (payload?.seq) state.lastSeq = Math.max(state.lastSeq, payload.seq);
  switch (kind) {
    case "hello":
      state.session = payload.session;
      renderDocuments();
      renderPager();
      await Promise.all([renderDocument(), renderChat()]);
      break;
    case "progress":
    case "log":
      addPendingMessage(payload.message);
      break;
    case "error":
      state.pendingChat = Math.max(0, state.pendingChat - 1);
      addPendingMessage(`✗ ${payload.message}`);
      toast(payload.message, true);
      setStatus(`error: ${payload.message}`);
      break;
    case "job":
      handleJobEvent(payload.job);
      break;
    case "document":
      scheduleRefresh();
      if (payload.document?.some((d) => d.source === state.activeSource)) await renderDocument();
      break;
    case "render":
      renderPager();
      await renderDocument();
      break;
    case "chat":
      if (payload.cleared) await renderChat();
      else if (payload.message) {
        state.pendingChat = Math.max(0, state.pendingChat - 1);
        $("chat-log").querySelectorAll(".msg.pending").forEach((n) => n.remove());
        appendMessage(payload.message);
      }
      break;
    default:
      break;
  }
}

function handleJobEvent(job) {
  if (!job) return;
  if (state.currentJob === job.job_id && ["success", "failure", "cancelled"].includes(job.state)) {
    state.currentJob = null;
    $("btn-cancel").disabled = true;
  }
  if (job.kind === "open_document") {
    setStatus(
      job.state === "running"
        ? "ingesting…"
        : job.state === "success"
          ? `loaded ${job.result?.file_name || job.source}`
          : job.state === "failure"
            ? "ingest failed"
            : "ingest cancelled",
    );
  }
  scheduleRefresh();
}

/* ------------------------------------------------------- tool console --- */

function buildToolConsole() {
  const picker = $("tool-picker");
  picker.replaceChildren();
  for (const tool of state.tools) {
    picker.append(el("option", { value: tool.name, textContent: tool.name }));
  }
  picker.onchange = () => renderToolForm();
  renderToolForm();
}

function renderToolForm() {
  const name = $("tool-picker").value;
  const tool = state.tools.find((t) => t.name === name);
  $("tool-desc").textContent = tool?.description || "";
  const container = $("tool-args");
  container.replaceChildren();
  const schema = tool?.input_schema || {};
  for (const [key, spec] of Object.entries(schema.properties || {})) {
    const required = (schema.required || []).includes(key);
    const label = el("label", { className: "field" });
    label.append(el("span", { textContent: `${key}${required ? " *" : ""} (${spec.type || "any"})` }));
    const input = el("input", { type: "text", dataset: { arg: key } });
    input.placeholder = spec.default === undefined ? "" : String(spec.default);
    if (spec.enum) {
      input.setAttribute("list", `enum-${key}`);
      label.append(input, el("datalist", { id: `enum-${key}` }, spec.enum.map((v) => el("option", { value: v }))));
    } else {
      label.append(input);
    }
    container.append(label);
  }
}

async function runTool(event) {
  event.preventDefault();
  const name = $("tool-picker").value;
  const args = {};
  for (const input of $("tool-args").querySelectorAll("input[data-arg]")) {
    const value = input.value.trim();
    if (value !== "") args[input.dataset.arg] = value;
  }
  const output = $("tool-output");
  output.textContent = "running…";
  try {
    const data = await callTool(name, args);
    output.textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    output.textContent = `Error: ${error.message}`;
  }
}

/* --------------------------------------------------------------- wiring --- */

function wireEvents() {
  $("gate-form").onsubmit = (event) => {
    event.preventDefault();
    state.token = $("gate-token").value.trim();
    sessionStorage.setItem("sqwakvox_token", state.token);
    boot();
  };

  $("btn-open").onclick = openDocument;
  $("btn-cancel").onclick = async () => {
    if (!state.currentJob) return;
    await callTool("sqwakvox_presenter_cancel", { job_id: state.currentJob }).catch((e) =>
      toast(e.message, true),
    );
  };
  $("btn-clear-chat").onclick = () =>
    callTool("sqwakvox_presenter_clear_chat", {}).catch((e) => toast(e.message, true));
  $("btn-xvalidate").onclick = async () => {
    if (!state.activeSource) return toast("Load a document first", true);
    addPendingMessage("Cross-validating table numbers…");
    try {
      const job = await callTool("sqwakvox_presenter_cross_validate_async", {
        source: state.activeSource,
      });
      state.currentJob = job.job_id;
      $("btn-cancel").disabled = false;
    } catch (error) {
      toast(error.message, true);
    }
  };
  $("chat-form").onsubmit = sendChat;

  $("domain").onchange = refreshSkills;
  $("api-key").oninput = () => {
    $("key-hint").textContent = $("api-key").value ? "in memory for this tab" : "";
  };

  $("chat-input").onkeydown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  };

  for (const button of $("view-tabs").querySelectorAll("button")) {
    button.onclick = () => {
      state.view = button.dataset.view;
      for (const sibling of $("view-tabs").querySelectorAll("button")) {
        sibling.classList.toggle("active", sibling === button);
      }
      $("document-view").classList.toggle("hidden", state.view !== "document");
      $("agent-view").classList.toggle("hidden", state.view !== "agent");
    };
  }

  $("tool-run").onclick = runTool;
  document.addEventListener("keydown", (event) => {
    if (event.key === "F2") {
      event.preventDefault();
      $("tools-dialog").showModal();
    }
  });
  setStatus("press F2 for the MCP tool console");
}

boot();