import { useCallback, useEffect, useMemo, useState } from "react";

// 公开视图：隐藏资源没有 amount，只有 estimate 区间与 display 文本
type Resource = { id: string; name: string; amount?: number; unit: string; hidden?: boolean; estimate?: number[]; display?: string };
type Region = {
  id: string;
  name: string;
  x: number;
  y: number;
  polygon: number[][];
  adjacency: Record<string, number>;
  stats: Record<string, number>;
};
type Character = {
  id: string;
  name: string;
  region: string;
  office: string | null;
  status: string;
  traits: Record<string, number>;
  persona: string;
};
type State = {
  scenario_id: string;
  turn: number;
  calendar: { label: string };
  resources: Record<string, Resource>;
  regions: Record<string, Region>;
  characters: Record<string, Character>;
  offices: Record<string, { id: string; name: string; holder: string | null }>;
  ledger: Array<{ id: string; name: string; amount: number; kind: string; turns: number | null }>;
  projects: Array<{ id: string; name: string; progress: number; turns: number; stalled: boolean }>;
  pending_clauses: Array<{ id: string; due_turn: number }>;
  standing_orders: Array<{ id: string; text: string; until_turn: number | null }>;
};
type Scenario = {
  id: string;
  title: string;
  era: string;
  valid: boolean;
  theme_css: string;
  categories: Array<{ id: string; name: string; hint: string }>;
  asset_ids: string[];
};
type SessionRow = { id: string; scenario: string; turn: number };
type Fact = { kind: string; text: string };
type TurnReport = {
  facts: Fact[];
  ledger_lines: Array<{ name: string; amount: number; kind: string; reason: string }>;
  events_fired: string[];
  warnings: string[];
  reconciliation?: Array<{ resource: string; display?: string }>;
};
type Receipt = {
  id: string;
  confirmed: boolean;
  clauses: Array<{ category: string; text: string; summary?: string; status: string; reason?: string; delay_reason?: string; op?: unknown }>;
  state?: State;
};
type MemorialOption = { id: string; label: string; hint: string; ops_summary: string[] };
type Memorial = {
  id: string;
  title: string;
  summary: string;
  arrival?: string;
  advisors: Array<{ character: string; name: string; stance: string; line: string }>;
  options: MemorialOption[];
};
type PendingChoice = { id: string; title: string; text: string; deadline_turn: number; default_option: string; options: Array<{ id: string; text: string }> };
type Outcome = { kind: string; id: string; title: string; text: string; satisfied: string[] };
type Crisis = { title: string; display: string; band: string; terms: Array<{ label: string; display: string }> };
type View = {
  turn: number;
  calendar_label: string;
  state: State;
  objectives: Array<{ id: string; title: string; status: string; detail: string }>;
  crisis: Partial<Crisis> | null;
  memorials: Memorial[];
  pending_choices: PendingChoice[];
  outcome: Outcome | null;
  agenda: { text: string } | null;
  final_notice: { text: string } | null;
  provider_mode: string;
  tutorial: { steps: Array<{ step: number; title: string; text: string; target: string }> };
};
const stanceLabels: Record<string, string> = { support: "赞成", oppose: "反对", neutral: "持中" };
const objectiveLabels: Record<string, string> = { on_track: "可期", at_risk: "危殆", failed: "已失", achieved: "已成", pending: "未定" };

class ApiError extends Error {
  code: string;
  constructor(message: string, code: string) {
    super(message);
    this.code = code;
  }
}

function resourceText(resource: Resource): string {
  if (resource.hidden && resource.estimate) return `约 ${resource.estimate[0]}–${resource.estimate[1]}${resource.unit}`;
  return `${Number((resource.amount ?? 0).toFixed(1)).toLocaleString("zh-CN")}${resource.unit}`;
}
type Preset = {
  base_url: string;
  model: string;
  api_key_env: string;
  api_key_url: string;
};
type RoleSetting = { provider: string; model: string };
type Settings = {
  provider: string;
  base_url: string;
  model: string;
  api_key?: string;
  image_provider: string;
  image_base_url: string;
  image_model: string;
  roles: Record<string, RoleSetting>;
  presets: Record<string, Preset>;
};
type Usage = { total_tokens: number; estimated_cost: number; calls: Array<{ role: string }> };
type HistoryEntry = { entry_id?: string; id?: string; type?: string; kind?: string; turn?: number };

const roleLabels: Record<string, string> = {
  edict_interpreter: "诏令解释",
  narrator: "纪事撰写",
  character_chat: "人物对话",
  court_debate: "朝议辩论",
  edict_polish: "诏令润色",
  chronicler: "史册整理",
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
    const raw = body.detail as { message?: unknown; code?: unknown } | unknown[] | string | undefined;
    const detail = Array.isArray(raw)
      ? raw.join("；")
      : raw && typeof raw === "object" && typeof (raw as { message?: unknown }).message === "string"
        ? (raw as { message: string }).message
        : raw;
    const code = raw && typeof raw === "object" && !Array.isArray(raw) && typeof raw.code === "string" ? raw.code : "";
    throw new ApiError(typeof detail === "string" ? detail : `请求失败 (${response.status})`, code);
  }
  return response.json() as Promise<T>;
}

function assetUrl(scenarioId: string, assetId: string): string {
  return `/api/assets/${encodeURIComponent(scenarioId)}/${assetId}`;
}

function App() {
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [scenarioId, setScenarioId] = useState("");
  const [state, setState] = useState<State | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [usage, setUsage] = useState<Usage>({ total_tokens: 0, estimated_cost: 0, calls: [] });
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [report, setReport] = useState<TurnReport | null>(null);
  const [memorial, setMemorial] = useState("");
  const [edictText, setEdictText] = useState<Record<string, string>>({});
  const [selectedRegion, setSelectedRegion] = useState("");
  const [selectedCharacter, setSelectedCharacter] = useState("");
  const [chatText, setChatText] = useState("");
  const [chatLog, setChatLog] = useState<string[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [historyEntry, setHistoryEntry] = useState("");
  const [probeStatus, setProbeStatus] = useState("");
  const [view, setView] = useState<View | null>(null);
  const [adopted, setAdopted] = useState<Record<string, string>>({});
  const [draftBlocked, setDraftBlocked] = useState(false);

  const scenario = scenarios.find((item) => item.id === scenarioId) ?? null;
  const resources = useMemo(() => Object.values(state?.resources ?? {}), [state]);
  const characters = useMemo(() => Object.values(state?.characters ?? {}), [state]);
  const regions = useMemo(() => Object.values(state?.regions ?? {}), [state]);
  const selectedRegionData = regions.find((region) => region.id === selectedRegion) ?? null;

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api<SessionRow[]>("/api/sessions"));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取存档");
    }
  }, []);

  // /view 是前端主数据：奏疏、待决事件、目标、危机计、朝会日程、新手引导、终局
  const refreshView = useCallback(async (id: string) => {
    try {
      const next = await api<View>(`/api/sessions/${id}/view`);
      setView(next);
      setState(next.state);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取局势");
    }
  }, []);

  const refreshUsage = useCallback(async () => {
    try {
      setUsage(await api<Usage>("/api/usage"));
    } catch {
      setUsage({ total_tokens: 0, estimated_cost: 0, calls: [] });
    }
  }, []);

  useEffect(() => {
    void api<Scenario[]>("/api/scenarios").then((items) => {
      setScenarios(items);
      setScenarioId(items[0]?.id ?? "");
    }).catch((error: unknown) => setStatus(error instanceof Error ? error.message : "后端未连接"));
    void refreshSessions();
    void refreshUsage();
    const timer = window.setInterval(() => void refreshUsage(), 8000);
    return () => window.clearInterval(timer);
  }, [refreshSessions, refreshUsage]);

  useEffect(() => {
    const styleId = "scenario-theme";
    let style = document.getElementById(styleId) as HTMLStyleElement | null;
    if (!style) {
      style = document.createElement("style");
      style.id = styleId;
      document.head.append(style);
    }
    style.textContent = scenario?.theme_css ?? "";
  }, [scenario]);

  useEffect(() => {
    if (scenario?.categories) {
      setEdictText((previous) => Object.fromEntries(
        scenario.categories.map((category) => [category.id, previous[category.id] ?? ""]),
      ));
    }
  }, [scenario]);

  const createSession = async () => {
    if (!scenarioId) return;
    setBusy(true);
    setStatus("");
    try {
      const created = await api<{ id: string; state: State }>("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ scenario: scenarioId, seed: 0 }),
      });
      setSessionId(created.id);
      setState(created.state);
      setAdopted({});
      await refreshView(created.id);
      setReceipt(null);
      setReport(null);
      setMemorial("");
      setChatLog([]);
      await refreshSessions();
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法创建存档");
    } finally {
      setBusy(false);
    }
  };

  const openSession = async (id: string) => {
    setBusy(true);
    try {
      const loaded = await api<State>(`/api/sessions/${id}/state`);
      setSessionId(id);
      setState(loaded);
      setScenarioId(loaded.scenario_id);
      setAdopted({});
      await refreshView(id);
      setReceipt(null);
      setReport(null);
      setMemorial("");
      setSelectedRegion("");
      setChatLog([]);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法打开存档");
    } finally {
      setBusy(false);
    }
  };

  const draftEdict = async () => {
    if (!sessionId) return;
    setBusy(true);
    setStatus("");
    try {
      const draft = await api<Receipt>(`/api/sessions/${sessionId}/edicts/draft`, {
        method: "POST",
        body: JSON.stringify({
          sections: edictText,
          adopted: Object.entries(adopted).map(([memorial_id, option_id]) => ({ memorial_id, option_id })),
        }),
      });
      setReceipt(draft);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "诏令解释失败");
    } finally {
      setBusy(false);
      void refreshUsage();
    }
  };

  const confirmEdict = async () => {
    if (!sessionId || !receipt) return;
    setBusy(true);
    try {
      const confirmed = await api<Receipt>(
        `/api/sessions/${sessionId}/edicts/${receipt.id}/confirm`,
        { method: "POST" },
      );
      setReceipt(confirmed);
      setAdopted({});
      setDraftBlocked(false);
      await refreshView(sessionId);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "确认诏令失败");
    } finally {
      setBusy(false);
    }
  };

  // 本季有拟而未确认的草案时后端返回 409 DRAFT_UNCONFIRMED：先确认，或明确放弃后再推进
  const advanceTurn = async (discardDrafts = false) => {
    if (!sessionId) return;
    setBusy(true);
    setStatus("");
    try {
      const result = await api<{ state: State; report: TurnReport; memorial: string }>(
        `/api/sessions/${sessionId}/turn`,
        { method: "POST", body: JSON.stringify({ discard_drafts: discardDrafts }) },
      );
      setState(result.state);
      setReport(result.report);
      setMemorial(result.memorial);
      setReceipt(null);
      setAdopted({});
      setDraftBlocked(false);
      await refreshView(sessionId);
      await refreshSessions();
    } catch (error) {
      if (error instanceof ApiError && error.code === "DRAFT_UNCONFIRMED") setDraftBlocked(true);
      setStatus(error instanceof Error ? error.message : "推进回合失败");
    } finally {
      setBusy(false);
      void refreshUsage();
    }
  };

  const choose = async (choiceId: string, optionId: string) => {
    if (!sessionId) return;
    setBusy(true);
    try {
      await api(`/api/sessions/${sessionId}/choices/${choiceId}`, { method: "POST", body: JSON.stringify({ option_id: optionId }) });
      await refreshView(sessionId);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "处置失败");
    } finally {
      setBusy(false);
    }
  };

  const toggleAdopt = (memorialId: string, optionId: string) => {
    setAdopted((previous) => {
      const next = { ...previous };
      if (next[memorialId] === optionId) delete next[memorialId];
      else next[memorialId] = optionId;
      return next;
    });
  };

  const sendChat = async () => {
    if (!sessionId || !selectedCharacter || !chatText.trim()) return;
    const outgoing = chatText.trim();
    setChatText("");
    setChatLog((previous) => [...previous, `我：${outgoing}`]);
    setBusy(true);
    try {
      const response = await fetch(
        `/api/sessions/${sessionId}/chat/${selectedCharacter}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: outgoing }),
        },
      );
      if (!response.ok || !response.body) {
        // 朝会日程已满（409 AGENDA_EXHAUSTED）等错误在流开始前返回
        const body = (await response.json().catch(() => ({}))) as { detail?: { message?: string } };
        throw new Error(body.detail?.message ?? "对话连接失败");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let answer = "";
      let finished = false;
      while (!finished) {
        const chunk = await reader.read();
        buffer += decoder.decode(chunk.value, { stream: !chunk.done });
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          const event = block.match(/^event: (.+)$/m)?.[1];
          const data = block.match(/^data: (.+)$/m)?.[1];
          if (event === "message" && data) answer += (JSON.parse(data) as { text: string }).text;
          if (event === "error" && data) answer += `（${(JSON.parse(data) as { message: string }).message}）`;
          if (event === "done") finished = true;
        }
        if (chunk.done) finished = true;
      }
      setChatLog((previous) => [...previous, `人物：${answer || "（未收到回复）"}`]);
      void refreshView(sessionId);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "对话失败");
    } finally {
      setBusy(false);
      void refreshUsage();
    }
  };

  const forkSession = async (entryId?: string) => {
    if (!sessionId) return;
    setBusy(true);
    try {
      const fork = await api<{ id: string; state: State }>(`/api/sessions/${sessionId}/fork`, {
        method: "POST",
        body: JSON.stringify({ entry_id: entryId || null }),
      });
      setSessionId(fork.id);
      setState(fork.state);
      setAdopted({});
      await refreshView(fork.id);
      await refreshSessions();
      setStatus("已从所选记录分出新存档");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "分支存档失败");
    } finally {
      setBusy(false);
    }
  };

  const loadHistory = async () => {
    if (!sessionId) return;
    try {
      setHistory(await api<HistoryEntry[]>(`/api/sessions/${sessionId}/history`));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取历史");
    }
  };

  const openSettings = async () => {
    try {
      setSettings(await api<Settings>("/api/settings"));
      setApiKeyInput("");
      setSettingsOpen(true);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "无法读取设置");
    }
  };

  const saveSettings = async () => {
    if (!settings) return;
    const body: Record<string, unknown> = {
      provider: settings.provider,
      base_url: settings.base_url,
      model: settings.model,
      image_provider: settings.image_provider,
      image_base_url: settings.image_base_url,
      image_model: settings.image_model,
      roles: settings.roles,
    };
    if (apiKeyInput) body.api_key = apiKeyInput;
    try {
      const saved = await api<Settings>("/api/settings", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      setSettings(saved);
      setApiKeyInput("");
      setStatus("设置已保存到本机");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "设置保存失败");
    }
  };

  const testSettings = async () => {
    setProbeStatus("正在探测");
    try {
      const result = await api<{ tool_calling: boolean; json_fallback: boolean }>(
        "/api/settings/test",
        { method: "POST" },
      );
      setProbeStatus(result.tool_calling ? "工具调用可用" : "将使用 JSON 回退");
    } catch (error) {
      setProbeStatus(error instanceof Error ? error.message : "探测失败");
    }
  };

  const updateRole = (role: string, field: keyof RoleSetting, value: string) => {
    if (!settings) return;
    const existingRole = settings.roles?.[role] ?? { provider: "default", model: "default" };
    setSettings({
      ...settings,
      roles: {
        ...settings.roles,
        [role]: { ...existingRole, [field]: value },
      },
    });
  };

  const currentAssetIds = scenario?.asset_ids ?? [];
  const iconAsset = (id: string) => currentAssetIds.includes(id) ? assetUrl(scenarioId, id) : "";

  return (
    <div className="app-shell">
      <header
        className="topbar"
        style={{ backgroundImage: `linear-gradient(90deg, rgba(21, 39, 35, .96), rgba(21, 39, 35, .72)), url("${iconAsset("ui.topbar.background")}")` }}
      >
        <div className="brand">
          <span className="seal">政</span>
          <div><strong>本地朝局</strong><small>LLM 模拟游戏模板</small></div>
        </div>
        <div className="top-status">
          <span>{scenario?.title ?? "选择场景"}</span>
          <span>第 {state?.turn ?? 0} 回合</span>
          <span>{state?.calendar.label ?? "尚未开局"}</span>
          <span className="resource-strip" data-testid="desk.resources">
            {resources.map((resource) => (
              <span className={resource.hidden ? "resource-chip hidden" : "resource-chip"} key={resource.id} title={resource.display}>
                {resource.name} <b>{resourceText(resource)}</b>
              </span>
            ))}
            {view?.crisis?.title && (
              <span className="resource-chip crisis" title={(view.crisis.terms ?? []).map((term) => `${term.label} ${term.display}`).join("；")}>
                {view.crisis.title} <b>{view.crisis.display} · {view.crisis.band}</b>
              </span>
            )}
            {view?.agenda && <span className="resource-chip">{view.agenda.text}</span>}
          </span>
        </div>
        <div className="top-actions">
          <span className="usage-chip">{usage.total_tokens.toLocaleString("zh-CN")} tokens · ¥{usage.estimated_cost.toFixed(3)}</span>
          <button className="ghost-button" onClick={() => void openSettings()}>设置</button>
        </div>
      </header>

      {!state && (
        <main className="welcome card">
          <div className="welcome-art"><img src={iconAsset("ui.map.base") || "/missing.svg"} alt="" /></div>
          <div>
            <p className="eyebrow">LOCAL-FIRST · BYOK</p>
            <h1>从一道诏令，开始治理天下</h1>
            <p>规则与存档留在本机。无需密钥即可使用模拟模型完整体验回合流程。</p>
            <div className="start-row">
              <select value={scenarioId} onChange={(event) => setScenarioId(event.target.value)}>
                {scenarios.map((item) => <option key={item.id} value={item.id}>{item.title} · {item.era}</option>)}
              </select>
              <button className="primary-button" disabled={!scenarioId || busy} onClick={() => void createSession()}>
                {busy ? "正在开局…" : "开始新存档"}
              </button>
            </div>
            <p className="muted">可选场景：架空模板与崇祯初政。自定义兼容服务请在「设置」中配置。</p>
          </div>
        </main>
      )}

      {state && (
        <main className="dashboard">
          <aside className="left-rail">
            <section className="card session-card">
              <div className="panel-heading"><h2>存档</h2><button onClick={() => void createSession()}>新建</button></div>
              <p className="session-id">当前：{sessionId.slice(0, 10)}</p>
              <div className="session-list">
                {sessions.map((item) => (
                  <button className={item.id === sessionId ? "session-item active" : "session-item"} key={item.id} onClick={() => void openSession(item.id)}>
                    <span>{scenarios.find((row) => row.id === item.scenario)?.title ?? item.scenario}</span>
                    <small>第 {item.turn} 回合 · {item.id.slice(0, 6)}</small>
                  </button>
                ))}
              </div>
              <div className="button-row">
                <button onClick={() => void forkSession()}>分支存档</button>
                <button onClick={() => void loadHistory()}>历史记录</button>
              </div>
              {history.length > 0 && (
                <div className="history-row">
                  <select value={historyEntry} onChange={(event) => setHistoryEntry(event.target.value)}>
                    <option value="">选择历史节点</option>
                    {history.map((item, index) => {
                      const id = item.entry_id ?? item.id ?? "";
                      return <option key={id || index} value={id}>{item.type ?? item.kind ?? "记录"} · {item.turn ?? index}</option>;
                    })}
                  </select>
                  <button disabled={!historyEntry} onClick={() => void forkSession(historyEntry)}>从此处分叉</button>
                </div>
              )}
            </section>

            <section className="card map-card">
              <div className="panel-heading"><h2>疆域图</h2><span className="muted">{regions.length} 地区</span></div>
              {currentAssetIds.includes("ui.map.base") && <img className="map-base" src={assetUrl(scenarioId, "ui.map.base")} alt="" />}
              <svg className="map-svg" viewBox="0 0 100 60" role="img" aria-label="地区地图">
                {regions.map((region) => {
                  const points = region.polygon.length
                    ? region.polygon.map((point) => point.join(",")).join(" ")
                    : `${region.x - 4},${region.y - 3} ${region.x + 4},${region.y - 3} ${region.x + 4},${region.y + 3} ${region.x - 4},${region.y + 3}`;
                  return (
                    <g key={region.id} onClick={() => setSelectedRegion(region.id)} className="map-region">
                      <polygon points={points} className={selectedRegion === region.id ? "selected" : ""} />
                      <text x={region.x} y={region.y} textAnchor="middle">{region.name}</text>
                    </g>
                  );
                })}
              </svg>
              {selectedRegionData && (
                <div className="region-drawer">
                  <strong>{selectedRegionData.name}</strong>
                  <button aria-label="关闭地区详情" onClick={() => setSelectedRegion("")}>×</button>
                  <p>邻接：{Object.keys(selectedRegionData.adjacency).map((id) => state.regions[id]?.name ?? id).join("、") || "无"}</p>
                  <p>{Object.entries(selectedRegionData.stats).map(([key, value]) => `${key} ${value}`).join(" · ")}</p>
                </div>
              )}
            </section>

            <section className="card list-card">
              <div className="panel-heading"><h2>朝臣</h2><span className="muted">{characters.length} 人</span></div>
              <div className="character-list">
                {characters.map((character) => (
                  <button
                    className={selectedCharacter === character.id ? "character-row selected" : "character-row"}
                    key={character.id}
                    onClick={() => setSelectedCharacter(character.id)}
                  >
                    <img src={assetUrl(scenarioId, `character.${character.id}.portrait`)} alt="" />
                    <span><b>{character.name}</b><small>{character.office ? state.offices[character.office]?.name ?? character.office : "在野"} · {character.status}</small></span>
                  </button>
                ))}
              </div>
            </section>
          </aside>

          <section className="main-column">
            {view && view.tutorial.steps.length > 0 && (
              <section className="card tutorial-card" data-testid="tutorial.panel">
                {view.tutorial.steps.map((step) => <p key={step.step}><b>{step.step}. {step.title}</b> {step.text}</p>)}
              </section>
            )}
            {view?.final_notice && <p className="warning">{view.final_notice.text}</p>}
            {view?.outcome && (
              <section className="card outcome-card" data-testid="ending.panel">
                <p className="eyebrow">{view.outcome.kind === "fail" ? "FAIL" : "ENDING"}</p>
                <h2>{view.outcome.title}</h2>
                <p>{view.outcome.text}</p>
                {view.outcome.satisfied.length > 0 && <ul>{view.outcome.satisfied.map((line) => <li key={line}>{line}</li>)}</ul>}
              </section>
            )}
            {view && view.objectives.length > 0 && (
              <section className="card objectives-card">
                {view.objectives.map((row) => (
                  <span className={`objective ${row.status}`} key={row.id} title={row.detail}>{row.title} · {objectiveLabels[row.status] ?? row.status}</span>
                ))}
              </section>
            )}
            {view && view.memorials.length > 0 && (
              <section className="card memorial-list-card" data-testid="memorial.list">
                <div className="panel-heading"><h2>本季奏疏</h2><span className="muted">批答即加入诏书草案</span></div>
                {view.memorials.map((item) => (
                  <article className="memorial-item" key={item.id} data-testid={`memorial.item.${item.id}`}>
                    <h3>{item.title}{item.arrival && <small>（{item.arrival}）</small>}</h3>
                    <p>{item.summary}</p>
                    {item.advisors.map((advisor) => <p className="advisor" key={advisor.character}>{advisor.name}（{stanceLabels[advisor.stance] ?? advisor.stance}）：{advisor.line}</p>)}
                    <div className="option-row">
                      {item.options.map((option) => (
                        <button
                          key={option.id}
                          className={adopted[item.id] === option.id ? "option-button selected" : "option-button"}
                          data-testid={`memorial.option.${item.id}.${option.id}`}
                          title={option.ops_summary.join("；")}
                          onClick={() => toggleAdopt(item.id, option.id)}
                        >
                          {option.label}{option.hint && <small>{option.hint}</small>}
                        </button>
                      ))}
                    </div>
                  </article>
                ))}
              </section>
            )}
            {view && view.pending_choices.length > 0 && (
              <section className="card choice-card" data-testid="choice.list">
                <div className="panel-heading"><h2>待决事件</h2><span className="muted">逾期按默认处置</span></div>
                {view.pending_choices.map((choice) => (
                  <article className="memorial-item" key={choice.id}>
                    <h3>{choice.title}</h3>
                    <p>{choice.text}</p>
                    <div className="option-row">
                      {choice.options.map((option) => (
                        <button key={option.id} className="option-button" disabled={busy} onClick={() => void choose(choice.id, option.id)}>
                          {option.text}{option.id === choice.default_option && <small>默认</small>}
                        </button>
                      ))}
                    </div>
                  </article>
                ))}
              </section>
            )}
            <section className="card edict-card" style={{ backgroundImage: `linear-gradient(rgba(248,244,234,.94), rgba(248,244,234,.97)), url("${iconAsset("ui.edict.parchment")}")` }}>
              <div className="panel-heading"><div><p className="eyebrow">IMPERIAL EDICT</p><h2>诏令拟定</h2></div><span className="muted">先预览，再确认生效</span></div>
              {view?.provider_mode === "offline" && <p className="muted">未接模型：御笔只记入史册，不生效力；请批答奏疏（或在设置中接入模型）。</p>}
              {Object.keys(adopted).length > 0 && <p className="muted">已批答 {Object.keys(adopted).length} 条奏疏，将随本诏一并预览。</p>}
              <div className="edict-fields">
                {(scenario?.categories ?? []).map((category) => (
                  <label key={category.id}>
                    <span>{category.name}<small>{category.hint}</small></span>
                    <textarea value={edictText[category.id] ?? ""} onChange={(event) => setEdictText({ ...edictText, [category.id]: event.target.value })} placeholder={`写下${category.name}安排…`} />
                  </label>
                ))}
              </div>
              <div className="button-row align-right">
                <button data-testid="edict.preview" disabled={busy || Boolean(view?.outcome)} onClick={() => void draftEdict()}>生成预览</button>
                <button data-testid="edict.confirm" className="primary-button" disabled={!receipt || receipt.confirmed || busy} onClick={() => void confirmEdict()}>确认诏令</button>
                {draftBlocked && <button onClick={() => void advanceTurn(true)}>放弃草案并推进</button>}
                <button data-testid="desk.advance" className="advance-button" disabled={busy || Boolean(view?.outcome)} onClick={() => void advanceTurn()}>推进一回合 →</button>
              </div>
              {receipt && (
                <div className="receipt">
                  <div className="panel-heading"><h3>执行回执 {receipt.confirmed ? "· 已确认" : "· 待确认"}</h3><span>{receipt.clauses.length} 条款</span></div>
                  {receipt.clauses.map((clause, index) => (
                    <div className="receipt-row" key={`${clause.category}-${index}`}>
                      <span className={`pill ${clause.status}`}>{clause.status}</span>
                      <div><b>{clause.category}</b><p>{clause.summary || clause.text}</p>{(clause.reason || clause.delay_reason) && <small>{clause.reason || clause.delay_reason}</small>}</div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            <div className="two-column">
              <section className="card memorial-card" style={{ backgroundImage: `linear-gradient(rgba(248,244,234,.92), rgba(248,244,234,.98)), url("${iconAsset("ui.memorial.banner")}")` }}>
                <div className="panel-heading"><h2>起居纪事</h2><span className="muted">仅记录已确认事实</span></div>
                {memorial ? <p className="memorial-text">{memorial}</p> : <p className="empty-state">推进回合后，将在此显示纪事。</p>}
                {report && <div className="fact-list">{report.facts.map((fact, index) => <p key={`${fact.kind}-${index}`}>{fact.text}</p>)}</div>}
                {report?.reconciliation && <div className="fact-list">{report.reconciliation.map((row) => row.display && <p key={row.resource}>{row.display}</p>)}</div>}
                {report?.warnings.map((warning) => <p className="warning" key={warning}>{warning}</p>)}
              </section>
              <section className="card ledger-card">
                <div className="panel-heading"><h2>账目与工程</h2><span className="muted">{state.ledger.length} 账项</span></div>
                <div className="ledger-list">
                  {state.ledger.map((entry) => (
                    <div className="ledger-row" key={entry.id}><span>{entry.name}<small>{entry.kind}{entry.turns ? ` · 剩余 ${entry.turns} 回合` : ""}</small></span><b>{entry.amount > 0 ? "+" : ""}{entry.amount}</b></div>
                  ))}
                  {state.ledger.length === 0 && <p className="empty-state">没有待结算账项</p>}
                </div>
                <h3 className="subheading">工程</h3>
                {state.projects.map((project) => <div className="project-row" key={project.id}><span>{project.name}</span><span>{project.progress}/{project.turns}{project.stalled ? " · 停工" : ""}</span></div>)}
                {state.projects.length === 0 && <p className="empty-state">当前没有工程</p>}
                {state.pending_clauses.length > 0 && <p className="muted">待执行延期条款：{state.pending_clauses.length}</p>}
                {state.standing_orders.length > 0 && <p className="muted">常设命令：{state.standing_orders.length}</p>}
              </section>
            </div>

            <section className="card chat-card">
              <div className="panel-heading"><div><p className="eyebrow">PRIVATE AUDIENCE</p><h2>人物问答</h2></div><span className="muted">{selectedCharacter ? state.characters[selectedCharacter]?.name : "先选择一位朝臣"}</span></div>
              <div className="chat-log">
                {chatLog.length ? chatLog.map((line, index) => <p className={line.startsWith("我：") ? "chat-player" : "chat-character"} key={`${index}-${line}`}>{line}</p>) : <p className="empty-state">对话通过本地 API 流式返回。</p>}
              </div>
              <div className="chat-input"><input value={chatText} onChange={(event) => setChatText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void sendChat(); }} placeholder="向选中的人物询问…" /><button disabled={!selectedCharacter || busy || !chatText.trim()} onClick={() => void sendChat()}>发送</button></div>
            </section>
          </section>
        </main>
      )}

      {settingsOpen && settings && (
        <div className="modal-backdrop" role="presentation" onClick={() => setSettingsOpen(false)}>
          <section className="settings-modal card" role="dialog" aria-modal="true" aria-label="模型设置" onClick={(event) => event.stopPropagation()}>
            <div className="panel-heading"><div><p className="eyebrow">LOCAL PROVIDERS</p><h2>模型与图像设置</h2></div><button onClick={() => setSettingsOpen(false)}>关闭</button></div>
            <p className="muted">密钥只保存在运行本机后端的设置文件中；留空表示保留已保存密钥。</p>
            <div className="settings-grid">
              <label>默认提供方<select value={settings.provider} onChange={(event) => setSettings({ ...settings, provider: event.target.value })}>
                {["mock", ...Object.keys(settings.presets)].map((provider) => <option key={provider}>{provider}</option>)}
              </select></label>
              <label>模型<input value={settings.model} onChange={(event) => setSettings({ ...settings, model: event.target.value })} /></label>
              <label className="wide">兼容 API 地址<input value={settings.base_url} onChange={(event) => setSettings({ ...settings, base_url: event.target.value })} /></label>
              <label className="wide">API 密钥<input type="password" value={apiKeyInput} onChange={(event) => setApiKeyInput(event.target.value)} placeholder={settings.api_key ? "已保存密钥；留空保持" : "留空使用环境变量或 Mock"} /></label>
              <label>图像提供方<select value={settings.image_provider} onChange={(event) => setSettings({ ...settings, image_provider: event.target.value })}><option value="mock">mock</option><option value="openai">OpenAI 兼容</option></select></label>
              <label>图像模型<input value={settings.image_model} onChange={(event) => setSettings({ ...settings, image_model: event.target.value })} /></label>
              <label className="wide">图像 API 地址<input value={settings.image_base_url} onChange={(event) => setSettings({ ...settings, image_base_url: event.target.value })} /></label>
            </div>
            <h3 className="subheading">按角色路由</h3>
            <div className="role-grid">
              {Object.keys(roleLabels).map((role) => {
                const value = settings.roles?.[role] ?? { provider: "default", model: "default" };
                return (
                  <div className="role-row" key={role}>
                    <b>{roleLabels[role]}</b>
                    <select value={value.provider} onChange={(event) => updateRole(role, "provider", event.target.value)}>
                      {["default", "mock", ...Object.keys(settings.presets)].map((provider) => <option key={provider}>{provider}</option>)}
                    </select>
                    <input value={value.model} onChange={(event) => updateRole(role, "model", event.target.value)} aria-label={`${roleLabels[role]}模型`} />
                  </div>
                );
              })}
            </div>
            <div className="button-row align-right">
              <button onClick={() => void testSettings()}>测试工具能力</button>
              <button className="primary-button" onClick={() => void saveSettings()}>保存设置</button>
            </div>
            {probeStatus && <p className="muted">{probeStatus}</p>}
            {settings.presets[settings.provider]?.api_key_url && <a href={settings.presets[settings.provider].api_key_url} target="_blank" rel="noreferrer">获取此提供方密钥</a>}
          </section>
        </div>
      )}
      {status && <div className="toast" role="status"><span>{status}</span><button onClick={() => setStatus("")}>×</button></div>}
    </div>
  );
}

export default App;
