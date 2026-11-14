// Optional Node-only checks for the real inline UI.
const assert=require("node:assert/strict");
const {html,nodes,context,run,form,effortSelect}=require("./frontend_dom.js")(process.argv[2]);
const cases = {
  async delivery() {
    run(`S.group={id:"g",members:[{id:"dev",name:"开发",provider:"codex",role:"developer",
      status:"processing",pending:true,delivery_state:"uncertain",can_retry:true,
      delivery_id:"job",delivery_attempt:1}],messages:[]}; S.cur="g"`);
    assert.equal(run('statusOf(S.group.members[0]).st'),"delivery_uncertain");
    run("renderPane();renderComposer()");
    assert.match(nodes.get("#pane").innerHTML,/可能未送达/);
    assert.match(nodes.get("#pane").innerHTML,/data-retry="dev"/);
    assert.doesNotMatch(nodes.get("#typing").innerHTML,/正在思考/);
    run(`api=async(...args)=>{globalThis.retryCall=args;return {ok:true}};
      ask=()=>true;refresh=async()=>{}`);
    await run('retryDelivery("dev")');
    assert.equal(context.retryCall[0],"POST");
    assert.equal(context.retryCall[1],"/groups/g/members/dev/retry");
    assert.equal(context.retryCall[2].delivery_id,"job");
    assert.equal(context.retryCall[2].delivery_attempt,1);
    run('S.group.members[0].delivery_state="waiting_ready";S.group.members[0].pending=false');
    assert.equal(run('statusOf(S.group.members[0]).st'),"waiting_ready");
    run('S.group.members[0].delivery_state="not_ready";ask=text=>{globalThis.retryPrompt=text;return true}');
    await run('retryDelivery("dev")');
    assert.match(context.retryPrompt,/空闲/);
    assert.match(context.retryPrompt,/确认.*CLI.*可输入/);
  },
  choices() {
    for (const [provider, allowed] of [["codex", ["", "low", "medium", "high", "xhigh"]],
      ["claude_code", ["", "low", "medium", "high", "xhigh", "max"]]]) {
      const select = effortSelect(run(`memberFormHTML({provider:"${provider}"},0,false)`));
      assert.ok(select, `${provider} must expose reasoning effort`);
      assert.deepEqual(select.options.map(option => option.value), allowed);
      assert.equal(select.value, "");
      for (const value of allowed) {
        assert.equal(effortSelect(run(`memberFormHTML({provider:"${provider}",effort:"${value}"},0,false)`)).value, value);
      }
    }
    assert.equal(effortSelect(run('memberFormHTML({provider:"kiro_cli"},0,false)')), null);
  },
  switching() {
    context.form = form({provider: "claude_code", effort: "max"});
    run("wireMemberForm(form)");
    assert.equal(run("readMember(form).effort"), "max");
    context.form.fields.provider.value = "codex";
    context.form.fields.provider.onchange();
    assert.equal(run("readMember(form).effort"), "");
    context.form.querySelector("[name=effort]").value = "xhigh";
    context.form.fields.provider.value = "claude_code";
    context.form.fields.provider.onchange();
    assert.equal(run("readMember(form).effort"), "xhigh");
    context.form.fields.provider.value = "kiro_cli";
    context.form.fields.provider.onchange();
    assert.equal(context.form.querySelector("[name=effort]"), null);
    assert.equal(run("readMember(form).effort"), "");
    context.form.fields.provider.value = "codex";
    context.form.fields.provider.onchange();
    assert.equal(run("readMember(form).effort"), "");
  },
  async demo() {
    run("globalThis.demo = new Mock()");
    const group = await run('demo.call("POST","/api/groups",{name:"演示群",workdir:"~/projects/demo",members:[{name:"开发",provider:"codex",effort:"high",role:"developer"}]})');
    assert.equal(group.members[0].effort, "high");
    const member = await run(`demo.call("POST","/api/groups/${group.id}/members",{name:"评审",provider:"claude_code",effort:"max",role:"reviewer"})`);
    assert.equal(member.effort, "max");
    const edited = await run(`demo.call("PUT","/api/groups/${group.id}/members/${member.id}",{name:"评审",provider:"codex",effort:"xhigh",role:"reviewer"})`);
    assert.equal(edited.effort, "xhigh");
    assert.match(run(`demo.logs["${group.id}"]`), /推理强度=xhigh/);
    run(`S.group=demo.groups["${group.id}"];renderPane()`);
    assert.match(nodes.get("#pane").innerHTML, /推理强度：xhigh/);
    await run(`demo.call("PUT","/api/groups/${group.id}/members/${member.id}",{name:"评审",provider:"codex",effort:"",role:"reviewer"})`);
    run("renderPane()");
    assert.match(nodes.get("#pane").innerHTML, /推理强度：CLI 默认/);
    assert.match(run(`demo.logs["${group.id}"]`), /推理强度=CLI 默认/);
    for (const [provider, effort] of [["codex", "max"], ["claude_code", "ultra"], ["kiro_cli", "high"]]) {
      await assert.rejects(run(`demo.call("POST","/api/groups/${group.id}/members",{name:"非法",provider:"${provider}",effort:"${effort}"})`));
    }
    // Model a remote edit where polling sees the same terminal status and only effort changes.
    run(`S.cur="${group.id}"; S.msgs=demo.groups[S.cur].messages.slice();
      api=(method,path,body)=>demo.call(method,"/api"+path,body);
      renderHead=()=>{}; renderComposer=()=>{}`);
    await run("refresh()");
    run(`demo.groups[S.cur].members.find(m=>m.id==="${member.id}").effort="low"`);
    await run("refresh()");
    assert.match(nodes.get("#pane").innerHTML, /推理强度：low/);
  },
};
Promise.resolve().then(() => cases[process.argv[3]]()).then(() => {
  process.stdout.write("OK\n");
}).catch(error => { console.error(error); process.exitCode = 1; });
