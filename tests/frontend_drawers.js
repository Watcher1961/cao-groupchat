// Tests actual handlers/state only; responsive geometry requires browser acceptance.
const assert=require("node:assert/strict");
const {html,nodes,context,run,element,form,get,alerts}=require("./frontend_dom.js")(process.argv[2]);
function width(value){context.window.innerWidth=Math.round(value);context.window.viewportWidth=value;context.window.dispatchEvent({type:"resize"});}
function key(value,shiftKey=false){const e={type:"keydown",key:value,shiftKey,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;}};context.document.dispatchEvent(e);return e;}
const cases={
  drawers(){
    assert.match(html,/<button[^>]*id="groupsToggle"[^>]*aria-controls="groupsPanel"/);
    assert.match(html,/<button[^>]*id="membersToggle"[^>]*aria-controls="membersPanel"/);
    assert.match(html,/<button[^>]*id="groupsClose"/);assert.match(html,/<button[^>]*id="membersClose"/);
    assert.match(html,/<aside[^>]*id="groupsPanel"/);assert.match(html,/<aside[^>]*id="membersPanel"/);
    assert.match(html,/id="drawerBackdrop"[^>]*hidden/);
    run("renderHead()");
    for(const value of [1440,1100,1099.8,1099,760,759.8,759,375]){
      width(value);
      assert.equal(get("#groupsToggle").hidden,value>=760);
      assert.equal(get("#membersToggle").hidden,value>=1100);
      get("#groupsToggle").click();assert.equal(run("S.drawer"),value<760?"groups":null);
      if(value<760){
        assert.equal(get("#groupsToggle").getAttribute("aria-expanded"),"true");
        assert.equal(get("#groupsPanel").getAttribute("role"),"dialog");
        assert.equal(get("#mainContent").inert,true);assert.equal(get("#membersPanel").inert,true);
        assert.equal(context.document.activeElement,get("#groupsClose"));
      }
      get("#membersToggle").click();assert.equal(run("S.drawer"),value<1100?"members":null);
      if(value<1100){
        assert.equal(get("#groupsPanel").classList.contains("is-drawer"),false);
        assert.equal(get("#membersToggle").getAttribute("aria-expanded"),"true");
        assert.equal(get("#membersPanel").getAttribute("aria-modal"),"true");
        assert.equal(get("#drawerBackdrop").hidden,false);
        get("#membersClose").click();assert.equal(context.document.activeElement,get("#membersToggle"));
      }
      assert.equal(run("S.drawer"),null);assert.equal(get("#drawerBackdrop").hidden,true);
      assert.equal(get("#mainContent").inert,false);
    }
  },
  drawer_media_change(){
    width(375);
    let resizes=0;context.window.addEventListener("resize",()=>resizes++);
    function mediaWidth(value,query){
      context.window.innerWidth=value;context.window.viewportWidth=value;
      const media=context.window.matchMedia(query);
      media.dispatchEvent({type:"change",media:query,matches:media.matches});
    }
    get("#membersToggle").click();
    mediaWidth(800,"(width < 760px)");
    assert.equal(run("S.drawer"),"members");
    assert.equal(get("#groupsToggle").hidden,true);
    assert.equal(get("#groupsPanel").inert,true);
    mediaWidth(1100,"(width < 1100px)");
    assert.equal(run("S.drawer"),null);
    for(const id of ["#mainContent","#groupsPanel","#membersPanel"])assert.equal(get(id).inert,false);
    assert.equal(get("#drawerBackdrop").hidden,true);
    assert.equal(get("#membersToggle").getAttribute("aria-expanded"),"false");
    assert.equal(get("#membersPanel").getAttribute("role"),null);
    assert.equal(context.document.activeElement,get("#membersPanel"));
    mediaWidth(375,"(width < 1100px)");mediaWidth(375,"(width < 760px)");
    get("#groupsToggle").click();
    mediaWidth(760,"(width < 760px)");
    assert.equal(run("S.drawer"),null);
    assert.equal(get("#drawerBackdrop").hidden,true);
    assert.equal(get("#mainContent").inert,false);assert.equal(get("#groupsPanel").inert,false);
    assert.equal(get("#membersPanel").inert,true); // Only the collapsed drawer stays inert.
    assert.equal(get("#groupsToggle").getAttribute("aria-expanded"),"false");
    assert.equal(get("#groupsPanel").getAttribute("role"),null);
    assert.equal(context.document.activeElement,get("#groupsPanel"));
    assert.equal(resizes,0);
  },
  async drawer_keyboard(){
    width(375);get("#membersToggle").click();assert.equal(run("S.drawer"),"members");
    const panel=get("#membersPanel"),first=element("button"),last=element("button"),hidden=element("button"),disabled=element("button");
    hidden.hidden=true;disabled.disabled=true;[first,hidden,disabled,last].forEach(n=>panel.appendChild(n));
    panel.querySelectorAll=()=>[first,hidden,disabled,last];
    last.focus();assert.equal(key("Tab").defaultPrevented,true);assert.equal(context.document.activeElement,first);
    first.focus();key("Tab",true);assert.equal(context.document.activeElement,last);
    key("Escape");assert.equal(run("S.drawer"),null);assert.equal(context.document.activeElement,get("#membersToggle"));
    get("#membersToggle").click();get("#drawerBackdrop").click();assert.equal(run("S.drawer"),null);
    get("#membersToggle").click();width(800);assert.equal(run("S.drawer"),"members");assert.equal(get("#groupsPanel").inert,true);
    width(1100);assert.equal(run("S.drawer"),null);assert.equal(get("#membersPanel").getAttribute("aria-hidden"),"false");assert.equal(get("#drawerBackdrop").hidden,true);
    width(375);get("#groupsToggle").click();width(760);assert.equal(run("S.drawer"),null);assert.equal(get("#groupsPanel").getAttribute("aria-hidden"),"false");
    width(375);get("#membersToggle").click();run('$("#mask").classList.add("on");syncDrawers();$("#modal").focus();mock=new Mock()');
    const selection=run('browseFolder("~/projects/demo")');
    assert.ok(context.document.querySelector(".fsmask.on"));key("Escape");assert.equal(await selection,null);
    assert.equal(get("#mask").classList.contains("on"),true);assert.equal(run("S.drawer"),"members");
    key("Escape");assert.equal(get("#mask").classList.contains("on"),false);assert.equal(run("S.drawer"),"members");assert.equal(panel.inert,false);
    key("Escape");assert.equal(run("S.drawer"),null);assert.equal(get("#mainContent").inert,false);
  },
  async drawer_group_select(){
    width(375);get("#groupsToggle").click();assert.equal(run("S.drawer"),"groups");
    run('globalThis.responses={};api=(method,path)=>new Promise(resolve=>responses[path]=resolve);loadUsage=()=>{}');
    const first=run('openGroup("first")'),second=run('openGroup("second")');
    run('responses["/groups/first"]({id:"first",name:"先选群",workdir:"~/projects/a",members:[],messages:[]})');await first;
    assert.equal(run("S.drawer"),"groups");
    run('responses["/groups/second"]({id:"second",name:"当前群",workdir:"~/projects/b",members:[],messages:[]})');await second;
    assert.equal(run("S.cur"),"second");assert.equal(run("S.group.id"),"second");assert.equal(run("S.drawer"),null);
    assert.equal(get("#drawerBackdrop").hidden,true);assert.equal(context.document.activeElement,get("#groupsToggle"));
  },
  async drawer_log_race(){
    run('S.group={id:"old",members:[]};S.tab="log";globalThis.logs={};api=(method,path)=>new Promise((resolve,reject)=>logs[path]={resolve,reject})');
    const previous=run('loadLog()');run('S.group={id:"current",members:[]}');const current=run('loadLog()');
    run('logs["/groups/current/log"].resolve({path:"current/GROUP_LOG.md",content:"当前群日志"})');await current;
    run('logs["/groups/old/log"].resolve({path:"old/GROUP_LOG.md",content:"过期群日志"})');await previous;
    assert.match(get("#pane").innerHTML,/当前群日志/);assert.doesNotMatch(get("#pane").innerHTML,/过期群日志/);
    const switchedTab=run('loadLog()');run('S.tab="members"');get("#pane").innerHTML="成员视图";
    run('logs["/groups/current/log"].resolve({path:"current/GROUP_LOG.md",content:"过期页签日志"})');await switchedTab;
    assert.equal(get("#pane").innerHTML,"成员视图");
    run('S.tab="log"');const failed=run('loadLog()');run('S.group={id:"another",members:[]}');get("#pane").innerHTML="新群内容";
    run('logs["/groups/current/log"].reject(new Error("expired log request"))');await failed;
    assert.equal(get("#pane").innerHTML,"新群内容");
  },
  async drawer_workflow(){
    width(375);run('mock=new Mock();const actualRefresh=refresh;refresh=()=>globalThis.lastRefresh=actualRefresh();const actualLog=loadLog;loadLog=()=>globalThis.lastLog=actualLog()');
    async function create(name){
      get("#groupsToggle").click();get("#newGroup").click();assert.equal(run("S.drawer"),null);assert.equal(get("#mask").classList.contains("on"),true);
      get("#gName").value=name;get("#gDir").value="~/projects/demo";await get("#mk").onclick();
      assert.equal(get("#mask").classList.contains("on"),false);return run("S.cur");
    }
    const first=await create("手机第一群"),second=await create("手机第二群");assert.notEqual(first,second);assert.equal(run("S.groups.length"),2);
    assert.equal([...get("#glist").innerHTML.matchAll(/<button\b/g)].length,2);
    assert.equal([...get("#glist").innerHTML.matchAll(/<\/button>/g)].length,2);
    get("#groupsToggle").click();await run(`openGroup(${JSON.stringify(first)})`);assert.equal(run("S.drawer"),null);
    get("#input").value="手机测试消息";await get("#send").onclick();assert.equal(get("#input").value,"");
    assert.equal(run('mock.groups[S.cur].messages.some(m=>m.kind==="owner"&&m.text==="手机测试消息")'),true);
    get("#membersToggle").click();assert.match(get("#pane").innerHTML,/开发/);
    const member=run('S.group.members.find(m=>m.name==="开发")'),edit=form(member);
    edit.fields.name.value=member.name;edit.fields.duty.value="手机更新职责";edit.fields.model.value=member.model||"";edit.fields.plan.value=member.plan||"";
    nodes.set("#modal .medit",edit);run('openMemberModal(S.group.members.find(m=>m.name==="开发"))');
    assert.equal(get("#mask").classList.contains("on"),true);assert.equal(get("#membersPanel").inert,true);
    await get("#ok").onclick();await run("lastRefresh");assert.equal(get("#mask").classList.contains("on"),false);assert.equal(get("#membersPanel").inert,false);
    assert.match(get("#pane").innerHTML,/手机更新职责/);
    get("#logTab").click();await run("lastLog");assert.equal(run("S.tab"),"log");assert.match(get("#pane").innerHTML,/手机测试消息|手机更新职责/);
    get("#membersClose").click();get("#membersToggle").click();assert.equal(run("S.tab"),"log");
    get("#groupsToggle").click();
    for(const [value,label] of [["light","浅色"],["dark","深色"],["","跟随系统"]]){
      get("#themeBtn").click();assert.equal(context.document.documentElement.dataset.theme||"",value);assert.match(get("#themeBtn").title,new RegExp(label));
    }
    assert.equal(alerts.length,0);
  }
};
Promise.resolve().then(()=>cases[process.argv[3]]()).then(()=>process.stdout.write("OK\n")).catch(error=>{console.error(error);process.exitCode=1;});
