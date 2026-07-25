import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  XCircle,
  Cpu,
  Database,
  FileClock,
  Send,
  LogIn,
  RefreshCw,
  ArrowLeft,
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

function QueryApp() {
  const [conversationId] = useState(() => {
    const existing = localStorage.getItem('clawsphere-conversation-id');
    const value = existing || crypto.randomUUID();
    localStorage.setItem('clawsphere-conversation-id', value);
    return value;
  });
  const [overview, setOverview] = useState(null);
  const [platformStatus, setPlatformStatus] = useState(null);
  const [llmStatus, setLlmStatus] = useState(null);
  const [tools, setTools] = useState([]);
  const [audit, setAudit] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [messages, setMessages] = useState([
    {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '我是 ClawSphere DCS 运维智能体（DCS Copilot）。我已接入运维平台，可以查询资源、解释告警、预测容量和诊断 VM 性能。',
    },
  ]);
  const [input, setInput] = useState('');
  const [lastTrace, setLastTrace] = useState(null);
  const [loading, setLoading] = useState(false);
  const listRef = useRef(null);

  const refreshSideData = async () => {
    const [overviewRes, platformRes, llmRes, toolsRes, auditRes, approvalRes] = await Promise.all([
      fetch(`${API_BASE}/api/overview`),
      fetch(`${API_BASE}/api/platform-status`),
      fetch(`${API_BASE}/api/llm-status`),
      fetch(`${API_BASE}/api/tools`),
      fetch(`${API_BASE}/api/audit`),
      fetch(`${API_BASE}/api/approvals`),
    ]);
    setOverview(await overviewRes.json());
    setPlatformStatus(await platformRes.json());
    setLlmStatus(await llmRes.json());
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
    setMessages((items) => [...items, { id: crypto.randomUUID(), role: 'user', content }]);
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: content,
          conversation_id: conversationId,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || `请求失败（HTTP ${res.status}）`);
      }
      setLastTrace(data);
      if (data.llm_status) setLlmStatus(data.llm_status);
      setMessages((items) => {
        const next = [...items, { id: crypto.randomUUID(), role: 'assistant', content: data.answer }];
        return next.slice(-12);
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

  const platformLabel = useMemo(() => {
    if (platformStatus?.fusioncompute === 'real' && platformStatus?.edme === 'real') return 'FC + eDME real';
    if (platformStatus?.fusioncompute === 'real') return 'FusionCompute real';
    if (platformStatus?.edme === 'real') return 'eDME real';
    return 'mock online';
  }, [platformStatus]);

  const llmState = lastTrace?.llm_status || llmStatus;
  const llmStateLabel = {
    healthy: 'LLM 正常',
    degraded: 'LLM 已降级',
    misconfigured: 'LLM 配置错误',
    not_configured: 'LLM 未配置',
    unknown: 'LLM 待检测',
  }[llmState?.status] || 'LLM 待检测';
  const responseSourceLabel = {
    deepseek: 'DeepSeek',
    deterministic: '确定性策略',
    pending: '等待审批',
    error: '处理失败',
  }[lastTrace?.response_source] || '未知';

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
          <div className="runtimeBadges">
            <span className="live"><CheckCircle2 size={14} /> {platformLabel}</span>
            <span className={`llmBadge ${llmState?.status || 'unknown'}`}>
              {llmStateLabel} · {llmState?.model || '-'}
            </span>
          </div>
        </div>

        <div className="exampleBar">
          {examples.map((item) => (
            <button key={item} type="button" onClick={() => sendMessage(item)}>{item}</button>
          ))}
        </div>

        <div className="messageList" ref={listRef}>
          {messages.map((message) => (
            <div className={`message ${message.role}`} key={message.id}>
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
          {lastTrace ? (
            <div className="runtimeTrace">
              <span>回答来源</span>
              <strong>{responseSourceLabel}</strong>
              <small>
                上下文 v{lastTrace.context?.schema_version || '-'}
                {' · '}历史 {lastTrace.context?.estimated_tokens ?? '-'} tokens
                {' · '}{lastTrace.context?.relevant_message_count ?? 0} 条相关历史
                {lastTrace.fallback_reason ? ` · ${lastTrace.fallback_reason}` : ''}
              </small>
            </div>
          ) : null}
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
          <p className="muted">高风险变更由独立审批台处理。</p>
          <a className="approvalLink" href="/approval"><ShieldAlert size={14} />进入审批台</a>
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

function ApprovalApp() {
  const [token, setToken] = useState(() => sessionStorage.getItem('clawsphere-admin-token') || '');
  const [approvals, setApprovals] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState('');

  const loadApprovals = async (accessToken = token) => {
    if (!accessToken) return;
    const response = await fetch(`${API_BASE}/api/approvals`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (!response.ok) throw new Error('审批身份已失效');
    const items = await response.json();
    setApprovals(items);
    setSelectedId((current) => current || items[0]?.id || null);
  };

  const login = async () => {
    setBusy(true);
    setResult('');
    try {
      const response = await fetch(`${API_BASE}/api/auth/demo-token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: 'demo-admin', roles: ['admin'], tenant_id: 'demo-tenant' }),
      });
      const data = await response.json();
      sessionStorage.setItem('clawsphere-admin-token', data.access_token);
      setToken(data.access_token);
      await loadApprovals(data.access_token);
    } catch (error) {
      setResult(error.message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (token) loadApprovals(token).catch((error) => setResult(error.message));
  }, [token]);

  const decide = async (approvalId, approved) => {
    setBusy(true);
    setResult('');
    try {
      const response = await fetch(`${API_BASE}/api/approvals/${approvalId}/decision`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ approved, reason: approved ? '演示管理员批准' : '演示管理员拒绝' }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || '审批失败');
      setResult(data.answer || '审批已处理');
      await loadApprovals();
    } catch (error) {
      setResult(error.message);
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <main className="approvalLogin">
        <div className="loginMark"><ShieldAlert size={28} /></div>
        <h1>ClawSphere 审批台</h1>
        <p>高风险变更需要管理员确认后才能从断点继续。</p>
        <button type="button" onClick={login} disabled={busy}><LogIn size={17} />管理员演示登录</button>
        <a href="/"><ArrowLeft size={14} />返回查询工作台</a>
        {result && <div className="decisionResult">{result}</div>}
      </main>
    );
  }

  const selected = approvals.find((item) => item.id === selectedId) || approvals[0];
  return (
    <main className="approvalShell">
      <aside className="approvalSidebar">
        <header className="approvalBrand">
          <div className="brandMark"><ShieldAlert size={21} /></div>
          <div><h1>变更审批</h1><p>admin · demo-tenant</p></div>
        </header>
        <a className="backLink" href="/"><ArrowLeft size={14} />查询工作台</a>
        <div className="queueTitle">
          <span>审批队列</span>
          <button type="button" title="刷新" onClick={() => loadApprovals()}><RefreshCw size={15} /></button>
        </div>
        <div className="approvalQueue">
          {approvals.map((item) => (
            <button
              type="button"
              className={item.id === selected?.id ? 'queueItem active' : 'queueItem'}
              key={item.id}
              onClick={() => setSelectedId(item.id)}
            >
              <span>{item.description}</span>
              <small>{item.id} · {item.status}</small>
            </button>
          ))}
          {!approvals.length && <p className="emptyQueue">暂无审批记录</p>}
        </div>
      </aside>

      <section className="approvalMain">
        <header className="approvalHeader">
          <div><h2>审批详情</h2><p>所有决定都会写入审计日志</p></div>
          <span className="adminBadge">管理员</span>
        </header>
        {selected ? (
          <div className="approvalDetail">
            <div className="detailTitle">
              <div><span className={`severity ${selected.risk}`}>{selected.risk}</span><h2>{selected.description}</h2></div>
              <strong className={`status ${selected.status}`}>{selected.status}</strong>
            </div>
            <dl className="detailGrid">
              <div><dt>审批编号</dt><dd>{selected.id}</dd></div>
              <div><dt>发起人</dt><dd>{selected.user_id}</dd></div>
              <div><dt>租户</dt><dd>{selected.tenant_id}</dd></div>
              <div><dt>创建时间</dt><dd>{new Date(selected.created_at).toLocaleString()}</dd></div>
            </dl>
            <section className="toolReview">
              <h3>待执行工具</h3>
              {selected.tool_calls.map((call, index) => (
                <div className="toolCall" key={`${call.tool_name}-${index}`}>
                  <strong>{call.tool_name}</strong>
                  <pre>{JSON.stringify(call.params, null, 2)}</pre>
                </div>
              ))}
            </section>
            {selected.status === 'pending' && (
              <div className="decisionBar">
                <button className="rejectButton" type="button" disabled={busy} onClick={() => decide(selected.id, false)}><XCircle size={17} />拒绝</button>
                <button className="approveButton" type="button" disabled={busy} onClick={() => decide(selected.id, true)}><CheckCircle2 size={17} />批准并继续</button>
              </div>
            )}
            {selected.status !== 'pending' && <p className="handledBy">处理人：{selected.approver || '-'} · {selected.decision_reason || '无备注'}</p>}
            {result && <div className="decisionResult">{result}</div>}
          </div>
        ) : <div className="noSelection"><ShieldAlert size={28} /><p>当前没有审批记录</p></div>}
      </section>
    </main>
  );
}

function Root() {
  return window.location.pathname.startsWith('/approval') ? <ApprovalApp /> : <QueryApp />;
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

createRoot(document.getElementById('root')).render(<Root />);
