"""Execute the shipped UI JavaScript with a small DOM stub, not a browser."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest


HTML = (Path(__file__).resolve().parents[2] / "web" / "static" / "index.html").read_text(encoding="utf-8")


def run_ui(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute UI JavaScript; no browser runner is configured")
    names = ["renderPipelineDetailTab", "capturePipelineTabData", "savePipelineFromDetail",
             "savePipeline", "formatPipelineError", "toggleChunkConfig", "showPipelineDetailError"]
    functions = "\n".join(
        re.search(rf"^        ((?:async )?function {name}\(.*?^        \}})", HTML, re.M | re.S)[1]
        for name in names
    )
    result = subprocess.run([node, "-"], input=functions + "\n" + script,
                            text=True, encoding="utf-8", capture_output=True, check=True, timeout=30)
    return json.loads(result.stdout)


def test_ui_create_chunk_payload_and_inline_rejection():
    result = run_ui(r"""
const values = {name:'test', source_id:'source', destination:'destination', docgrok_pipeline:'mdl-test',
 content_strategy:'chunk', processing_mode:'queue', chunk_size:'450', chunk_overlap:'0',
 chunk_unit:'tokens', chunk_text_field:'passage', chunk_doc_id_pattern:'custom-{source_hash}-{chunk}'};
const form = {querySelector: s => ({checked: !s.includes('store_content')}),
 querySelectorAll: () => []};
const elements = {'pipeline-form':form, 'pl-dest-vector-policy':{value:'embedding'}};
const document = {getElementById: id => elements[id]};
const FormData = class {get(key) {return values[key] ?? null;}};
const pipelineSourceMode='existing', pipelineDestMode='existing', pipelineTransformMode='pipeline';
const alerts=[], requests=[];
let step;
const alert = m => alerts.push(m), showPipelineStep = s => step=s;
const closeModal=()=>{}, refreshData=()=>{};
const apiFetch = async (url, options) => {requests.push(JSON.parse(options.body)); return {ok:true};};
(async()=>{
 await savePipeline();
 values.processing_mode='inline';
 await savePipeline();
 values.content_strategy='truncate';
 await savePipeline();
 console.log(JSON.stringify({requests,alerts,step}));
})().catch(e=>{console.error(e);process.exitCode=1});
""")
    chunk, inline = result["requests"]
    assert chunk["chunk_config"] == {
        "chunk_size": 450, "chunk_overlap": 0, "chunk_unit": "tokens", "store_text": True,
        "text_field": "passage", "doc_id_pattern": "custom-{source_hash}-{chunk}",
    }
    assert result["step"] == "options" and "Inline + chunk" in result["alerts"][0]
    assert inline["processing_mode"] == "inline" and "chunk_config" not in inline


def test_ui_detail_render_capture_and_save_preserve_contract():
    result = run_ui(r"""
const cc={chunk_size:450, chunk_overlap:0, chunk_unit:'tokens', store_text:true,
 text_field:'passage', doc_id_pattern:'custom-{source_hash}-{chunk}'};
const pipeline={id:'pip-test', name:'test', sources:[{source_id:'source'}],
 destination_id:'destination', content_strategy:'chunk', chunk_config:cc, processing_mode:'queue'};
const pipelines=[pipeline], sources=[{id:'source',type:'cosmosdb'}], destinations=[];
let currentPipelineTab='chunking', currentPipelineId='pip-test', editedPipelineData={}, pipelineDetailDirty=false;
const elements={'pipeline-detail-config':{innerHTML:''}};
const document={getElementById:id=>elements[id],querySelectorAll:()=>[]};
const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
renderPipelineDetailTab(pipeline);
const html=elements['pipeline-detail-config'].innerHTML;
elements['pip-detail-content-strategy']={value:'chunk'};
for (const [id,value] of Object.entries({'chunk-size':'450','chunk-overlap':'0','chunk-unit':'tokens',
 'chunk-text-field':'passage','chunk-doc-id-pattern':cc.doc_id_pattern})) {
 elements['pip-detail-'+id]={value};
}
elements['pip-detail-store-text']={checked:true};
capturePipelineTabData();
elements['pip-detail-chunk-size'].value='500';
capturePipelineTabData();
renderPipelineDetailTab(pipeline);
const rerender=elements['pipeline-detail-config'].innerHTML;
let request;
const apiFetch=async(url,opts)=>{request=JSON.parse(opts.body);return {ok:true};};
const closeModal=()=>{},refreshData=async()=>{};
(async()=>{await savePipelineFromDetail();console.log(JSON.stringify({html,rerender,request}));})()
 .catch(e=>{console.error(e);process.exitCode=1});
""")
    assert 'id="pip-detail-chunk-overlap" value="0"' in result["html"]
    assert 'value="tokens" selected' in result["html"]
    assert 'id="pip-detail-chunk-size" value="500"' in result["rerender"]
    for field in ("chunk-size", "chunk-overlap", "chunk-doc-id-pattern"):
        tag = re.search(rf'<input[^>]+id="pip-detail-{field}"[^>]*>', result["html"])[0]
        assert "disabled" not in tag and "markPipelineDetailDirty" in tag
    assert "mandatory pipeline/source namespace" in result["html"]
    assert 'id="pip-detail-doc-id-pattern"' not in result["html"]
    assert result["request"]["chunk_config"] == {
        "chunk_size": 500, "chunk_overlap": 0, "chunk_unit": "tokens", "store_text": True,
        "text_field": "passage", "doc_id_pattern": "custom-{source_hash}-{chunk}",
    }


def test_ui_validation_errors_are_readable():
    result = run_ui("""
console.log(JSON.stringify(formatPipelineError([{loc:['body','chunk_config','chunk_size'],msg:'Must be an integer'}])));
""")
    assert result == "body.chunk_config.chunk_size: Must be an integer"


def test_ui_chunk_section_is_conditional():
    result = run_ui("""
const section={style:{}};
const document={getElementById:()=>section};
let refreshed=0;
const updateSameContainerIndicator=()=>refreshed++;
toggleChunkConfig('chunk');
const chunk=section.style.display;
toggleChunkConfig('truncate');
console.log(JSON.stringify({chunk,truncate:section.style.display,refreshed}));
""")
    assert result == {"chunk": "block", "truncate": "none", "refreshed": 2}
    for field in ("chunk_size", "chunk_overlap", "chunk_unit", "store_text", "chunk_text_field",
                  "chunk_doc_id_pattern"):
        assert f'name="{field}"' in HTML


def test_ui_save_validation_error_keeps_editor_open():
    result = run_ui("""
let currentPipelineId='pip-test', editedPipelineData={}, pipelineDetailDirty=true, closed=false;
const pipelines=[{id:'pip-test',name:'test',sources:[],chunk_config:{chunk_size:99}}];
let error;
const panel={parentNode:{insertBefore:element=>error=element}};
const document={getElementById:id=>id==='pipeline-detail-config'?panel:null,
 createElement:()=>({style:{}})};
const apiFetch=async()=>({ok:false,json:async()=>({detail:[
 {loc:['body','chunk_config','chunk_size'],msg:'Invalid chunk size'}]})});
const closeModal=()=>closed=true;
(async()=>{await savePipelineFromDetail();console.log(JSON.stringify({
 text:error.textContent,display:error.style.display,closed,dirty:pipelineDetailDirty}));})()
 .catch(e=>{console.error(e);process.exitCode=1});
""")
    assert result == {"text": "body.chunk_config.chunk_size: Invalid chunk size",
                      "display": "block", "closed": False, "dirty": True}
