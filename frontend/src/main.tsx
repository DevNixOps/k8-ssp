import { ReactNode, StrictMode, useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, ApiError, login, Me, post, setToken } from "./api";
import LogViewer, { Drawer } from "./LogViewer";
import "./styles.css";

type Dep = { name: string; replicas: number; ready: number; image: string };
type Pod = { name: string; phase: string; ready: boolean; restarts: number; node: string; containers: string[]; image: string; created: string | null };
type Svc = { name: string; type: string; cluster_ip: string; ports: string[] };
type Ev = { type: string; reason: string; message: string; object: string; time: string | null };
type Rel = { name: string; namespace: string; chart: string; app_version: string; revision: number; status: string };
type RelDetail = Rel & { values: unknown; history: { revision: number; status: string }[] };
type Cm = { name: string; keys: string[]; created: string | null };
type Sec = { name: string; type: string; keys: string[]; created: string | null };
type Kind = "ok" | "warn" | "bad" | "muted";
type View = "deployments" | "pods" | "services" | "configmaps" | "secrets" | "helm" | "events" | "logs";

const ACTIONS: { key: View; icon: string; label: string; hint: string; minRole?: string }[] = [
  { key: "deployments", icon: "🚀", label: "List deployments", hint: "Status, scale & restart" },
  { key: "pods", icon: "📦", label: "List pods", hint: "Phase, restarts, node" },
  { key: "services", icon: "🔌", label: "List services", hint: "Types, IPs and ports" },
  { key: "configmaps", icon: "🗂️", label: "List configmaps", hint: "Names and keys" },
  { key: "secrets", icon: "🔐", label: "List secrets", hint: "Names and key names only", minRole: "developer" },
  { key: "helm", icon: "⎈", label: "List Helm releases", hint: "Charts, revisions, values" },
  { key: "events", icon: "⚡", label: "List events", hint: "Recent cluster events" },
  { key: "logs", icon: "📜", label: "Get pod logs", hint: "Live tail or snapshot" },
];
const RANK: Record<string, number> = { viewer: 1, developer: 2, admin: 3 };

function useFetch<T>(path: string | null, refreshMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  useEffect(() => {
    setData(null);
    setError("");
  }, [path]);
  useEffect(() => {
    if (!path) return;
    let off = false;
    const load = () =>
      api<T>(path).then((d) => !off && (setData(d), setError("")), (e) => !off && setError(e.message));
    load();
    const id = refreshMs ? setInterval(load, refreshMs) : undefined;
    return () => {
      off = true;
      if (id) clearInterval(id);
    };
  }, [path, refreshMs, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}

const age = (iso: string | null) => {
  if (!iso) return "–";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 129600) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
};

const Pill = ({ kind, children }: { kind: Kind; children: ReactNode }) => (
  <span className={`pill ${kind}`}><i />{children}</span>
);

const podKind = (p: Pod): Kind =>
  p.phase === "Running" && p.ready ? "ok" : p.phase === "Succeeded" ? "muted" : ["Pending", "ContainerCreating", "Running"].includes(p.phase) ? "warn" : "bad";

const Keys = ({ keys }: { keys: string[] }) => (
  <div className="chips">{keys.length ? keys.map((k) => <code key={k}>{k}</code>) : <span className="muted">none</span>}</div>
);

function Login({ onDone }: { onDone: () => void }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try { await login(u, p); onDone(); } catch (x) { setErr((x as Error).message); } finally { setBusy(false); }
      }}>
        <div className="logo big">⎈</div>
        <h2>Kubernetes Portal</h2>
        <p className="muted">Sign in to manage your workloads</p>
        <input placeholder="Username" value={u} onChange={(e) => setU(e.target.value)} autoFocus />
        <input placeholder="Password" type="password" value={p} onChange={(e) => setP(e.target.value)} />
        <button className="btn primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        {err && <p className="err">{err}</p>}
      </form>
    </div>
  );
}

function Table({ cols, rows, empty }: { cols: string[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="muted pad">{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>{rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function Result({ ns, view, me, toast }: { ns: string; view: View; me: Me; toast: (m: string, bad?: boolean) => void }) {
  const meta = ACTIONS.find((a) => a.key === view)!;
  const canAct = me.role !== "viewer";
  const base = `/namespaces/${ns}`;
  const path = view === "helm" ? "/helm" : `${base}/${view === "logs" ? "pods" : view}`;
  const [auto, setAuto] = useState(false);
  const { data, error, reload } = useFetch<any[]>(path, auto ? 5000 : 0);
  const [q, setQ] = useState("");
  const [warnOnly, setWarnOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [logPod, setLogPod] = useState<{ pod: Pod; live: boolean } | null>(null);
  const [rel, setRel] = useState<RelDetail | null>(null);

  const match = (s: string) => !q || s.toLowerCase().includes(q.toLowerCase());
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key);
    try { await fn(); toast(ok); reload(); } catch (e) { toast((e as ApiError).message, true); }
    setBusy("");
  };

  let body: ReactNode = null;
  let count = 0;
  if (data) {
    if (view === "deployments") {
      const l = (data as Dep[]).filter((d) => match(d.name)); count = l.length;
      const scale = (d: Dep, n: number) => act(`s-${d.name}`, () => post(`${base}/deployments/${d.name}/scale`, { replicas: n }), `${d.name} → ${n} replicas`);
      body = <Table empty="No deployments found." cols={["Name", "Ready", "Image", ...(canAct ? ["Actions"] : [])]}
        rows={l.map((d) => [
          <strong>{d.name}</strong>,
          <div className="ready"><div className="bar"><div className={d.ready === d.replicas ? "fill ok" : "fill warn"} style={{ width: `${d.replicas ? (d.ready / d.replicas) * 100 : 100}%` }} /></div><span>{d.ready}/{d.replicas}</span></div>,
          <code>{d.image}</code>,
          ...(canAct ? [<div className="actions">
            <div className="stepper">
              <button disabled={!!busy || d.replicas <= 0} title="Scale down" onClick={() => scale(d, d.replicas - 1)}>−</button>
              <span>{d.replicas}</span>
              <button disabled={!!busy || d.replicas >= 20} title="Scale up" onClick={() => scale(d, d.replicas + 1)}>+</button>
            </div>
            <button className="btn" disabled={!!busy} onClick={() => act(`r-${d.name}`, () => post(`${base}/deployments/${d.name}/restart`), `Restarting ${d.name}`)}>↻ Restart</button>
          </div>] : []),
        ])} />;
    } else if (view === "pods" || view === "logs") {
      const l = (data as Pod[]).filter((p) => match(p.name)); count = l.length;
      body = <Table empty="No pods found." cols={["Name", "Status", "Restarts", "Age", "Node", view === "logs" ? "Get logs" : "Image"]}
        rows={l.map((p) => [
          <strong>{p.name}</strong>, <Pill kind={podKind(p)}>{p.phase}</Pill>,
          p.restarts > 3 ? <span className="bad-text">{p.restarts}</span> : p.restarts, age(p.created), p.node,
          view === "logs"
            ? <div className="actions"><button className="btn primary sm" onClick={() => setLogPod({ pod: p, live: true })}>▶ Live tail</button><button className="btn sm" onClick={() => setLogPod({ pod: p, live: false })}>Snapshot</button></div>
            : <code>{p.image}</code>,
        ])} />;
    } else if (view === "services") {
      const l = (data as Svc[]).filter((s) => match(s.name)); count = l.length;
      body = <Table empty="No services found." cols={["Name", "Type", "Cluster IP", "Ports"]}
        rows={l.map((s) => [<strong>{s.name}</strong>, <Pill kind="muted">{s.type}</Pill>, s.cluster_ip, s.ports.join(", ")])} />;
    } else if (view === "configmaps") {
      const l = (data as Cm[]).filter((c) => match(c.name)); count = l.length;
      body = <Table empty="No configmaps found." cols={["Name", "Keys", "Age"]}
        rows={l.map((c) => [<strong>{c.name}</strong>, <Keys keys={c.keys} />, age(c.created)])} />;
    } else if (view === "secrets") {
      const l = (data as Sec[]).filter((s) => match(s.name)); count = l.length;
      body = <Table empty="No secrets found." cols={["Name", "Type", "Keys (values are never shown)", "Age"]}
        rows={l.map((s) => [<strong>{s.name}</strong>, <Pill kind="muted">{s.type}</Pill>, <Keys keys={s.keys} />, age(s.created)])} />;
    } else if (view === "helm") {
      const l = (data as Rel[]).filter((r) => r.namespace === ns && match(r.name)); count = l.length;
      body = <Table empty="No Helm releases in this namespace." cols={["Release", "Chart", "App version", "Revision", "Status", ""]}
        rows={l.map((r) => [
          <strong>{r.name}</strong>, r.chart, r.app_version, r.revision, <Pill kind={r.status === "deployed" ? "ok" : "warn"}>{r.status}</Pill>,
          <button className="btn sm" onClick={() => api<RelDetail>(`/namespaces/${r.namespace}/helm/${r.name}`).then(setRel, (e) => toast(e.message, true))}>Values & history</button>,
        ])} />;
    } else if (view === "events") {
      const l = (data as Ev[]).filter((e) => (!warnOnly || e.type !== "Normal") && match(`${e.object} ${e.message} ${e.reason}`)).slice(0, 50); count = l.length;
      body = <Table empty="No events." cols={["Type", "Reason", "Object", "Message", "Age"]}
        rows={l.map((e) => [<Pill kind={e.type === "Normal" ? "muted" : "warn"}>{e.type}</Pill>, e.reason, e.object, e.message, age(e.time)])} />;
    }
  }

  return (
    <section className="card">
      <div className="card-head">
        <span className="icon">{meta.icon}</span>
        <h3>{meta.label}</h3>
        <span className="muted">in</span><code>{ns}</code>
        {data && <span className="count">{count}</span>}
        <span className="spacer" />
        {view === "events" && <label className="check"><input type="checkbox" checked={warnOnly} onChange={(e) => setWarnOnly(e.target.checked)} /> warnings only</label>}
        <input className="mini" placeholder="Filter…" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto-refresh</label>
        <button className="btn sm" onClick={reload}>↻ Refresh</button>
      </div>
      {error ? <p className="err pad">{error}</p> : !data ? <div className="skeleton" /> : body}
      {logPod && <LogViewer key={logPod.pod.name + logPod.live} ns={ns} pod={logPod.pod.name} containers={logPod.pod.containers} startLive={logPod.live} onClose={() => setLogPod(null)} />}
      {rel && (
        <Drawer title={<><span className="muted">Helm ·</span> {rel.name}</>} onClose={() => setRel(null)}>
          <p className="muted">{rel.chart} · revision {rel.revision} · {rel.status}</p>
          <pre className="logs">{JSON.stringify({ values: rel.values, history: rel.history }, null, 2)}</pre>
        </Drawer>
      )}
    </section>
  );
}

function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [authed, setAuthed] = useState(!!sessionStorage.getItem("token"));
  const [ns, setNs] = useState("");
  const [view, setView] = useState<View | null>(null);
  const [toastMsg, setToastMsg] = useState<{ m: string; bad: boolean } | null>(null);
  const logout = useCallback(() => { setToken(null); setMe(null); setAuthed(false); setView(null); }, []);
  const toast = useCallback((m: string, bad = false) => {
    setToastMsg({ m, bad });
    setTimeout(() => setToastMsg(null), 3500);
  }, []);

  useEffect(() => {
    if (authed) api<Me>("/me").then(setMe, logout);
  }, [authed, logout]);
  const { data: namespaces } = useFetch<string[]>(authed ? "/namespaces" : null);
  useEffect(() => {
    if (namespaces?.length && !namespaces.includes(ns)) setNs(namespaces[0]);
  }, [namespaces, ns]);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  if (!me) return <p className="muted pad">Loading…</p>;

  return (
    <div className="app">
      <header>
        <div className="brand"><span className="logo">⎈</span><b>K8s Portal</b></div>
        <div className="ns-picker">
          <span className="muted">Namespace</span>
          <select value={ns} onChange={(e) => setNs(e.target.value)}>
            {(namespaces ?? []).map((n) => <option key={n}>{n}</option>)}
          </select>
        </div>
        <span className="spacer" />
        <span className="user"><b>{me.name}</b><em>{me.role}</em></span>
        <button className="btn ghost" onClick={logout}>Sign out</button>
      </header>
      <main>
        <div className="actions-grid">
          {ACTIONS.map((a) => {
            const locked = a.minRole ? RANK[me.role] < RANK[a.minRole] : false;
            return (
              <button key={a.key} disabled={!ns || locked} className={`action ${view === a.key ? "on" : ""}`}
                title={locked ? `Requires ${a.minRole} role` : a.hint} onClick={() => setView(a.key)}>
                <span className="icon">{a.icon}</span>
                <span><b>{a.label}</b><small>{locked ? `Requires ${a.minRole}` : a.hint}</small></span>
              </button>
            );
          })}
        </div>
        {!ns ? <p className="muted pad">No accessible namespaces.</p>
          : view ? <Result key={`${ns}-${view}`} ns={ns} view={view} me={me} toast={toast} />
          : <div className="empty card"><div className="logo big">⎈</div><h3>What would you like to do in <code>{ns}</code>?</h3><p className="muted">Choose an action above to get started.</p></div>}
      </main>
      {toastMsg && <div className={`toast ${toastMsg.bad ? "bad" : ""}`}>{toastMsg.m}</div>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
