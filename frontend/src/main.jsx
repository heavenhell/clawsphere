import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  Cpu,
  Database,
  FileClock,
  Send,
  ShieldAlert,
  Server,
  TerminalSquare,
  UserRound,
} from 'lucide-react';
import './styles.css';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8010';

const examples = [
  '我现在有哪些资源？',
  '现在有哪些严重告警？请解释原因和影响范围',
  'cluster-002 还能撑多久？给我容量预测',
  'dcs-app-01 为什么变慢？做一下 VM 性能诊断',
];

function metricPercent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function App() {
  const [conversationId] = useState(() => {
    const existing = localStorage.getItem('clawsphere-conversation-id');
    const value = existing || crypto.randomUUID();
    localStorage.setItem('clawsphere-conversation-id', value);
    return value;
  });
  const [overview, setOverview] = useState(null);
  const [tools, setTools] = useState([]);
  const [audit, setAudit] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content: '我已接入 FusionCompute mock 环境，可以演示告警解释、容量预测和 VM 性能诊断。',
    },
  ]);
  const [input, setInput] = useState('');
  const [lastTrace, setLastTrace] = useState(null);
  const [summary, setSummary] = useState('');
  const [loading, setLoading] = useState(false);
  const listRef = useRef(null);

  const refreshSideData = async () => {
    const [overviewRes, toolsRes, auditRes, approvalRes] = await Promise.all([
      fetch(`${API_BASE}/api/overview`),
      fetch(`${API_BASE}/api/tools`),
      fetch(`${API_BASE}/api/audit`),
      fetch(`${API_BASE}/api/approvals`),
    ]);
    setOverview(await overviewRes.json());
    setTools(await toolsRes.json());
    setAudit(await auditRes.json());
    setApprovals(await approvalRes.json());
  };

  useEffect(() => {
    refreshSideData();
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, loading]);

  const sendMessage = async (text = input) => {
    const content = text.trim();
    if (!content || loading) return;
    setInput('');
    setMessages((items) => [...items, { role: 'user', content }]);
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: content,
          roles: ['readonly'],
          history: messages.map((item) => ({
            role: item.role,
            content: item.content,
          })),
          summary,
          conversation_id: conversationId,
        }),
      });
      const data = await res.json();
      setLastTrace(data);
      setSummary(data.summary || summary);
      setMessages((items) => {
        const next = [...items, { role: 'assistant', content: data.answer }];
        return data.summary ? next.slice(-12) : next;
      });
      await refreshSideData();
    } catch (error) {
      setMessages((items) => [
        ...items,
        { role: 'assistant', content: `后端连接失败：${error.message}` },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const riskLabel = useMemo(() => {
    if (!overview?.capacity_risks?.length) return '低';
    return overview.capacity_risks.some((item) => item.free_gb / item.capacity_gb < 0.1) ? '高' : '中';
  }, [overview]);

  return (
    <main className="shell">
      <section className="leftPane">
        <header className="brand">
          <div className="brandMark"><Bot size={22} /></div>
          <div>
            <h1>DCS Copilot</h1>
            <p>FusionCompute 运维 Agent Demo</p>
          </div>
        </header>

        <div className="statusGrid">
          <Stat icon={<Server />} label="集群" value={overview?.cluster_count ?? '-'} />
          <Stat icon={<Cpu />} label="主机" value={overview?.host_count ?? '-'} />
          <Stat icon={<Activity />} label="VM" value={overview?.vm_count ?? '-'} />
          <Stat icon={<AlertTriangle />} label="活跃告警" value={overview?.active_alarm_count ?? '-'} warn />
        </div>

        <section className="panel">
          <h2><BarChart3 size={16} />资源态势</h2>
          <Gauge label="CPU 平均使用率" value={overview?.avg_cpu_usage ?? 0} />
          <Gauge label="内存平均使用率" value={overview?.avg_memory_usage ?? 0} />
          <div className={`riskBadge risk${riskLabel}`}>容量风险：{riskLabel}</div>
        </section>

        <section className="panel">
          <h2><Database size={16} />容量风险对象</h2>
          {(overview?.capacity_risks || []).map((item) => (
            <div className="resourceRow" key={item.id}>
              <span>{item.name}</span>
              <strong>{item.free_gb}GB free</strong>
            </div>
          ))}
          {!overview?.capacity_risks?.length && <p className="muted">暂无高风险存储对象</p>}
        </section>
      </section>

      <section className="chatPane">
        <div className="chatHeader">
          <div>
            <h2>运维问答</h2>
            <p>只读演示链路，写操作会被护栏拦截</p>
          </div>
          <span className="live"><CheckCircle2 size={14} /> mock online</span>
        </div>

        <div className="exampleBar">
          {examples.map((item) => (
            <button key={item} type="button" onClick={() => sendMessage(item)}>{item}</button>
          ))}
        </div>

        <div className="messageList" ref={listRef}>
          {messages.map((message, index) => (
            <div className={`message ${message.role}`} key={`${message.role}-${index}`}>
              <div className="avatar">{message.role === 'user' ? <UserRound size={16} /> : <Bot size={16} />}</div>
              <div className="bubble">{message.content}</div>
            </div>
          ))}
          {loading && (
            <div className="message assistant">
              <div className="avatar"><Bot size={16} /></div>
              <div className="bubble typing">正在调用工具并生成诊断...</div>
            </div>
          )}
        </div>

        <form className="composer" onSubmit={(event) => { event.preventDefault(); sendMessage(); }}>
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="输入问题，例如：cluster-002 还能撑多久？"
          />
          <button type="submit" aria-label="发送"><Send size={18} /></button>
        </form>
      </section>

      <section className="rightPane">
        <section className="panel tracePanel">
          <h2><TerminalSquare size={16} />工具调用轨迹</h2>
          {lastTrace?.plan?.length ? (
            <div className="planBox">
              {lastTrace.plan.map((step) => <span key={step}>{step}</span>)}
            </div>
          ) : null}
          {lastTrace?.tool_results?.length ? (
            lastTrace.tool_results.map((item, index) => (
              <div className="traceItem" key={`${item.tool_name}-${index}`}>
                <span>{item.tool_name}</span>
                <strong className={item.success ? 'ok' : 'bad'}>{item.success ? 'success' : item.error_code}</strong>
              </div>
            ))
          ) : (
            <p className="muted">发送问题后展示本轮工具调用</p>
          )}
        </section>

        <section className="panel">
          <h2><FileClock size={16} />Skill 命中</h2>
          {lastTrace?.retrieved_docs?.length ? lastTrace.retrieved_docs.map((doc) => (
            <div className="auditItem" key={doc.id}>
              <span>{doc.title}</span>
              <small>{doc.score}</small>
            </div>
          )) : <p className="muted">暂无 Skill 命中</p>}
        </section>

        <section className="panel">
          <h2><ShieldAlert size={16} />审批队列</h2>
          {approvals.length ? approvals.map((item) => (
            <div className="approval" key={item.id}>
              <span>{item.title}</span>
              <strong>{item.status}</strong>
            </div>
          )) : <p className="muted">暂无待审批变更</p>}
        </section>

        <section className="panel">
          <h2><FileClock size={16} />审计摘要</h2>
          <p className="muted">{audit.length} 条工具调用记录</p>
          <div className="auditList">
            {audit.slice(-6).reverse().map((item) => (
              <div className="auditItem" key={item.audit_id}>
                <span>{item.tool_name}</span>
                <small>{item.duration_ms}ms</small>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <h2><TerminalSquare size={16} />工具目录</h2>
          <div className="toolList">
            {tools.map((tool) => (
              <span key={tool.name}>{tool.name}</span>
            ))}
          </div>
        </section>
      </section>
    </main>
  );
}

function Stat({ icon, label, value, warn }) {
  return (
    <div className={`stat ${warn ? 'warn' : ''}`}>
      {React.cloneElement(icon, { size: 18 })}
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Gauge({ label, value }) {
  return (
    <div className="gauge">
      <div className="gaugeTop">
        <span>{label}</span>
        <strong>{metricPercent(value)}</strong>
      </div>
      <div className="track">
        <div style={{ width: metricPercent(value) }} />
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
