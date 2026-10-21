"""Check economic settings against an installed Pi without provider requests."""

import json
import shutil
import subprocess

import pytest

from omnigent.inner.pi_compaction import economic_settings


def test_installed_pi_threshold_and_summary_output_budgets(tmp_path):
    pi = shutil.which("pi")
    node = shutil.which("node")
    if not pi or not node:
        pytest.skip("Installed Pi and Node required for runtime contract test")
    settings, policy = economic_settings(
        {}, {"managed/vendor/large-model": 1050000}, '{"vendor/large-model":200000}'
    )
    source = tmp_path / "settings.json"
    source.write_text(json.dumps({"settings": settings, "policy": policy}))
    script = r"""
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const [binary,file]=process.argv.slice(1);
(async()=>{
 const mod=path.join(path.dirname(fs.realpathSync(binary)),'index.js');
 const runtime=await import(require('node:url').pathToFileURL(mod).href);
 const {settings,policy}=JSON.parse(fs.readFileSync(file));
 const model={id:'vendor/large-model',provider:'managed',contextWindow:1050000,maxTokens:128000,
              reasoning:false};
 const effective=runtime.SettingsManager.inMemory(settings).getCompactionSettings(model);
 assert.equal(runtime.shouldCompact(199999,model.contextWindow,effective),false);
 assert.equal(runtime.shouldCompact(200001,model.contextWindow,effective),true);
 assert.equal(model.contextWindow,1050000);
 let options;
 const usage={input:4,output:3,cacheRead:0,cacheWrite:0,totalTokens:7,cost:{input:0,output:0,
 cacheRead:0,cacheWrite:0,total:0}};
 const stream=async(_model,_context,opts)=>{options=opts;
 return {result:async()=>({role:'assistant',
 content:[{type:'text',text:'Summary'}],stopReason:'stop',usage,timestamp:Date.now()})}};
 const result=await runtime.generateSummaryWithUsage([],model,
   policy['managed/vendor/large-model'].summaryReserveTokens,'synthetic-key',undefined,
   undefined,undefined,undefined,'off',stream);
 assert.equal(options.maxTokens,Math.floor(0.8*16384));
 assert.equal(options.cacheRetention,'none');assert.equal(result.usage.totalTokens,7);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, pi, str(source)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
