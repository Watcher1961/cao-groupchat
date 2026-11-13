// Optional Node-only behavior checks for the actual inline UI, with no npm packages.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const html = fs.readFileSync(process.argv[2], "utf8");
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
const cutoff = script.indexOf("(async function boot(){");
assert.ok(cutoff > 0);
function element() {
  return {innerHTML: "", value: "", style: {}, dataset: {},
    classList: {toggle() {}, add() {}, remove() {}}, addEventListener() {},
    querySelectorAll() { return []; }};
}
const nodes = new Map();
const context = vm.createContext({
  document: {querySelector(selector) {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  }, querySelectorAll() { return []; }},
  localStorage: {getItem() { return ""; }, setItem() {}},
  URLSearchParams, setTimeout(fn) { fn(); return 1; },
});
vm.runInContext(script.slice(0, cutoff), context);
function run(code) { return vm.runInContext(code, context); }
function effortSelect(markup) {
  const match = markup.match(/<select\b[^>]*name="effort"[^>]*>([\s\S]*?)<\/select>/);
  if (!match) return null;
  const options = [...match[1].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(option => ({
    value: option[1].match(/value="([^"]*)"/)[1], selected: /\bselected\b/.test(option[1])}));
  return {options, value: (options.find(option => option.selected) || options[0]).value};
}
function form(member) {
  const fields = Object.fromEntries(Object.entries({name: "测试成员", provider: member.provider,
    role: "developer", duty: "测试职责", permission_mode: "", plan: "", model: ""})
    .map(([key, value]) => [key, {value}]));
  let markup = run(`memberFormHTML(${JSON.stringify(member)},0,false)`);
  let select = effortSelect(markup);
  const box = {get innerHTML() { return markup; }, set innerHTML(value) {
    markup = value; select = effortSelect(value);
  }};
  const modelBox = {innerHTML: "", querySelector(selector) {
    return selector === "[name=model]" ? fields.model : null;
  }};
  return {fields, querySelector(selector) {
    if (selector === ".effort") return box;
    if (selector === ".mdl") return modelBox;
    if (selector === ".cc") return {style: {}};
    if (selector === "[name=effort]") return select;
    const name = selector.match(/^\[name=(.+)\]$/);
    if (name) return fields[name[1]] || null;
    throw Error(`Unexpected selector ${selector}`);
  }, querySelectorAll() { return []; }};
}
const cases = {
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
