import { ReactNode, useEffect, useRef, useState } from "react";
import { api, getToken } from "./api";

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="backdrop" onClick={onClose}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="drawer-head">
          <h3>{title}</h3>
          <button className="btn ghost" onClick={onClose}>✕ Close</button>
        </div>
        {children}
      </aside>
    </div>
  );
}

type Props = { ns: string; pod: string; containers: string[]; startLive?: boolean; onClose: () => void };

export default function LogViewer({ ns, pod, containers, startLive = true, onClose }: Props) {
  const [container, setContainer] = useState(containers[0] ?? "");
  const [live, setLive] = useState(startLive);
  const [previous, setPrevious] = useState(false);
  const [tail, setTail] = useState(200);
  const [filter, setFilter] = useState("");
  const [follow, setFollow] = useState(true);
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState("");
  const box = useRef<HTMLPreElement>(null);

  useEffect(() => {
    setLines([]);
    setStatus("");
    const base = `/namespaces/${ns}/pods/${pod}`;
    if (!live) {
      let cancelled = false;
      const q = new URLSearchParams({ tail: String(tail), previous: String(previous), container });
      api<{ logs: string }>(`${base}/logs?${q}`)
        .then((r) => !cancelled && setLines(r.logs.split("\n")))
        .catch((e) => !cancelled && setStatus(e.message));
      return () => {
        cancelled = true;
      };
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = new URLSearchParams({ token: getToken() ?? "", tail: String(tail), container });
    const ws = new WebSocket(`${proto}://${location.host}/api/ws${base}/logs?${q}`);
    setStatus("connecting…");
    ws.onopen = () => setStatus("live");
    ws.onmessage = (e) => setLines((l) => [...l, e.data].slice(-5000));
    ws.onclose = (e) => setStatus(e.code >= 4400 ? `rejected (${e.code})` : "disconnected");
    return () => ws.close();
  }, [ns, pod, container, live, previous, tail]);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  const shown = filter ? lines.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : lines;

  return (
    <Drawer
      onClose={onClose}
      title={
        <>
          <span className="muted">Logs ·</span> {pod}{" "}
          <span className={`pill ${status === "live" ? "ok" : "muted"}`}><i />{status || (live ? "live" : "snapshot")}</span>
        </>
      }
    >
      <div className="toolbar">
        <select value={container} onChange={(e) => setContainer(e.target.value)}>
          {containers.map((c) => <option key={c}>{c}</option>)}
        </select>
        <div className="seg">
          <button className={live ? "on" : ""} onClick={() => setLive(true)}>Live</button>
          <button className={!live ? "on" : ""} onClick={() => setLive(false)}>Snapshot</button>
        </div>
        <select value={tail} onChange={(e) => setTail(+e.target.value)}>
          {[50, 200, 500, 1000, 5000].map((n) => <option key={n} value={n}>last {n}</option>)}
        </select>
        {!live && <label className="check"><input type="checkbox" checked={previous} onChange={(e) => setPrevious(e.target.checked)} /> previous</label>}
        <input className="grow" placeholder="Filter lines…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <label className="check"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> autoscroll</label>
        <button className="btn" onClick={() => navigator.clipboard.writeText(shown.join("\n"))}>Copy</button>
      </div>
      <pre ref={box} className="logs">{shown.join("\n") || (status ? "" : "Waiting for logs…")}</pre>
    </Drawer>
  );
}
