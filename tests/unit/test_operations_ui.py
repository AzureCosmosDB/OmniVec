"""Execute operational UI calculations and parse the shipped scripts with Node."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
SCRIPT = ROOT / "web" / "static" / "operations.js"


def run_js(code):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser JavaScript contract tests")
    program = ("const ui = require(" + json.dumps(str(SCRIPT)) + ");\n(async () => {\n" + code
               + "\n})().catch(error => { console.error(error); process.exitCode = 1; });")
    result = subprocess.run([node, "-"], input=program, capture_output=True, text=True,
                            encoding="utf-8", check=False, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_all_inline_scripts_parse():
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", HTML, re.S)
    result = run_js("const vm = require('vm'); const scripts = " + json.dumps(scripts) +
                    "; scripts.forEach(source => new vm.Script(source)); console.log(JSON.stringify(scripts.length));")
    assert result >= 1


def test_unknown_and_invalid_measurements_are_not_zero():
    result = run_js("""
console.log(JSON.stringify({
 values:[null,undefined,-1,NaN,Infinity,'0',true,0,1.5].map(ui.number),
 missing:ui.display(null),zero:ui.display(0),empty:ui.total([]),partial:ui.total([1,null])
}));
""")
    assert result["values"] == [None] * 7 + [0, 1.5]
    assert result["missing"] == "Not observed" and result["zero"] == "0"
    assert result["empty"] is None and result["partial"] is None


def test_health_requires_fresh_observation():
    result = run_js("""
const now=Date.parse('2026-09-16T12:00:00Z');
console.log(JSON.stringify([
 ui.healthSignal({overall:'healthy'},now),
 ui.healthSignal({overall:'healthy',checked_at:'2026-09-16T11:00:00'},now),
 ui.healthSignal({overall:'healthy',checked_at:'2026-09-16T12:01:00Z'},now),
 ui.healthSignal({overall:'healthy',checked_at:'2026-09-16T11:59:00+00:00'},now)
]));
""")
    assert [entry["tone"] for entry in result] == ["warning", "warning", "warning", "good"]
    assert result[-1]["label"] == "Dependencies ready"


def test_diagnostic_metrics_preserve_unknown_counters_and_rollout_state():
    result = run_js("""
const data={pipelines:[{progress:{embedded_after:3}}],evidence:{
 queues:{queues:{blob:{ok:true,value:{active_message_count:0,dead_letter_message_count:2}}}},
 deployments:[{desired:2,ready:2,generation:3,observed_generation:2}]}};
const first=ui.diagnosticMetrics(data);
data.evidence.queues.queues.blob.ok=false;
console.log(JSON.stringify({first,failed:ui.diagnosticMetrics(data),empty:ui.diagnosticMetrics({})}));
""")
    assert result["first"] == {"activeMessages": 0, "deadLetters": 2, "workloads": 0,
                               "workloadCount": 1, "destinationCount": 3}
    assert result["failed"]["activeMessages"] is None
    assert all(value is None for value in result["empty"].values())


def test_time_series_does_not_invent_latency_or_activity():
    result = run_js("""
const bucket={t:'2026-09-16T11:00:00',processed:60,failed:0};
const missing=ui.summarizeBuckets([bucket],'minute');
bucket.avg_latency_ms=12;
const measured=ui.summarizeBuckets([bucket],'minute');
bucket.processed=0;
console.log(JSON.stringify({missing,measured,idle:ui.summarizeBuckets([bucket],'minute'),
 empty:ui.summarizeBuckets([],'minute')}));
""")
    assert result["missing"]["averageLatency"] is None
    assert result["measured"] == {"processed": 60, "failed": 0, "averageLatency": 12, "throughput": 1}
    assert result["idle"]["throughput"] == 0 and result["idle"]["averageLatency"] is None
    assert all(value is None for value in result["empty"].values())


def test_navigation_is_independent_of_global_click_event():
    body = re.search(r"^        function showSection\(section\) \{.*?^        \}", HTML, re.M | re.S)[0]
    assert "event.target" not in body
    assert "aria-current" in body
    assert "m.model_category === 'chat'" in HTML
    assert 'href="/static/operations.css"' in HTML and 'src="/static/operations.js"' in HTML
    assert "alias /usr/share/nginx/html/;" in (ROOT / "web" / "nginx.conf").read_text()


def test_diagnostics_never_render_untrusted_html_or_approve_actions():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "/chat/approve" not in source
    assert "controller.abort()" in source and "response.ok" in source


def test_old_agent_stream_cannot_render_into_new_session():
    function = re.search(r"^        async function _agentConsumeStream\(.*?^        \}", HTML, re.M | re.S)[0]
    result = run_js(function + """
let _agentUiVersion=2, cancelled=false, rendered=false;
const document={getElementById:()=>({})};
const _agentRenderTool=()=>rendered=true;
const reader={read:async()=>({done:false,value:new TextEncoder().encode('data: {"type":"tool_call"}\\n\\n')}),
 cancel:async()=>cancelled=true};
(async()=>{await _agentConsumeStream({body:{getReader:()=>reader}},{},1);
 console.log(JSON.stringify({cancelled,rendered}));})().catch(e=>{console.error(e);process.exitCode=1});
""")
    assert result == {"cancelled": True, "rendered": False}


@pytest.mark.parametrize("ending", ["done", "eof", "malformed"])
def test_agent_stream_reports_errors_only_for_incomplete_or_invalid_streams(ending):
    function = re.search(r"^        async function _agentConsumeStream\(.*?^        \}", HTML, re.M | re.S)[0]
    result = run_js(function + "\nconst ending = " + json.dumps(ending) + ";\n" + """
let _agentUiVersion=1, _agentSessionId='', cancelled=false;
const document={getElementById:()=>({})};
const agentRefreshSessions=async()=>{};
const bubble={textContent:''}, intermediate=[];
const chunks=['event: token\\ndata: {"text":"hel"}\\n\\n',
              'event: token\\ndata: {"text":"lo"}\\n\\n'];
if(ending==='done') chunks.push('event: final\\ndata: {"text":"hello"}\\n\\n',
                              'event: done\\ndata: {}\\n\\n');
if(ending==='malformed') chunks.push('event: token\\ndata: {\\n\\n');
const reader={
 read:async()=>{
  intermediate.push(bubble.textContent);
  return chunks.length ? {done:false,value:new TextEncoder().encode(chunks.shift())} : {done:true};
 },
 cancel:async()=>cancelled=true
};
await _agentConsumeStream({body:{getReader:()=>reader}},bubble,1);
console.log(JSON.stringify({text:bubble.textContent,intermediate,cancelled}));
""")
    assert all("[error]" not in text for text in result["intermediate"])
    if ending == "done":
        assert result["text"] == "hello"
    elif ending == "eof":
        assert result["text"].startswith("hello\n[error] The stream ended before completion.")
    else:
        assert "stream could not be processed" in result["text"]
    assert result["cancelled"] == (ending == "malformed")
