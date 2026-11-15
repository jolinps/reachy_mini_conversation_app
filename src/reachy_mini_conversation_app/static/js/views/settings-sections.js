/** Extra settings sections on the existing console. */

import {
  applySettings,
  describeError,
  exportDiagnostics,
  getSettings,
  refreshGatewayAgents,
  saveSettings,
  testGatewayText,
  testRobotConnection,
} from "../api.js";
import { h } from "../ui.js";

const SECTIONS = [
  ["robot", "Robot & Simulator"],
  ["agent", "AI Agent"],
  ["voice", "Voice"],
  ["conversation", "Conversation"],
  ["tools", "Tools & Modules"],
  ["diagnostics", "Diagnostics"],
];

export function settingsSectionIds() {
  return SECTIONS.map(([id]) => id);
}

export function buildSettingsNav(current, onSelect) {
  const nav = h("nav", { class: "settings-nav", "aria-label": "Settings sections" });
  for (const [id, title] of SECTIONS) {
    nav.appendChild(
      h(
        "button",
        {
          type: "button",
          class: "settings-nav-button",
          "data-section": id,
          ...(id === current ? { "aria-current": "page" } : {}),
          onClick: () => onSelect(id),
        },
        title
      )
    );
  }
  return {
    element: nav,
    select(id) {
      for (const button of nav.querySelectorAll("[data-section]")) {
        if (button.dataset.section === id) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
      }
    },
  };
}

function field(label, input) {
  return h("label", { class: "settings-field" }, h("span", { class: "settings-label" }, label), input);
}

function statusLine() {
  return h("p", { class: "settings-status", role: "status", "aria-live": "polite" });
}

export function buildRobotSection() {
  const mode = h(
    "select",
    { class: "settings-select", name: "robot_mode" },
    h("option", { value: "simulator" }, "Local simulator"),
    h("option", { value: "physical" }, "Physical Reachy")
  );
  const host = h("input", { class: "settings-input", name: "robot_host", placeholder: "127.0.0.1" });
  const port = h("input", { class: "settings-input", name: "robot_port", type: "number", min: "1", max: "65535", value: "8010" });
  const name = h("input", { class: "settings-input", name: "robot_name", placeholder: "reachy-mini-sim" });
  const advanced = h("input", { class: "settings-input", name: "robot_advanced_host", placeholder: "Optional override host" });
  const status = statusLine();
  const form = h(
    "form",
    { class: "settings-form" },
    field("Robot", mode),
    field("Address", host),
    field("Port", port),
    field("Robot name", name),
    h("details", { class: "settings-advanced" }, h("summary", {}, "Advanced"), field("Connection override", advanced)),
    h("p", { class: "settings-hint" }, "Saving does not start, stop, or move the robot. Test connection reads status only."),
    h(
      "div",
      { class: "settings-actions" },
      h("button", { type: "submit", class: "btn btn--primary" }, "Save robot"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "test" }, "Test connection"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "apply" }, "Apply")
    ),
    status
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit(form, status, {
      robot_mode: mode.value,
      robot_host: host.value.trim(),
      robot_port: Number.parseInt(port.value, 10),
      robot_name: name.value.trim(),
      ...(advanced.value.trim() ? { robot_advanced_host: advanced.value.trim() } : {}),
    });
  });
  form.querySelector("[data-action=test]").addEventListener("click", async () => {
    status.textContent = "Checking status…";
    try {
      const result = await testRobotConnection();
      status.textContent = result.ok
        ? `Reachable. Simulation ${result.verified_simulation ? "verified" : "not reported"}. ${result.robot_name || ""}`.trim()
        : result.message || "The status check failed.";
      status.classList.toggle("is-error", !result.ok);
    } catch (error) {
      fail(status, error);
    }
  });
  form.querySelector("[data-action=apply]").addEventListener("click", () => apply(status));
  return {
    element: section("Robot & Simulator", form),
    sync(snapshot) {
      const saved = snapshot?.saved || {};
      if (saved.robot_mode) mode.value = saved.robot_mode;
      host.value = saved.robot_host || "";
      if (saved.robot_port) port.value = String(saved.robot_port);
      name.value = saved.robot_name || "";
      advanced.value = saved.robot_advanced_host || "";
      const robot = snapshot?.statuses?.robot || {};
      status.textContent = `Connection ${robot.connection || "not_tested"}. Simulation ${robot.simulation || "not_tested"}.`;
    },
  };
}

export function buildAgentSection() {
  const gateway = h("input", { class: "settings-input", name: "gateway_url", placeholder: "https://example.invalid" });
  const identity = h("input", { class: "settings-input", name: "device_identity_path", placeholder: "Absolute path to the existing identity file" });
  const agent = h("select", { class: "settings-select", name: "gateway_agent_id" }, h("option", { value: "" }, "Refresh to list authorized agents"));
  const catalog = h("p", { class: "settings-hint" }, "Profile catalog has not been requested.");
  const observed = h("p", { class: "settings-hint" }, "Observed session profile: none.");
  const prompt = h("input", { class: "settings-input", name: "text", value: "Reply with the single word pong." });
  const status = statusLine();
  const form = h(
    "form",
    { class: "settings-form" },
    field("Gateway URL", gateway),
    field("Device identity file", identity),
    h("p", { class: "settings-hint" }, "The private key stays in that file. Administrative credentials stay on the gateway."),
    field("Authorized agent", agent),
    catalog,
    observed,
    field("Harmless text test", prompt),
    h(
      "div",
      { class: "settings-actions" },
      h("button", { type: "submit", class: "btn btn--primary" }, "Save agent"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "refresh" }, "Refresh agents"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "test" }, "Test text"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "apply" }, "Apply")
    ),
    status
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = {};
    if (gateway.value.trim()) payload.gateway_url = gateway.value.trim();
    if (identity.value.trim()) payload.device_identity_path = identity.value.trim();
    if (agent.value) payload.gateway_agent_id = agent.value;
    submit(form, status, payload);
  });
  form.querySelector("[data-action=refresh]").addEventListener("click", async () => {
    status.textContent = "Refreshing agents…";
    try {
      const result = await refreshGatewayAgents();
      fillAgents(agent, result.agents || []);
      const catalogStatus = result.catalog?.status || "unsupported";
      catalog.textContent = catalogStatus === "fresh"
        ? `Catalog: ${(result.catalog.profiles || []).map((item) => item.id).join(", ") || "empty"}.`
        : `Catalog: ${catalogStatus}. ${result.catalog?.error || "No selectable profile was added."}`;
      observed.textContent = "Observed session profile: none. A catalog label is not a session profile.";
      status.textContent = result.ok
        ? `Authenticated ${result.device_id}. Auth ${result.timing_ms?.authentication ?? "-"} ms.`
        : result.message || "Agent refresh failed.";
      status.classList.toggle("is-error", !result.ok);
    } catch (error) {
      fail(status, error);
    }
  });
  form.querySelector("[data-action=test]").addEventListener("click", async () => {
    status.textContent = "Sending one text turn…";
    try {
      const result = await testGatewayText(prompt.value);
      const timing = result.timing_ms || {};
      status.textContent = result.ok
        ? `Answer “${result.answer}”. Auth ${timing.authentication} ms, select ${timing.selection} ms, gateway bound ${timing.gateway_bound} ms, first answer ${timing.first_answer} ms, final ${timing.final_answer} ms. Progress events ${timing.progress_events} are not the answer.`
        : result.message || "The text test failed.";
      status.classList.toggle("is-error", !result.ok);
    } catch (error) {
      fail(status, error);
    }
  });
  form.querySelector("[data-action=apply]").addEventListener("click", () => apply(status));
  return {
    element: section("AI Agent", form),
    sync(snapshot) {
      const saved = snapshot?.saved || {};
      gateway.value = saved.gateway_url || "";
      const fileName = saved.identity?.file_name;
      if (!identity.value && fileName) identity.placeholder = fileName;
      if (saved.gateway_agent_id) fillAgents(agent, [{ id: saved.gateway_agent_id, name: saved.gateway_agent_id, enabled: true }]);
      const gatewayStatus = snapshot?.statuses?.gateway || {};
      catalog.textContent = `Catalog: ${gatewayStatus.catalog || "unsupported"}.`;
      observed.textContent = `Observed session profile: ${gatewayStatus.observed_session_profile || "none"}.`;
    },
  };
}

export function buildExperimentalVoice(connectionElement) {
  const route = h(
    "select",
    { class: "settings-select", name: "voice_route" },
    h("option", { value: "huggingface" }, "Hugging Face baseline"),
    h("option", { value: "hermes" }, "Hermes voice (not connected)")
  );
  const stt = h(
    "select",
    { class: "settings-select", name: "stt_provider" },
    h("option", { value: "hosted_hf" }, "Hosted Hugging Face session"),
    h("option", { value: "external" }, "Separate STT provider")
  );
  const tts = h(
    "select",
    { class: "settings-select", name: "tts_provider" },
    h("option", { value: "hosted_hf" }, "Hosted Hugging Face session"),
    h("option", { value: "external" }, "Separate TTS provider")
  );
  const language = h("input", { class: "settings-input", name: "stt_language", placeholder: "en" });
  const voice = h("input", { class: "settings-input", name: "tts_voice", placeholder: "Provider voice id" });
  const runtime = h("input", { class: "settings-input", name: "voice_runtime_url", placeholder: "Voice runtime URL" });
  const status = statusLine();
  const form = h(
    "form",
    { class: "settings-form" },
    h("p", { class: "settings-hint" }, "The hosted Hugging Face session does not accept a custom Hermes backend or separate STT and TTS credentials."),
    field("Voice route", route),
    field("STT provider", stt),
    field("TTS provider", tts),
    field("STT language", language),
    field("TTS voice", voice),
    field("Voice-runtime URL", runtime),
    h("p", { class: "settings-hint" }, "Microphone and speaker selection is unavailable. WebRTC is not implemented. Microphone-level and speaker-playback tests stay off so this page does not open the baseline session's devices."),
    h(
      "div",
      { class: "settings-actions" },
      h("button", { type: "button", class: "btn btn--ghost", disabled: "disabled" }, "Test microphone level"),
      h("button", { type: "button", class: "btn btn--ghost", disabled: "disabled" }, "Test speaker playback")
    ),
    h("p", { class: "settings-hint", "data-role": "voice-status" }, "Transport: unavailable."),
    h(
      "div",
      { class: "settings-actions" },
      h("button", { type: "submit", class: "btn btn--primary" }, "Save voice route"),
      h("button", { type: "button", class: "btn btn--ghost", "data-action": "apply" }, "Apply")
    ),
    status
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const payload = {
      voice_route: route.value,
      stt_provider: stt.value,
      tts_provider: tts.value,
    };
    if (language.value.trim()) payload.stt_language = language.value.trim();
    if (voice.value.trim()) payload.tts_voice = voice.value.trim();
    if (runtime.value.trim()) payload.voice_runtime_url = runtime.value.trim();
    submit(form, status, payload);
  });
  form.querySelector("[data-action=apply]").addEventListener("click", () => apply(status));
  const element = h(
    "div",
    { class: "settings-stack" },
    connectionElement,
    section("Speech providers", form)
  );
  return {
    element,
    sync(snapshot) {
      const saved = snapshot?.saved || {};
      route.value = saved.voice_route || "huggingface";
      stt.value = saved.stt_provider || "hosted_hf";
      tts.value = saved.tts_provider || "hosted_hf";
      language.value = saved.stt_language || "";
      voice.value = saved.tts_voice || "";
      runtime.value = saved.voice_runtime_url || "";
      const voiceStatus = snapshot?.statuses?.voice || {};
      const routeState = snapshot?.application?.voice_route?.state || voiceStatus.route_state || "live_huggingface";
      form.querySelector("[data-role=voice-status]").textContent =
        `Live route ${voiceStatus.live_route || "huggingface"}. Saved route ${voiceStatus.saved_route || "huggingface"} (${routeState}). Transport ${voiceStatus.transport || "unavailable"}. WebRTC ${voiceStatus.webrtc || "not_implemented"}. STT ${voiceStatus.stt || "unavailable"}. TTS ${voiceStatus.tts || "unavailable"}.`;
    },
  };
}

export function buildConversationIntro() {
  return h(
    "section",
    { class: "settings-section" },
    h("h2", { class: "settings-section-title" }, "Conversation"),
    h(
      "p",
      { class: "settings-hint" },
      "Personalities are application characters. They are not Hermes profiles. Local speech detection and barge-in affect the running Hugging Face session. The Hermes voice route does not use them until that runtime exists."
    )
  );
}

export function buildToolsIntro(toolsElement) {
  return h(
    "div",
    { class: "settings-stack" },
    h(
      "section",
      { class: "settings-section" },
      h("h2", { class: "settings-section-title" }, "Tools & Modules"),
      h(
        "p",
        { class: "settings-hint" },
        "Tool access and Tool Spaces below are the implemented modules. Bedtime stories, star charts, and facial recognition are not connected."
      )
    ),
    toolsElement
  );
}

export function buildDiagnosticsSection(statusElement) {
  const summary = h("p", { class: "settings-hint", "data-role": "diagnostics-summary" }, "Status has not been loaded.");
  const report = h("pre", { class: "settings-export" }, "No export yet.");
  const status = statusLine();
  const button = h("button", { type: "button", class: "btn btn--ghost" }, "Export redacted diagnostics");
  button.addEventListener("click", async () => {
    try {
      const payload = await exportDiagnostics();
      const text = JSON.stringify(payload, null, 2);
      report.textContent = text;
      status.textContent = text.includes("private_key") || text.includes("session_token")
        ? "Export contained a secret and was not saved."
        : "Export is redacted. It omits transcripts.";
      status.classList.toggle("is-error", status.textContent.startsWith("Export contained"));
    } catch (error) {
      fail(status, error);
    }
  });
  return {
    element: h(
      "div",
      { class: "settings-stack" },
      statusElement,
      section("Diagnostics", h("div", { class: "settings-form" }, summary, button, report, status))
    ),
    sync(snapshot) {
      const statuses = snapshot?.statuses || {};
      const robot = statuses.robot || {};
      const gateway = statuses.gateway || {};
      const voice = statuses.voice || {};
      const latency = gateway.latency_ms;
      const timing = latency
        ? ` Measured text timing: auth ${latency.authentication ?? "n/a"} ms, selection ${latency.selection ?? "n/a"} ms, gateway bound ${latency.gateway_bound ?? "n/a"} ms, first answer ${latency.first_answer ?? "n/a"} ms, final ${latency.final_answer ?? "n/a"} ms.`
        : " No text-test timing has been measured.";
      summary.textContent = [
        `Robot ${robot.connection || "not_tested"}. Simulation ${robot.simulation || "not_tested"}.`,
        `Gateway authentication ${gateway.authentication || "not_tested"}. Authorization ${gateway.authorization_status || "not_tested"}. Agent list ${Array.isArray(gateway.agents) && gateway.agents.length ? "ready" : "not_ready"}.`,
        `STT ${voice.stt || "unavailable"}. TTS ${voice.tts || "unavailable"}. Transport ${voice.transport || "unavailable"}.`,
        timing.trim(),
      ].join(" ");
    },
  };
}

function section(title, body) {
  return h("section", { class: "settings-section" }, h("h2", { class: "settings-section-title" }, title), body);
}

async function submit(form, status, payload) {
  status.classList.remove("is-error");
  status.textContent = "Saving…";
  try {
    const snapshot = await saveSettings(payload);
    status.textContent = snapshot.apply_blocked
      ? "Saved. Apply waits until the conversation is idle."
      : "Saved. Robot, gateway, and Hermes voice stay unapplied until a runtime can use them.";
  } catch (error) {
    fail(status, error);
  }
}

async function apply(status) {
  status.classList.remove("is-error");
  status.textContent = "Applying…";
  try {
    const snapshot = await applySettings();
    const speech = snapshot.application?.hf_speech?.state;
    if (speech === "pending_restart") {
      status.textContent = "Hugging Face or speech settings are reconnecting. Robot, gateway, and Hermes voice were not applied.";
    } else if (speech === "applied") {
      status.textContent = "Hugging Face and speech settings match the running session. Robot, gateway, and Hermes voice were not applied.";
    } else {
      status.textContent = "Saved values were not applied to the live session. Hermes voice and WebRTC remain unavailable.";
    }
  } catch (error) {
    fail(status, error);
  }
}

function fail(status, error) {
  status.textContent = describeError(error);
  status.classList.add("is-error");
}

function fillAgents(select, agents) {
  const current = select.value;
  select.replaceChildren(h("option", { value: "" }, agents.length ? "Choose an agent" : "No authorized agent"));
  for (const row of agents) {
    if (!row.enabled && row.id !== current) continue;
    const option = h("option", { value: row.id }, row.name && row.name !== row.id ? `${row.name} (${row.id})` : row.id);
    if (row.id === current) option.selected = true;
    select.appendChild(option);
  }
}

export async function loadSettingsSnapshot(signal) {
  const snapshot = await getSettings();
  if (signal?.aborted) return null;
  return snapshot;
}
