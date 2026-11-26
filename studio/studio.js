"use strict";

const stages = {
  question: { label: "Demande", sub: "Entrée utilisateur", glyph: "IN", x: 50, y: 18,
    about: "La question définit le périmètre de l’analyse et accompagne chaque appel au modèle." },
  model: { label: "Modèle", sub: "Décide du prochain outil", glyph: "AI", x: 50, y: 91,
    about: "Le LLM reçoit les règles métier Olist dans le prompt système, avec la demande, les outils disponibles et l’historique des échanges. Il consulte le schéma des tables utiles à la demande." },
  list_tables: { label: "list_tables", tool: true,
    about: "Liste les noms des tables et des vues SQLite. Le LLM peut utiliser ces noms dans describe_table ou dans une requête SQL." },
  describe_table: { label: "describe_table", tool: true,
    about: "Le LLM fournit le nom d’une table ou d’une vue. Le code vérifie qu’elle existe dans SQLite, puis renvoie ses colonnes, leurs types, les clés et jusqu’à trois lignes d’exemple." },
  get_data_dictionary: { label: "get_data_dictionary", tool: true, legacy: true,
    about: "Ancien outil qui chargeait le dictionnaire Olist complet." },
  run_sql: { label: "run_sql", tool: true,
    about: "Le LLM prépare une requête SQL. Le code vérifie qu’il s’agit d’un seul SELECT en lecture seule, puis l’exécute dans SQLite. Il stocke le résultat complet dans ResultStore et renvoie au LLM son identifiant et jusqu’à cinq lignes d’aperçu." },
  create_chart: { label: "create_chart", tool: true,
    about: "Le LLM choisit un résultat SQL, un type de graphique, les colonnes à tracer et un titre. Le code vérifie les paramètres et la compatibilité des données, puis génère le PNG avec Matplotlib. Le LLM reçoit l’identifiant du graphique pour l’inclure dans le rapport." },
  write_report: { label: "write_report", tool: true,
    about: "Le LLM rédige un brouillon structuré et fournit ses références aux résultats SQL et aux graphiques. Le code valide le format, résout les références et signale les chiffres non reliés à un résultat SQL. Une fois le rapport accepté, l’analyse se termine et la commande produit le PDF." },
  pdf: { label: "PDF", sub: "ReportLab", glyph: "PDF", local: true,
    about: "ReportLab transforme le rapport validé en PDF à la fin de l’analyse." },
  pdf_review: { label: "Juge visuel", sub: "Pages du PDF", glyph: "AI",
    about: "Un modèle multimodal examine les images des pages du PDF final et propose des corrections de mise en page." },
};

const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 2 });
const money = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 4, maximumFractionDigits: 6 });
const missing = "—";
const state = { events: [], index: 0, stage: "question", filter: "all", tab: "overview", timer: null };
const nodes = new Map();
const titleFor = (key) => stages[key].label;

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function duration(ms) {
  if (!Number.isFinite(ms)) return missing;
  return ms >= 1000 ? `${number.format(ms / 1000)} s` : `${number.format(ms)} ms`;
}

function eventStage(event) {
  if (event.event === "run_start") return "question";
  if (event.event === "model_call") return "model";
  if (event.event === "pdf_written") return "pdf";
  if (event.event === "pdf_review") return "pdf_review";
  if (event.event === "tool_call") {
    return Object.hasOwn(stages, event.name) && stages[event.name].tool ? event.name : "model";
  }
  return "write_report";
}

function endEvent() {
  return state.events.findLast((event) => ["run_end", "run_aborted"].includes(event.event));
}

function observed(key, event) {
  if (stages[key].tool) return event.event === "tool_call" && event.name === key;
  return eventStage(event) === key;
}

function setupMap() {
  for (const [key, stage] of Object.entries(stages)) {
    if (stage.legacy) continue;
    const button = element("button", "node");
    button.type = "button";
    button.dataset.stage = key;
    if (stage.tool) {
      button.dataset.tool = "";
      button.textContent = stage.label;
      const cell = element("div", "tool-cell");
      cell.append(button);
      byId("tool-row").append(cell);
    } else {
      const words = element("span");
      words.append(element("strong", "", stage.label), element("small", "", stage.sub));
      button.append(element("span", `glyph${stage.local ? " local" : ""}`, stage.glyph), words);
      if (key === "pdf" || key === "pdf_review") {
        nodes.get("write_report").parentElement.append(button);
      } else {
        button.style.setProperty("--x", `${stage.x}%`);
        button.style.setProperty("--y", `${stage.y}px`);
        byId("flow-map").insertBefore(button, byId("tool-row"));
      }
    }
    button.addEventListener("click", () => {
      stop();
      const at = state.events[state.index];
      const event = observed(key, at) ? at
        : state.events.slice(0, state.index + 1).findLast((entry) => observed(key, entry))
          || state.events.find((entry) => observed(key, entry));
      if (event) select(state.events.indexOf(event), key);
      else {
        state.stage = key;
        renderSelection();
      }
    });
    nodes.set(key, button);
  }
}

function displayStatus(status) {
  const labels = {
    completed: "Exécution terminée", step_limit: "Limite d’étapes", context_limit: "Limite de contexte",
    model_error: "Erreur du modèle", aborted: "Exécution interrompue", partial: "Trace partielle",
  };
  return Object.hasOwn(labels, status) ? labels[status] : (status || "Trace partielle");
}

function renderHeader() {
  const start = state.events.find((event) => event.event === "run_start");
  const end = endEvent();
  const status = end?.status || (end?.event === "run_aborted" ? "aborted" : "partial");
  byId("question").textContent = start?.question || "Question absente de cette trace.";
  const runStatus = byId("run-status");
  runStatus.textContent = `${state.source} · ${displayStatus(status)}`;
  runStatus.dataset.state = status;
  const values = [
    ["Appels au modèle d’analyse", end?.steps ?? state.events.filter((event) => event.event === "model_call").length],
    ["Appels d’outils", end?.tool_calls ?? state.events.filter((event) => event.event === "tool_call").length],
    ["Durée de l’analyse", duration(end?.duration_ms)],
    ["Tokens déclarés", Number.isFinite(end?.usage?.total_tokens) ? number.format(end.usage.total_tokens) : missing],
  ];
  byId("metrics").replaceChildren(...values.map(([label, value]) => {
    const metric = element("div", "metric");
    metric.append(element("span", "", label), element("strong", "", value));
    return metric;
  }));
}

function timelineLabel(event) {
  switch (event.event) {
    case "run_start": return ["Demande reçue", "Début de l’analyse", "IN"];
    case "model_call": return [`Tour modèle ${event.step ?? ""}`.trim(), event.model || "Appel au fournisseur", "AI"];
    case "tool_call": return [event.name || "Outil", event.output?.error ? "Erreur renvoyée au modèle" : `Tour ${event.step ?? "—"} · exécution locale`, "FN"];
    case "run_end": return [displayStatus(event.status), "Fin de l’analyse", "OK"];
    case "pdf_written": return ["PDF créé", event.filename, "PDF"];
    case "pdf_review": return [event.error ? "Relecture incomplète" : "Avis du juge visuel", event.model, "AI"];
    case "run_aborted": return ["Exécution interrompue", event.error_type, "!"];
    default: return [event.event, "Événement importé", "·"];
  }
}

function visibleIndexes() {
  return state.events.flatMap((event, index) => (
    state.filter === "all" || event.event === state.filter
      || (state.filter === "model_call" && event.event === "pdf_review") ? [index] : []
  ));
}

function renderTimeline() {
  const items = visibleIndexes();
  const timeline = byId("timeline");
  byId("event-count").textContent = `${items.length} sur ${state.events.length} événements`;
  timeline.replaceChildren();
  if (!items.length) {
    timeline.append(element("li", "empty", "Aucun événement pour ce filtre."));
    return;
  }
  for (const index of items) {
    const event = state.events[index];
    const [label, detail, glyph] = timelineLabel(event);
    const listItem = element("li");
    const item = element("button", "timeline-entry");
    item.type = "button";
    item.dataset.index = index;
    const description = element("span");
    description.append(element("strong", "", label), element("span", "event-detail", detail));
    item.append(
      element("span", `glyph${event.event === "tool_call" ? " local" : ""}`, glyph),
      description,
      element("span", "event-duration", Number.isFinite(event.duration_ms) ? duration(event.duration_ms) : ""),
    );
    item.addEventListener("click", () => { stop(); select(index); });
    listItem.append(item);
    timeline.append(listItem);
  }
}

function block(parent, label, content) {
  if (content === undefined || content === null || content === "") return;
  const section = element("section", "inspect-block");
  section.append(element("h4", "", label));
  section.append(typeof content === "string" ? element("p", "", content) : content);
  parent.append(section);
}

function code(text, language = "text") {
  const snippet = element("code", "snippet");
  highlightCode(snippet, String(text), language);
  const pre = element("pre");
  pre.append(snippet);
  return pre;
}

function json(value) {
  return code(JSON.stringify(value, null, 2), "json");
}

function renderPrompt(panel, event) {
  const request = event?.request ?? event;
  const messages = Array.isArray(request?.messages) ? request.messages : [];
  for (const [role, label] of [["system", "Système"], ["user", "Utilisateur (exemple de demande)"]]) {
    const content = messages.find((message) => message?.role === role)?.content;
    block(panel, label, typeof content === "string" ? code(content)
      : content == null ? "Message absent de la trace." : json(content));
  }

  if (Array.isArray(request?.tools) && request.tools.length) {
    block(panel, "Outils", json(request.tools));
  } else {
    block(panel, "Outils", event?.request || Array.isArray(request?.tools)
      ? "Aucun outil proposé pour cet appel." : "Schémas des outils absents de la trace.");
  }
}

function renderRequestJson(panel, event) {
  if (event.request) {
    panel.append(json(event.request));
    return;
  }

  const { messages, tools } = event;
  if (state.demo) {
    const request = { ...DEMO_REQUEST_SETTINGS, messages };
    if (tools?.length) request.tools = tools;
    panel.append(element("p", "", "Exemple de requête Mistral pour ce tour simulé."), json(request));
    return;
  }

  panel.append(element("p", "", "Cette trace contient les messages et les outils du tour. Le corps HTTP complet n’a pas été enregistré."));
  if (messages || tools) panel.append(json({ messages, tools }));
}

function findToolSchema(events, name) {
  for (const event of events) {
    if (event.event !== "model_call") continue;
    const specs = (event.request ?? event).tools;
    const schema = Array.isArray(specs) && specs.find((spec) => spec?.function?.name === name);
    if (schema) return schema;
  }
}

function renderToolSchema(panel, name, step) {
  const matchingCalls = state.events.filter((event) => event.step === step);
  const schema = findToolSchema(matchingCalls, name) ?? findToolSchema(state.events, name);
  if (schema) {
    panel.append(json(schema));
    return;
  }
  block(panel, "Schéma de la démonstration", json(findToolSchema(DEMO_TRACE, name)));
}

function toolCall(id, name, args) {
  return { id, type: "function", function: { name, arguments: args } };
}

function renderToolExchange(panel, call, response) {
  block(panel, "Appel de l’outil", json(call));
  block(panel, "Réponse de l’outil", response === undefined
    ? "Réponse absente de la trace." : json(response));
}

function renderToolExample(panel, name, event) {
  const inTrace = event?.event === "tool_call";
  const events = inTrace ? state.events : DEMO_TRACE;
  const example = inTrace ? event : events.find((entry) => entry.event === "tool_call" && entry.name === name);
  if (!example) {
    const { id, args, output } = DEMO_TOOL_EXAMPLES[name];
    panel.append(element("p", "", "Exemple illustratif."));
    renderToolExchange(panel, toolCall(id, name, JSON.stringify(args)),
      { role: "tool", tool_call_id: id, name, content: JSON.stringify(output) });
    return;
  }

  const parent = events.find((entry) => entry.event === "model_call" && entry.step === example.step);
  const calls = parent?.response?.tool_calls;
  const call = (Array.isArray(calls) && calls.find((item) => item?.id === example.tool_call_id))
    || toolCall(example.tool_call_id, name, example.arguments);
  const nextCalls = events.slice(events.indexOf(example) + 1);
  let response;
  for (const entry of nextCalls) {
    if (entry.event !== "model_call") continue;
    const messages = (entry.request ?? entry).messages;
    if (!Array.isArray(messages)) continue;
    response = messages.find((message) => message?.role === "tool"
      && message.tool_call_id === example.tool_call_id);
    if (response) break;
  }
  if (!inTrace) panel.append(element("p", "", "Exemple tiré de la démonstration."));
  if (!response && example.output !== undefined) {
    panel.append(element("p", "", "La réponse affichée est la sortie locale enregistrée."));
  }
  renderToolExchange(panel, call, response ?? example.output);
}

function chips(parent, values) {
  const container = element("div", "chips");
  for (const value of values.filter(Boolean)) container.append(element("span", "chip", value));
  if (container.childElementCount) parent.append(container);
}

function quotePowerShell(value) {
  const escape = String.fromCharCode(96);
  return '"' + value.replace(/[`$"“”„‟]/g, (char) => escape + char) + '"';
}

function renderLaunch(panel) {
  panel.append(element("h3", "", "Lancement du run"));
  const start = state.events.find((event) => event.event === "run_start");
  if (typeof start?.question !== "string" || !start.question.trim()) {
    panel.append(element("p", "", "La question de départ est absente de la trace."));
    return;
  }
  if (start.report_mode === false) {
    panel.append(element("p", "", "Cette trace correspond à une analyse texte lancée depuis Python."));
    block(panel, "Question", start.question);
    return;
  }

  const commandParts = ["uv run --locked --env-file .env report", "--out rapport-rejoue.pdf"];
  const activeModel = state.events.find((event) => event.event === "model_call"
    && typeof event.model === "string" && !event.model.startsWith("scripted"))?.model;
  if (activeModel) commandParts.push("--model=" + quotePowerShell(activeModel));
  const review = state.events.find((event) => event.event === "pdf_review");
  if (state.demo || review) commandParts.push("--review-pdf");
  if (typeof review?.model === "string") {
    commandParts.push("--review-model=" + quotePowerShell(review.model));
  }
  if (Number.isSafeInteger(start.max_steps) && start.max_steps > 0) {
    commandParts.push("--max-steps " + start.max_steps);
  }
  commandParts.push("-- " + quotePowerShell(start.question));
  const command = commandParts.join(" ");
  panel.append(element("p", "", state.demo
    ? "La démo utilise un client scripté. Cette commande relance la même question avec Mistral."
    : "Commande reconstituée à partir des paramètres disponibles dans la trace."));
  const preview = code(command, "powershell");
  block(panel, "Commande PowerShell", preview);
  panel.append(element("p", "", "L’option --max-steps limite les appels au modèle. Un appel peut demander plusieurs outils."));
  const actions = element("div", "launch-actions");
  const copy = element("button", "quiet-button", "Copier la commande");
  copy.type = "button";
  const status = element("span", "copy-status");
  status.setAttribute("role", "status");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(command);
      status.textContent = "Commande copiée.";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(preview);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Commande sélectionnée. Appuie sur Ctrl+C.";
    }
  });
  actions.append(copy, status);
  panel.append(actions);
}

function renderPdfReview(panel, event) {
  if (!event) {
    block(panel, "Relecture visuelle", "Lance la commande avec --review-pdf, puis importe sa trace.");
    return;
  }
  chips(panel, [event.model, duration(event.duration_ms)]);
  if (event.error) block(panel, "Relecture incomplète", event.error);
  if (event.review) {
    const review = element("p", "review-text", event.review);
    block(panel, "Avis du juge", review);
  }
}

function renderPdfPages(panel, event) {
  if (!Array.isArray(event?.pages) || !event.pages.length) {
    panel.append(element("p", "", "Aucune image de page dans cet événement."));
    return;
  }
  const list = element("div", "review-pages");
  for (const page of event.pages) {
    if (!Number.isSafeInteger(page?.number) || page.number < 1) continue;
    if (typeof page.image !== "string" || !/^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(page.image)) continue;
    const label = `Page ${page.number}`;
    const button = element("button", "review-page");
    button.type = "button";
    const image = element("img");
    image.src = page.image;
    image.alt = "";
    image.loading = "lazy";
    button.append(image, element("span", "", label));
    button.addEventListener("click", () => {
      const dialog = byId("review-dialog");
      const preview = byId("review-preview");
      preview.alt = label;
      preview.src = page.image;
      byId("review-page-title").textContent = label;
      dialog.showModal();
    });
    list.append(button);
  }
  const heading = Number.isSafeInteger(event.total_pages)
    ? `Pages du lot · ${event.total_pages} au total` : "Pages du lot";
  if (list.childElementCount) block(panel, heading, list);
}

function renderInspector() {
  const stage = stages[state.stage];
  const tabs = byId("tabs").querySelectorAll("button");
  for (const button of tabs) {
    const { stage: scope, tab } = button.dataset;
    if (tab === "json") button.textContent = state.stage === "model" ? "Exemple d’appel LLM" : "JSON";
    button.hidden = (scope === "tool" && !stage.tool)
      || (tab === "json" && Boolean(stage.tool || ["pdf", "pdf_review"].includes(state.stage)))
      || Boolean(scope && scope !== "tool" && scope !== state.stage);
    if (button.hidden && tab === state.tab) state.tab = "overview";
  }
  for (const button of tabs) {
    button.setAttribute("aria-pressed", String(button.dataset.tab === state.tab));
  }

  const panel = byId("inspector");
  const selected = state.events[state.index];
  const event = !observed(state.stage, selected)
    ? (selected.event === "run_end" && state.stage === "write_report" ? selected : null) : selected;
  byId("selection-label").textContent = stage.label + (event?.step ? ` · tour ${event.step}` : "");
  panel.replaceChildren();
  panel.scrollTop = 0;
  if (state.tab === "launch") {
    byId("selection-label").textContent = "Exécution sélectionnée";
    renderLaunch(panel);
    return;
  }
  if (state.tab === "prompt") {
    renderPrompt(panel, event);
    return;
  }
  if (state.tab === "reportlab") {
    const intro = element("p");
    intro.append("Dans notre code, ", element("code", "", "write_report"), " reçoit la clé JSON ",
      element("code", "", "title"), ". Après validation, elle devient ",
      element("code", "", "report.title"), " dans un objet Python. On associe ensuite explicitement ce texte à un style ReportLab :");
    const detail = element("p");
    detail.append("Le style ", element("code", "", "title"), " définit la police, la taille (33 points) et l’espacement. Python assemble de la même façon les paragraphes, les tableaux et les images dans l’ordre voulu. ReportLab met ces blocs en page et génère le PDF.");
    panel.append(intro, code('_paragraph(report.title, styles["title"])'), detail);
    return;
  }
  if (state.tab === "pdf-pages") {
    renderPdfPages(panel, event);
    return;
  }
  if (state.tab === "tool-schema") {
    renderToolSchema(panel, state.stage, event?.step);
    return;
  }
  if (state.tab === "tool-example") {
    renderToolExample(panel, state.stage, event);
    return;
  }
  if (state.tab === "json") {
    if (event?.event === "model_call") renderRequestJson(panel, event);
    else panel.append(event ? json(event) : element("p", "", "Aucun événement JSON enregistré pour cette étape."));
    return;
  }
  if (event?.event === "run_end") {
    panel.append(element("h3", "", "Fin de l’analyse"));
  } else {
    panel.append(element("h3", "", stage.label), element("p", "", stage.about));
    if (stage.tool) return;
  }
  if (state.stage === "pdf") {
    block(panel, "État", event?.filename || (state.demo ? "Le PDF a été généré pour cette démonstration."
      : "La trace JSONL importée ne permet pas de confirmer qu’un fichier PDF a été écrit."));
    return;
  }
  if (state.stage === "pdf_review") {
    renderPdfReview(panel, event);
    return;
  }
  if (!event) {
    block(panel, "Trace", "Aucun appel enregistré pour cette étape.");
    return;
  }
  switch (event.event) {
    case "run_start":
      block(panel, "Question", event.question);
      chips(panel, [
        event.max_steps && `${event.max_steps} appels au modèle max.`,
      ]);
      if (event.max_steps) {
        panel.append(element("p", "", "Un appel au modèle peut demander plusieurs outils. Les appels d’outils sont comptés séparément."));
      }
      break;
    case "model_call":
      chips(panel, [event.model, duration(event.duration_ms), event.finish_reason]);
      if (Array.isArray(event.response?.tool_calls)) {
        block(panel, "Outils demandés", event.response.tool_calls.map((call) => call?.function?.name).filter(Boolean).join(", "));
      }
      if (event.error) block(panel, "Erreur", event.error);
      if (event.usage) block(panel, "Compteurs de tokens", json(event.usage));
      break;
    case "run_end":
      block(panel, "Synthèse après validation", event.answer);
      block(panel, "Erreur", event.error);
      if (Number.isFinite(event.estimated_cost_usd)) block(panel, "Coût estimé", `${money.format(event.estimated_cost_usd)} USD`);
      if (Array.isArray(event.number_audit?.literals)) {
        const flagged = event.number_audit.literals.filter((entry) => entry && !entry.supported);
        block(panel, "Chiffres à vérifier", flagged.length ? flagged.map((entry) => entry.literal).join(", ") : "Aucun chiffre signalé.");
      }
      break;
    default:
      panel.append(json(event));
  }
}

function renderSelection() {
  for (const item of byId("timeline").querySelectorAll("button")) {
    if (Number(item.dataset.index) === state.index) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
  for (const [key, node] of nodes) {
    node.setAttribute("aria-pressed", String(state.stage === key));
    node.dataset.observed = String((key === "pdf" && state.demo) || state.events.some((event) => observed(key, event)));
  }
  byId("position").textContent = `Événement ${state.index + 1} / ${state.events.length}`;
  byId("previous").disabled = state.index === 0;
  byId("next").disabled = state.index === state.events.length - 1;
  byId("play").disabled = state.events.length < 2;
  renderInspector();
}

function select(index, stage = eventStage(state.events[index])) {
  state.index = index;
  state.stage = stage;
  renderSelection();
}

function stop() {
  clearInterval(state.timer);
  state.timer = null;
  byId("play").textContent = "▷";
  byId("play").setAttribute("aria-label", "Lire la trace");
  byId("play").title = "Lire la trace";
}

function step(increment, scroll = false) {
  select(Math.min(state.events.length - 1, Math.max(0, state.index + increment)));
  if (scroll) {
    const timeline = byId("timeline");
    const selected = timeline.querySelector('[aria-current="step"]');
    if (selected) {
      const source = selected.getBoundingClientRect();
      const viewport = timeline.getBoundingClientRect();
      if (source.bottom > viewport.bottom) timeline.scrollTop += source.bottom - viewport.bottom;
      if (source.top < viewport.top) timeline.scrollTop -= viewport.top - source.top;
    }
  }
}

function loadTrace(events, source, demo = false) {
  stop();
  state.events = events;
  state.source = source;
  state.demo = demo;
  state.filter = "all";
  const exampleIndex = demo
    ? events.findIndex((event) => event.event === "model_call" && event.step === 4)
    : -1;
  state.tab = exampleIndex >= 0 ? "json" : "overview";
  for (const button of byId("filters").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter));
  }
  byId("import-error").hidden = true;
  renderHeader();
  renderTimeline();
  select(Math.max(0, exampleIndex));
}

function parseTrace(raw) {
  const text = raw.replace(/^\uFEFF/, "").trim();
  const records = text.startsWith("[") ? JSON.parse(text) : text.split(/\r?\n/).flatMap((line, index) => {
    if (!line.trim()) return [];
    try { return [JSON.parse(line)]; }
    catch { throw new Error(`JSON invalide à la ligne ${index + 1}.`); }
  });
  if (!Array.isArray(records) || !records.length || records.some((record) => (
    !record || typeof record !== "object" || typeof record.event !== "string"
  ))) throw new Error("Le fichier doit contenir des événements de trace JSONL.");
  return records;
}

setupMap();
loadTrace(DEMO_TRACE, "Exemple Olist", true);
byId("demo-button").addEventListener("click", () => loadTrace(DEMO_TRACE, "Exemple Olist", true));
byId("trace-file").addEventListener("change", async (event) => {
  const input = event.currentTarget;
  const file = input.files[0];
  if (!file) return;
  try {
    if (file.size > 16_000_000) throw new Error("Le fichier dépasse la limite d’import de 16 Mo.");
    loadTrace(parseTrace(await file.text()), file.name);
  } catch (error) {
    byId("import-error").textContent = error.message;
    byId("import-error").hidden = false;
  } finally {
    input.value = "";
  }
});
byId("filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  for (const choice of byId("filters").querySelectorAll("button")) choice.setAttribute("aria-pressed", String(choice === button));
  renderTimeline();
  renderSelection();
});
byId("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  state.tab = button.dataset.tab;
  renderInspector();
});
byId("previous").addEventListener("click", () => { stop(); step(-1, true); });
byId("next").addEventListener("click", () => { stop(); step(1, true); });
byId("play").addEventListener("click", () => {
  if (state.timer !== null) { stop(); return; }
  if (state.index === state.events.length - 1) select(0);
  byId("play").textContent = "Ⅱ";
  byId("play").setAttribute("aria-label", "Mettre en pause");
  byId("play").title = "Mettre en pause";
  state.timer = setInterval(() => {
    step(1, true);
    if (state.index === state.events.length - 1) stop();
  }, 1050);
});
