const assert=require("node:assert/strict");
const {run}=require("./frontend_dom.js")(process.argv[2]);
run('Date.now=()=>1000000;S.usage={members:{cc:{provider:"claude_code",mode:"subscription",usage:null,limits_available:true,limits:null}}}');
const html=()=>run('usageHTML({id:"cc",provider:"claude_code",terminal_id:"t"})');
const cases={
  partial(){
    run('S.usage.members.cc.limits={windows:[{window_minutes:300,used_percent:42,resets_in:900,stale:false}],updated_at:880}');
    const value=html();assert.match(value,/5 小时额度/);assert.match(value,/7 天额度/);
    assert.equal((value.match(/class="qrow"/g)||[]).length,2);
    assert.match(value,/width:42%/);assert.match(value,/暂无数据/);assert.match(value,/更新于 2 分钟前/);
    assert.doesNotMatch(value,/\/usage/);
  },
  missing(){
    const value=html();assert.equal((value.match(/暂无数据/g)||[]).length>=2,true);
    assert.match(value,/5 小时额度/);assert.match(value,/7 天额度/);assert.doesNotMatch(value,/width:NaN|\/usage/);
    run('S.usage.members.cc.mode="api";S.usage.members.cc.usage={cost:2,tokens:{in:1,out:1,cache_read:0,cache_write:0}}');
    assert.match(html(),/本群开销/);assert.match(html(),/暂无数据/);
  },
  reset(){
    run('S.usage.members.cc.limits={windows:[{window_minutes:300,used_percent:99,stale:true}],updated_at:940}');
    const value=html();assert.match(value,/已重置/);assert.doesNotMatch(value,/99%/);assert.match(value,/width:0%/);
  },
  async age_refresh(){
    run('S.group={id:"g",members:[]};S.tab="members";globalThis.renders=0;api=async()=>({members:{}});renderHead=()=>{};renderPane=()=>renders++');
    await run('loadUsage()');run('Date.now=()=>1060000');await run('loadUsage()');
    assert.equal(run('renders'),2);
  }
};
Promise.resolve().then(()=>cases[process.argv[3]]()).then(()=>process.stdout.write("OK\n"))
  .catch(error=>{console.error(error);process.exitCode=1;});
