"use strict";

const stages = {
  question: {
    label: "Demande",
    sub: "Entrée utilisateur",
    glyph: "IN",
    x: 50,
    y: 18,
    about:
      "La question définit le périmètre de l’analyse et accompagne chaque appel au modèle.",
  },
  model: {
    label: "Construct",
    llm: true,
    sub: "Construit le rapport",
    glyph: "AI",
    x: 50,
    y: 91,
    about:
      "Le LLM reçoit les règles métier Olist dans le prompt système, avec la demande, les outils disponibles et l’historique des échanges. Il consulte le schéma des tables utiles à la demande.",
  },
  list_tables: {
    label: "list_tables",
    tool: true,
    about:
      "Permet au modèle de découvrir les tables et les vues disponibles dans SQLite.",
    context:
      "Réponse intégrale. Tous les noms des tables et vues applicatives sont ajoutés à l’historique du modèle.",
  },
  describe_table: {
    label: "describe_table",
    tool: true,
    about: "Décrit une table ou une vue SQLite à partir de son nom.",
    context:
      "Réponse limitée au schéma par défaut. Le modèle reçoit les colonnes, leurs types et les clés. Avec sample_columns, il reçoit aussi jusqu’à trois lignes pour les colonnes demandées. Les textes d’exemple sont tronqués à 200 caractères.",
  },
  get_data_dictionary: {
    label: "get_data_dictionary",
    tool: true,
    legacy: true,
    about: "Ancien outil de consultation du dictionnaire Olist.",
    context:
      "Réponse intégrale. Le texte complet du dictionnaire était ajouté à l’historique, puis renvoyé aux appels suivants.",
  },
  run_sql: {
    label: "run_sql",
    tool: true,
    about:
      "Vérifie et exécute une requête SELECT en lecture seule dans SQLite.",
    context:
      "Aperçu compact. Le résultat complet reste dans ResultStore. Le modèle reçoit son identifiant, les colonnes, leurs types, le nombre total de lignes et les cinq premières lignes au maximum. Les textes de l’aperçu sont tronqués à 200 caractères.",
  },
  create_chart: {
    label: "create_chart",
    tool: true,
    about:
      "Génère un graphique PNG à partir d’un résultat SQL avec Matplotlib.",
    context:
      "Métadonnées seules. Le modèle reçoit les identifiants du graphique et du résultat SQL, ainsi que le nombre de lignes tracées et le nombre de lignes écartées pour valeurs manquantes. Le PNG reste sur disque.",
  },
  write_report: {
    label: "write_report",
    tool: true,
    about:
      "Valide le brouillon et résout les références SQL avant le rendu PDF. L’outil refuse les nombres saisis directement et indique les champs à corriger.",
    context:
      "Après validation, le juge des données peut contrôler la synthèse avant le rendu. Si un juge demande une correction, Construct reçoit son avis dans le même fil et peut rappeler write_report. Si le brouillon est invalide, Construct reçoit l’erreur de validation.",
  },
  data_review: {
    label: "Juge des données",
    llm: true,
    sub: "Vérifie les valeurs",
    glyph: "AI",
    about:
      "Vérifie les chiffres et les conclusions de la synthèse avec des requêtes SQLite en lecture seule avant le rendu du PDF.",
  },
  pdf: {
    label: "PDF",
    sub: "ReportLab",
    glyph: "PDF",
    local: true,
    about:
      "ReportLab transforme le rapport validé en PDF à la fin de l’analyse.",
  },
  pdf_review: {
    label: "Juge visuel",
    llm: true,
    sub: "Vérifie cohérence visuel",
    glyph: "AI",
    about:
      "Un modèle multimodal examine les images des pages du PDF. Un défaut de lisibilité déclenche une tentative de correction par le modèle d’analyse, suivie d’une nouvelle relecture.",
  },
};

const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 2 });
const money = new Intl.NumberFormat("fr-FR", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});
const missing = "—";
const state = {
  events: [],
  index: 0,
  dataRevisionIndex: -1,
  revisionIndex: -1,
  stage: "question",
  filter: "all",
  tab: "overview",
  timer: null,
};
const nodes = new Map();
const titleFor = (key) => stages[key].label;
const revisionArrows = [
  {
    id: "data-revision",
    stage: "data_review",
    index: "dataRevisionIndex",
    railInset: 28,
    targetOffset: 15,
  },
  {
    id: "revision",
    stage: "pdf_review",
    index: "revisionIndex",
    railInset: 12,
    targetOffset: 0,
  },
];

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function duration(ms) {
  if (!Number.isFinite(ms)) return missing;
  return ms >= 1000
    ? `${number.format(ms / 1000)} s`
    : `${number.format(ms)} ms`;
}

function eventStage(event) {
  if (event.event === "run_start") return "question";
  if (event.event === "model_call") return "model";
  if (event.event === "pdf_written" || event.event === "pdf_revision")
    return "pdf";
  if (event.event === "pdf_review") return "pdf_review";
  if (event.event === "data_review") return "data_review";
  if (event.event === "tool_call") {
    return Object.hasOwn(stages, event.name) && stages[event.name].tool
      ? event.name
      : "model";
  }
  return "write_report";
}

function endEvent() {
  return state.events.findLast((event) =>
    ["run_end", "run_aborted"].includes(event.event),
  );
}

function observed(key, event) {
  if (stages[key].tool)
    return event.event === "tool_call" && event.name === key;
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
      words.append(
        element("strong", "", stage.label),
        element("small", "", stage.sub),
      );
      button.append(
        element("span", `glyph${stage.local ? " local" : ""}`, stage.glyph),
        words,
      );
      if (["data_review", "pdf", "pdf_review"].includes(key)) {
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
      const event = observed(key, at)
        ? at
        : state.events
            .slice(0, state.index + 1)
            .findLast((entry) => observed(key, entry)) ||
          state.events.find((entry) => observed(key, entry));
      if (event) select(state.events.indexOf(event), key);
      else {
        state.stage = key;
        renderSelection();
      }
    });
    nodes.set(key, button);
  }
  for (const arrow of revisionArrows) {
    const link = byId(`${arrow.id}-link`);
    function openRevision() {
      stop();
      const index = state[arrow.index];
      if (index >= 0) {
        state.tab = "turn";
        select(index);
      } else {
        state.tab = "overview";
        nodes.get(arrow.stage).click();
      }
    }
    link.addEventListener("click", openRevision);
    link.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      openRevision();
    });
  }
  byId("flow-map").parentElement.addEventListener("click", (event) => {
    if (event.target.closest(".node, .revision-link")) return;
    stop();
    state.stage = null;
    state.tab = "overview";
    renderSelection();
  });
  drawArrows();
  const resize = new ResizeObserver(drawArrows);
  resize.observe(byId("flow-map"));
  resize.observe(nodes.get("write_report").parentElement);
}

function drawArrows() {
  const map = byId("flow-map").getBoundingClientRect();
  const radius = Math.max(
    0,
    Math.min(
      28,
      ...revisionArrows.map(
        ({ stage, railInset }) =>
          map.right -
          railInset -
          nodes.get(stage).getBoundingClientRect().right -
          4,
      ),
    ),
  );
  revisionArrows.forEach((arrow) => drawRevisionArrow(arrow, map, radius));
}

function drawRevisionArrow(
  { id, stage, railInset, targetOffset },
  map,
  radius,
) {
  const judge = nodes.get(stage).getBoundingClientRect();
  const model = nodes.get("model").getBoundingClientRect();
  const rail = map.width - railInset;
  const startX = judge.right - map.left + 4;
  const startY = judge.top - map.top + judge.height / 2;
  const endX = model.right - map.left + 4;
  const endY = model.top - map.top + model.height / 2 + targetOffset;
  byId(`${id}-curve`).setAttribute(
    "d",
    [
      `M ${startX} ${startY} H ${rail - radius}`,
      `Q ${rail} ${startY} ${rail} ${startY - radius}`,
      `V ${endY + radius} Q ${rail} ${endY} ${rail - radius} ${endY}`,
      `H ${endX}`,
    ].join(" "),
  );
  byId(`${id}-tip`).setAttribute(
    "d",
    `M ${endX + 5} ${endY - 3.5} L ${endX} ${endY} l 5 3.5`,
  );
}

function displayStatus(status) {
  const labels = {
    completed: "Exécution terminée",
    step_limit: "Limite d’étapes",
    context_limit: "Limite de contexte",
    model_error: "Erreur du modèle",
    data_review_failed: "Synthèse non validée",
    aborted: "Exécution interrompue",
    partial: "Trace partielle",
  };
  return Object.hasOwn(labels, status)
    ? labels[status]
    : status || "Trace partielle";
}

function renderMetrics(id, values) {
  byId(id).replaceChildren(
    ...values.map(([label, value]) => {
      const metric = element("div", "metric");
      metric.append(
        typeof label === "string" ? element("span", "", label) : label,
        element("strong", "", value),
      );
      return metric;
    }),
  );
}

function sumMeasured(events, measure) {
  const values = events.map(measure);
  if (values.length && values.every(Number.isFinite)) {
    return values.reduce((total, value) => total + value, 0);
  }
}

function renderHeader() {
  const start = state.events.find((event) => event.event === "run_start");
  const end = endEvent();
  const pdf = state.events.findLast((event) => event.event === "pdf_written");
  const deliveryMs =
    pdf?.delivery_duration_ms ??
    Date.parse(pdf?.time_utc) - Date.parse(start?.time_utc);
  const hasDeliveryDuration = Number.isFinite(deliveryMs) && deliveryMs >= 0;
  const status =
    end?.status || (end?.event === "run_aborted" ? "aborted" : "partial");
  byId("question").textContent =
    start?.question || "Question absente de cette trace.";
  const runStatus = byId("run-status");
  runStatus.textContent = `${state.source} · ${displayStatus(status)}`;
  runStatus.dataset.state = status;
  renderMetrics("metrics", [
    [
      "Appels au modèle d’analyse",
      end?.steps ??
        state.events.filter((event) => event.event === "model_call").length,
    ],
    [
      "Appels d’outils",
      end?.tool_calls ??
        state.events.filter((event) => event.event === "tool_call").length,
    ],
    hasDeliveryDuration
      ? ["Durée jusqu'à livraison du rapport", duration(deliveryMs)]
      : ["Durée de l’analyse", duration(end?.duration_ms)],
  ]);
  const dataReviews = state.events.filter(
    (event) => event.event === "data_review",
  );
  const visualReviews = state.events.filter(
    (event) => event.event === "pdf_review",
  );
  const format = (value, formatter) =>
    Number.isFinite(value) ? formatter.format(value) : missing;
  const byRole = (measure, formatter) =>
    [
      measure(end),
      sumMeasured(dataReviews, measure),
      sumMeasured(visualReviews, measure),
    ]
      .map((value) => format(value, formatter))
      .join(" / ");
  const cacheLabel = element("span");
  cacheLabel.append(
    "Tokens en cache (",
    element("em", "", "Construct / Juge des données / Juge visuel"),
    ")",
  );
  renderMetrics("model-metrics", [
    [cacheLabel, byRole((event) => event?.usage?.cached_prompt_tokens, number)],
    ["Tokens d’entrée", byRole((event) => event?.usage?.prompt_tokens, number)],
    [
      "Tokens de sortie",
      byRole((event) => event?.usage?.completion_tokens, number),
    ],
    ["Coût estimé (USD)", byRole((event) => event?.estimated_cost_usd, money)],
  ]);
}

function timelineLabel(event) {
  switch (event.event) {
    case "run_start":
      return ["Demande reçue", "Début de l’analyse", "IN"];
    case "model_call": {
      let label = "Tour modèle";
      if (event.revision_kind === "data") label = "Correction des données";
      if (event === state.events[state.revisionIndex])
        label = "Révision par le modèle";
      return [
        `${label} ${event.step ?? ""}`.trim(),
        event.model || "Appel au fournisseur",
        "AI",
      ];
    }
    case "tool_call":
      return [
        event.name || "Outil",
        event.output?.error
          ? "Erreur renvoyée au modèle"
          : `Tour ${event.step ?? "—"} · exécution locale`,
        "FN",
      ];
    case "run_end":
      return [displayStatus(event.status), "Fin de l’analyse", "OK"];
    case "pdf_written":
      return [
        event.revision ? "PDF révisé" : "PDF créé",
        event.filename,
        "PDF",
      ];
    case "pdf_revision":
      return event.accepted
        ? [
            "Révision acceptée",
            "Toutes les pages ont été jugées lisibles",
            "OK",
          ]
        : ["PDF initial conservé", event.reason, "PDF"];
    case "data_review":
      return [dataReviewLabel(event), event.model || "Juge des données", "AI"];
    case "pdf_review": {
      const label = event.error
        ? "Relecture incomplète"
        : event.revision
          ? "Avis sur le PDF révisé"
          : "Avis du juge visuel";
      const page = Number.isSafeInteger(event.target_page)
        ? ` · page ${event.target_page}`
        : "";
      return [label + page, event.model, "AI"];
    }
    case "run_aborted":
      return ["Exécution interrompue", event.error_type, "!"];
    default:
      return [event.event, "Événement importé", "·"];
  }
}

function visibleIndexes() {
  return state.events.flatMap((event, index) =>
    state.filter === "all" ||
    event.event === state.filter ||
    (state.filter === "model_call" &&
      ["data_review", "pdf_review"].includes(event.event))
      ? [index]
      : [],
  );
}

function renderTimeline() {
  const items = visibleIndexes();
  const timeline = byId("timeline");
  byId("event-count").textContent =
    `${items.length} sur ${state.events.length} événements`;
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
    const stage = stages[eventStage(event)];
    if (event.event === "tool_call") item.dataset.kind = "tool";
    else if (stage.llm) item.dataset.kind = "model";
    const description = element("span");
    description.append(
      element("strong", "", label),
      element("span", "event-detail", detail),
    );
    item.append(
      element("span", `glyph${stage.local ? " local" : ""}`, glyph),
      description,
      element(
        "span",
        "event-duration",
        Number.isFinite(event.duration_ms) ? duration(event.duration_ms) : "",
      ),
    );
    item.addEventListener("click", () => {
      stop();
      select(index);
    });
    listItem.append(item);
    timeline.append(listItem);
  }
}

function block(parent, label, content, className = "inspect-block") {
  if (content === undefined || content === null || content === "") return;
  const section = element("section", className);
  section.append(element("h4", "", label));
  section.append(
    typeof content === "string" ? element("p", "", content) : content,
  );
  parent.append(section);
}

function code(text, language = "text") {
  const snippet = element("code", "snippet");
  highlightCode(snippet, String(text), language);
  const pre = element("pre");
  pre.append(snippet);
  return pre;
}

function json(value, replacer = null) {
  return code(JSON.stringify(value, replacer, 2), "json");
}

function requestMessages(request) {
  if (Array.isArray(request?.messages)) return request.messages;
  return Array.isArray(request?.input) ? request.input : [];
}

function renderPrompt(panel, event) {
  const request = event?.request ?? event;
  const messages = requestMessages(request);
  for (const [role, label] of [
    ["system", "Système"],
    ["user", "Utilisateur"],
  ]) {
    const content = messages.find((message) => message?.role === role)?.content;
    const prompt = Array.isArray(content)
      ? content
          .filter(
            (part) =>
              ["text", "input_text"].includes(part?.type) &&
              typeof part.text === "string",
          )
          .map((part) => part.text)
          .join("\n\n")
      : content;
    block(
      panel,
      label,
      typeof prompt === "string"
        ? code(prompt)
        : prompt == null
          ? "Message absent de la trace."
          : json(prompt),
    );
  }

  if (Array.isArray(request?.tools) && request.tools.length) {
    block(panel, "Outils", json(request.tools));
  } else {
    block(
      panel,
      "Outils",
      event?.request || Array.isArray(request?.tools)
        ? "Aucun outil proposé pour cet appel."
        : "Schémas des outils absents de la trace.",
    );
  }
}

function requestImages(request) {
  return requestMessages(request).flatMap((message) =>
    Array.isArray(message?.content)
      ? message.content
          .filter((part) => ["image_url", "input_image"].includes(part?.type))
          .map((part) => part.image_url)
      : [],
  );
}

function modelTurnAction(event) {
  if (event.event === "data_review") return dataReviewLabel(event);
  if (event.event === "pdf_review") {
    if (event.error) return "La relecture est incomplète.";
    if (!event.review && !event.response?.content)
      return "Aucun avis enregistré pour ce tour.";
    if (Number.isSafeInteger(event.target_page)) {
      return `Le juge a rendu un avis sur la page ${event.target_page}.`;
    }
    const pages = Array.isArray(event.pages)
      ? event.pages.map((page) => page?.number).filter(Number.isSafeInteger)
      : [];
    if (!pages.length) return "Le juge a rendu un avis de mise en page.";
    const label = pages.length === 1 ? "la page" : "les pages";
    return `Le juge a rendu un avis sur ${label} ${pages.join(", ")}.`;
  }
  if (event.error) return "L’appel au modèle a échoué.";
  if (
    ["length", "model_length", "error", "content_filter"].includes(
      event.finish_reason,
    )
  ) {
    return "Le modèle a renvoyé une réponse interrompue.";
  }
  const calls = event.response?.tool_calls;
  const tools = Array.isArray(calls)
    ? calls.map((call) => call?.function?.name).filter(Boolean)
    : [];
  if (tools.length) return `Le modèle a demandé ${tools.join(", ")}.`;
  if (event.response?.content) return "Le modèle a répondu en texte.";
  return "Aucune action enregistrée pour ce tour.";
}

function renderModelTurn(panel, event) {
  if (!event) {
    panel.append(element("p", "", "Aucun appel enregistré pour ce nœud."));
    return;
  }
  block(panel, "Action du modèle", modelTurnAction(event));
  let request = event.request;
  if (!request && (event.messages || event.tools)) {
    request = { messages: event.messages, tools: event.tools };
    panel.append(
      element(
        "p",
        "",
        "Messages conservés dans la trace. Corps HTTP indisponible.",
      ),
    );
  }
  const images = requestImages(request);
  const shortenContent = (key, value) => {
    if (key === "encrypted_content") return "[Raisonnement chiffré]";
    if (key === "image_url" && images.includes(value))
      return "[Image de page ci-dessous]";
    return value;
  };
  const content = element("div", "turn-request");
  if (!request) {
    content.append(element("p", "", "Requête absente de la trace."));
  } else if (images.length) {
    content.append(json(request, shortenContent));
    const download = element(
      "button",
      "quiet-button",
      "Télécharger le JSON complet",
    );
    download.type = "button";
    download.addEventListener("click", () => {
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(request)], { type: "application/json" }),
      );
      const link = element("a");
      link.href = url;
      link.download = "requete-llm.json";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
    });
    content.append(download);
  } else {
    content.append(json(request, shortenContent));
  }
  block(panel, "Requête envoyée", content, "inspect-block turn-block");
  if (event.event === "pdf_review") renderPdfPages(panel, event, images);
  if (event.event === "data_review") renderDataReview(panel, event);
  block(
    panel,
    "Réponse reçue",
    event.response != null
      ? json(event.response)
      : event.review
        ? element("p", "review-text", event.review)
        : "Réponse absente de la trace.",
    "inspect-block turn-block",
  );
  if (event.error) block(panel, "Erreur", event.error);
}

function findToolSchema(events, name) {
  for (const event of events) {
    if (event.event !== "model_call") continue;
    const specs = (event.request ?? event).tools;
    const schema =
      Array.isArray(specs) &&
      specs.find((spec) => (spec?.function?.name ?? spec?.name) === name);
    if (schema) return schema;
  }
}

function renderToolSchema(panel, name, step) {
  const matchingCalls = state.events.filter((event) => event.step === step);
  const schema =
    findToolSchema(matchingCalls, name) ?? findToolSchema(state.events, name);
  if (schema) {
    panel.append(json(schema));
    return;
  }
  const fallback = findToolSchema(DEFAULT_TRACE, name);
  block(
    panel,
    "Schéma de référence",
    fallback ? json(fallback) : "Schéma absent de la trace.",
  );
}

function renderToolTurn(panel, event) {
  if (event?.event !== "tool_call") {
    panel.append(
      element(
        "p",
        "",
        "Aucun appel de cet outil sélectionné dans la chronologie.",
      ),
    );
    return;
  }
  const parent = state.events.find(
    (entry) => entry.event === "model_call" && entry.step === event.step,
  );
  const calls = parent?.response?.tool_calls;
  const recordedCall =
    Array.isArray(calls) &&
    calls.find((call) => call?.id === event.tool_call_id);
  const call = recordedCall || {
    id: event.tool_call_id,
    type: "function",
    function: { name: event.name, arguments: event.arguments },
  };
  block(panel, "Demande à l’outil", json(call), "inspect-block turn-block");
  block(
    panel,
    "Réponse de l’outil",
    event.output === undefined
      ? "Réponse absente de la trace."
      : json(event.output),
    "inspect-block turn-block",
  );
}

function renderToolContext(panel, name) {
  const output = DEMO_TOOL_EXAMPLES[name];
  const content = element("div");
  content.append(element("p", "", stages[name].context));

  const sent =
    name === "write_report" && output?.accepted === true
      ? "Construct reçoit cette confirmation s’il doit reprendre le rapport après l’avis d’un juge."
      : output === undefined
        ? "Exemple indisponible."
        : "Identique";
  const comparison = element("dl", "tool-context-comparison");
  for (const [label, body] of [
    [
      "Sortie de l’outil (exemple)",
      output === undefined ? "Exemple indisponible." : json(output),
    ],
    ["Envoyé au modèle (exemple)", sent],
  ]) {
    const value = element("dd");
    value.append(body);
    comparison.append(element("dt", "", label), value);
  }
  content.append(comparison);
  block(panel, "Gestion du contexte", content, "inspect-block tool-context");
}

function chips(parent, values) {
  const container = element("div", "chips");
  for (const value of values.filter(Boolean))
    container.append(element("span", "chip", value));
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
    panel.append(
      element("p", "", "La question de départ est absente de la trace."),
    );
    return;
  }
  if (start.report_mode === false) {
    panel.append(
      element(
        "p",
        "",
        "Cette trace correspond à une analyse texte lancée depuis Python.",
      ),
    );
    block(panel, "Question", start.question);
    return;
  }

  const commandParts = [
    "uv run --locked --env-file .env report",
    "--out rapport-rejoue.pdf",
  ];
  const activeModel = state.events.find(
    (event) =>
      event.event === "model_call" &&
      typeof event.model === "string" &&
      !event.model.startsWith("scripted"),
  )?.model;
  if (activeModel) commandParts.push("--model=" + quotePowerShell(activeModel));
  const review = state.events.find((event) => event.event === "pdf_review");
  const dataReview = state.events.find(
    (event) => event.event === "data_review",
  );
  if (dataReview || start.data_review) commandParts.push("--review-data");
  if (review) commandParts.push("--review-pdf");
  const reviewModel = review?.model || dataReview?.model;
  if (typeof reviewModel === "string") {
    commandParts.push("--review-model=" + quotePowerShell(reviewModel));
  }
  if (Number.isSafeInteger(start.max_steps) && start.max_steps > 0) {
    commandParts.push("--max-steps " + start.max_steps);
  }
  commandParts.push("-- " + quotePowerShell(start.question));
  const command = commandParts.join(" ");
  panel.append(
    element(
      "p",
      "",
      "Commande reconstituée à partir des paramètres disponibles dans la trace.",
    ),
  );
  const preview = code(command, "powershell");
  block(panel, "Commande PowerShell", preview);
  panel.append(
    element(
      "p",
      "",
      "L’option --max-steps limite les appels au modèle. Un appel peut demander plusieurs outils.",
    ),
  );
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

function dataReviewLabel(event) {
  switch (event.verdict) {
    case "valide":
      return "Synthèse vérifiée";
    case "a_corriger":
      return "Écart dans la synthèse";
    case "incomplet":
      return "Vérification des données incomplète";
    default:
      return "Contrôle SQL du juge";
  }
}

function renderDataReview(panel, event) {
  if (!event) {
    block(
      panel,
      "Activation",
      "Ajoute --review-data pour vérifier la synthèse.",
    );
    return;
  }
  block(panel, "État", dataReviewLabel(event));
  if (event.feedback)
    block(panel, "Avis du juge", element("p", "review-text", event.feedback));
  for (const check of Array.isArray(event.checks) ? event.checks : []) {
    if (typeof check.query === "string")
      block(panel, "Requête de contrôle", code(check.query, "sql"));
    block(panel, "Résultat du contrôle", json(check.output));
  }
}

function renderPdfReview(panel, event) {
  const pageScope = Number.isSafeInteger(event?.target_page)
    ? "Cette trace évalue une page cible par appel, avec ses voisines comme contexte."
    : "Chaque appel regroupe jusqu’à quatre pages avec leurs numéros, la question initiale et les consignes du juge.";
  block(
    panel,
    "Envoi au juge",
    "Une image JPEG par page, encodée en Base64 (côté long de 1 440 px, qualité 82). " +
      pageScope,
  );
  if (!event) {
    block(
      panel,
      "Relecture visuelle",
      "Lance la commande avec --review-pdf, puis importe sa trace.",
    );
    return;
  }
  chips(panel, [event.model, duration(event.duration_ms)]);
  if (event.review)
    block(panel, "Avis du juge", element("p", "review-text", event.review));
  if (event.error) block(panel, "Relecture incomplète", event.error);
  if (event.usage) block(panel, "Compteurs de tokens", json(event.usage));
}

function renderPdfPages(panel, event, images) {
  if (!Array.isArray(event.pages)) return;
  const list = element("div", "review-pages");
  for (const [index, page] of event.pages.entries()) {
    if (!Number.isSafeInteger(page?.number) || page.number < 1) continue;
    const source = images[index] ?? page.image;
    if (
      typeof source !== "string" ||
      !/^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/.test(source)
    )
      continue;
    const hasTarget = Number.isSafeInteger(event.target_page);
    const target = page.number === event.target_page;
    const role = hasTarget ? (target ? " · à évaluer" : " · contexte") : "";
    const label = `Page ${page.number}${role}`;
    const button = element("button", "review-page");
    button.type = "button";
    if (target) button.dataset.target = "";
    const image = element("img");
    image.src = source;
    image.alt = "";
    image.loading = "lazy";
    button.append(image, element("span", "", label));
    button.addEventListener("click", () => {
      const dialog = byId("review-dialog");
      const preview = byId("review-preview");
      preview.alt = label;
      preview.src = source;
      byId("review-page-title").textContent = label;
      dialog.showModal();
    });
    list.append(button);
  }
  const heading = Number.isSafeInteger(event.total_pages)
    ? `Pages envoyées · ${event.total_pages} au total`
    : "Pages envoyées";
  if (list.childElementCount) block(panel, heading, list);
}

function renderInspector() {
  const stage = stages[state.stage];
  const tabs = byId("tabs").querySelectorAll("button");
  const panel = byId("inspector");
  panel.replaceChildren();
  panel.scrollTop = 0;
  if (!stage) {
    for (const button of tabs) {
      button.hidden = true;
      button.setAttribute("aria-pressed", "false");
    }
    byId("selection-label").textContent = "";
    panel.append(
      element(
        "p",
        "",
        "Sélectionne une étape dans la carte ou un événement dans la chronologie.",
      ),
    );
    return;
  }
  for (const button of tabs) {
    const { stage: scope, tab } = button.dataset;
    if (tab === "overview") {
      if (stage.tool)
        button.replaceChildren(
          "Aperçu et gestion",
          element("br"),
          "du contexte",
        );
      else button.textContent = "Aperçu";
    }
    const inScope =
      scope === "tool"
        ? stage.tool
        : scope === "llm"
          ? stage.llm
          : scope === "turn"
            ? stage.tool || stage.llm
            : !scope || scope === state.stage;
    button.hidden =
      !inScope ||
      (tab === "json" &&
        Boolean(stage.tool || stage.llm || state.stage === "pdf"));
    if (button.hidden && tab === state.tab) state.tab = "overview";
  }
  for (const button of tabs) {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.tab === state.tab),
    );
  }

  const selected = state.events[state.index];
  const event = !observed(state.stage, selected)
    ? selected.event === "run_end" && state.stage === "write_report"
      ? selected
      : null
    : selected;
  const label = [stage.label];
  const turn = event?.step ?? event?.turn;
  if (Number.isSafeInteger(turn) && turn > 0) label.push(`tour ${turn}`);
  const model = event?.model || event?.request?.model;
  if (stage.llm && typeof model === "string" && model) {
    label.push(model === "gpt-6.2-sol" ? "GPT-6.2 Sol" : model);
  }
  byId("selection-label").textContent = label.join(" · ");
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
    intro.append(
      "Dans notre code, ",
      element("code", "", "write_report"),
      " reçoit la clé JSON ",
      element("code", "", "title"),
      ". Après validation, elle devient ",
      element("code", "", "report.title"),
      " dans un objet Python. On associe ensuite explicitement ce texte à un style ReportLab :",
    );
    const detail = element("p");
    detail.append(
      "Le style ",
      element("code", "", "title"),
      " définit la police, la taille (27 points) et l’espacement. Python assemble de la même façon les paragraphes, les tableaux et les images dans l’ordre voulu. ReportLab met ces blocs en page et génère le PDF.",
    );
    panel.append(
      intro,
      code('_paragraph(report.title, styles["title"])'),
      detail,
    );
    return;
  }
  if (state.tab === "turn") {
    if (stage.tool) renderToolTurn(panel, event);
    else renderModelTurn(panel, event);
    return;
  }
  if (state.tab === "tool-schema") {
    renderToolSchema(panel, state.stage, event?.step);
    return;
  }
  if (state.tab === "json") {
    panel.append(
      event
        ? json(event)
        : element("p", "", "Aucun événement JSON enregistré pour cette étape."),
    );
    return;
  }
  if (event?.event === "run_end") {
    panel.append(element("h3", "", "Fin de l’analyse"));
  } else {
    panel.append(element("h3", "", stage.label), element("p", "", stage.about));
  }
  if (stage.tool) {
    renderToolContext(panel, state.stage);
    if (event?.event !== "run_end") return;
  }
  if (state.stage === "pdf") {
    const status =
      event?.event === "pdf_revision"
        ? event.accepted
          ? "Le PDF révisé a remplacé la version initiale."
          : event.reason
        : event?.filename ||
          "La trace ne permet pas de confirmer qu’un fichier PDF a été écrit.";
    block(panel, "État", status);
    return;
  }
  if (state.stage === "pdf_review") {
    renderPdfReview(panel, event);
    return;
  }
  if (state.stage === "data_review") {
    renderDataReview(panel, event);
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
        event.max_steps &&
          `${event.max_steps} appels au modèle max. avant relecture`,
      ]);
      if (event.max_steps) {
        panel.append(
          element(
            "p",
            "",
            "Un appel au modèle peut demander plusieurs outils. Les appels d’outils sont comptés séparément.",
          ),
        );
      }
      break;
    case "model_call":
      if (event.error) block(panel, "Erreur", event.error);
      if (event.usage) block(panel, "Compteurs de tokens", json(event.usage));
      break;
    case "run_end":
      block(panel, "Synthèse après validation", event.answer);
      block(panel, "Erreur", event.error);
      if (Number.isFinite(event.estimated_cost_usd))
        block(
          panel,
          "Coût estimé",
          `${money.format(event.estimated_cost_usd)} USD`,
        );
      if (Array.isArray(event.number_audit?.literals)) {
        const flagged = event.number_audit.literals.filter(
          (entry) => entry && !entry.supported,
        );
        block(
          panel,
          "Chiffres à vérifier",
          flagged.length
            ? flagged.map((entry) => entry.literal).join(", ")
            : "Aucun chiffre signalé.",
        );
      }
      break;
    default:
      panel.append(json(event));
  }
}

function renderSelection() {
  for (const item of byId("timeline").querySelectorAll("button")) {
    if (Number(item.dataset.index) === state.index)
      item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  }
  for (const [key, node] of nodes) {
    node.setAttribute("aria-pressed", String(state.stage === key));
    node.dataset.observed = String(
      state.events.some((event) => observed(key, event)),
    );
  }
  for (const arrow of revisionArrows) {
    const link = byId(`${arrow.id}-link`);
    const index = state[arrow.index];
    link.dataset.observed = String(index >= 0);
    if (state.stage === "model" && state.index === index)
      link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
  }
  byId("position").textContent =
    `Événement ${state.index + 1} / ${state.events.length}`;
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
  select(
    Math.min(state.events.length - 1, Math.max(0, state.index + increment)),
  );
  if (scroll) {
    const timeline = byId("timeline");
    const selected = timeline.querySelector('[aria-current="step"]');
    if (selected) {
      const source = selected.getBoundingClientRect();
      const viewport = timeline.getBoundingClientRect();
      if (source.bottom > viewport.bottom)
        timeline.scrollTop += source.bottom - viewport.bottom;
      if (source.top < viewport.top)
        timeline.scrollTop -= viewport.top - source.top;
    }
  }
}

function loadTrace(events, source) {
  stop();
  state.events = events;
  state.dataRevisionIndex = events.findIndex(
    (event) => event.event === "model_call" && event.revision_kind === "data",
  );
  const reviewIndex = events.findIndex((event) => event.event === "pdf_review");
  state.revisionIndex =
    reviewIndex < 0
      ? -1
      : events.findIndex(
          (event, index) => index > reviewIndex && event.event === "model_call",
        );
  state.source = source;
  state.filter = "all";
  state.tab = "overview";
  for (const button of byId("filters").querySelectorAll("button")) {
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.filter === state.filter),
    );
  }
  byId("import-error").hidden = true;
  renderHeader();
  renderTimeline();
  select(0);
}

function parseTrace(raw) {
  const text = raw.replace(/^\uFEFF/, "").trim();
  const records = text.startsWith("[")
    ? JSON.parse(text)
    : text.split(/\r?\n/).flatMap((line, index) => {
        if (!line.trim()) return [];
        try {
          return [JSON.parse(line)];
        } catch {
          throw new Error(`JSON invalide à la ligne ${index + 1}.`);
        }
      });
  if (
    !Array.isArray(records) ||
    !records.length ||
    records.some(
      (record) =>
        !record ||
        typeof record !== "object" ||
        typeof record.event !== "string",
    )
  )
    throw new Error("Le fichier doit contenir des événements de trace JSONL.");
  return records;
}

const tracePicker = byId("trace-picker");
const traceOptions = byId("trace-options");
const traceChoices = new Map();
const traceDate = new Intl.DateTimeFormat("fr-FR", {
  dateStyle: "short",
  timeStyle: "short",
});
let displayedTrace = "default";
let traceLoad = 0;

function addTraceChoice(key, choice, label = choice.path) {
  if (traceChoices.has(key)) return;
  traceChoices.set(key, choice);
  const option = element("option", "", label);
  option.value = key;
  traceOptions.append(option);
  traceOptions.hidden = false;
}

function addTraceFile(file) {
  const key = JSON.stringify([file.name, file.lastModified, file.size]);
  addTraceChoice(key, { path: file.name, file });
  return key;
}

function addLocalTraces() {
  const entries = globalThis.OLIST_TRACE_INDEX;
  if (!Array.isArray(entries)) return;
  for (const entry of entries) {
    if (typeof entry.path !== "string" || !entry.path.endsWith(".jsonl"))
      continue;
    const parts = entry.path.split("/");
    if (parts.some((part) => !part || part === "." || part === "..")) continue;
    const folder = parts.at(-2);
    const name = parts.at(-1);
    const id = name === "trace.jsonl" && folder ? folder.slice(0, 8) : name;
    const labels = [
      Number.isFinite(entry.updated_ms)
        ? traceDate.format(entry.updated_ms)
        : "",
      id,
    ];
    if (typeof entry.question === "string")
      labels.push(entry.question.slice(0, 76));
    addTraceChoice(
      `local:${entry.path}`,
      { path: entry.path, parts },
      labels.filter(Boolean).join(" · "),
    );
  }
}

function readLocalTrace(parts) {
  return new Promise((resolve, reject) => {
    const script = element("script");
    const fail = () => {
      script.remove();
      reject(new Error("Trace locale indisponible."));
    };
    script.onload = () => {
      if (!Array.isArray(script.traceEvents)) {
        fail();
        return;
      }
      script.remove();
      resolve(script.traceEvents);
    };
    script.onerror = fail;
    script.src = `../runs/${parts.map(encodeURIComponent).join("/")}.js`;
    document.head.append(script);
  });
}

function showImportError(message) {
  const banner = byId("import-error");
  banner.textContent = message;
  banner.hidden = false;
}

async function openTraceChoice(choice) {
  const current = ++traceLoad;
  try {
    if (choice === "default") {
      loadTrace(DEFAULT_TRACE, "trace.jsonl");
    } else {
      const { file, path, parts } = traceChoices.get(choice);
      if (file?.size > 16_000_000)
        throw new Error("Le fichier dépasse la limite d’import de 16 Mo.");
      const records = file
        ? parseTrace(await file.text())
        : await readLocalTrace(parts);
      if (current !== traceLoad) return;
      loadTrace(records, path);
    }
    displayedTrace = choice;
  } catch (error) {
    if (current === traceLoad) showImportError(error.message);
  } finally {
    if (current === traceLoad) tracePicker.value = displayedTrace;
  }
}

setupMap();
addLocalTraces();
loadTrace(DEFAULT_TRACE, "trace.jsonl");
tracePicker.addEventListener("change", () => {
  const { value } = tracePicker;
  if (value === "file") {
    tracePicker.value = displayedTrace;
    byId("trace-file").click();
  } else {
    openTraceChoice(value);
  }
});
byId("trace-file").addEventListener("change", (event) => {
  const input = event.currentTarget;
  const file = input.files[0];
  input.value = "";
  if (file) openTraceChoice(addTraceFile(file));
});
byId("filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  for (const choice of byId("filters").querySelectorAll("button"))
    choice.setAttribute("aria-pressed", String(choice === button));
  renderTimeline();
  renderSelection();
});
byId("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  state.tab = button.dataset.tab;
  renderInspector();
});
byId("previous").addEventListener("click", () => {
  stop();
  step(-1, true);
});
byId("next").addEventListener("click", () => {
  stop();
  step(1, true);
});
byId("play").addEventListener("click", () => {
  if (state.timer !== null) {
    stop();
    return;
  }
  if (state.index === state.events.length - 1) select(0);
  byId("play").textContent = "Ⅱ";
  byId("play").setAttribute("aria-label", "Mettre en pause");
  byId("play").title = "Mettre en pause";
  state.timer = setInterval(() => {
    step(1, true);
    if (state.index === state.events.length - 1) stop();
  }, 1050);
});
