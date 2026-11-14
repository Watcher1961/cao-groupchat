// Small behavior-only DOM facade. It does not measure CSS or render layout.
const fs = require("node:fs");
const vm = require("node:vm");
module.exports = function loadUI(path) {
  const html=fs.readFileSync(path,"utf8");
  const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
  const cutoff=script.indexOf("(async function boot(){");
  if(cutoff<0) throw Error("UI boot boundary missing");
  const nodes=new Map(), storage=new Map(), alerts=[];
  let document;
  function events(target) {
    const listeners=new Map();
    target.addEventListener=(type,fn)=>{ if(!listeners.has(type)) listeners.set(type,[]);listeners.get(type).push(fn); };
    target.dispatchEvent=e=>{ if(!e.target)e.target=target; target["on"+e.type]?.(e);for(const fn of listeners.get(e.type)||[])fn(e);return !e.defaultPrevented; };
    return target;
  }
  function element(tag="div") {
    const attributes=new Map(), classes=new Set(), scoped=new Map();
    const node=events({tagName:tag.toUpperCase(),innerHTML:"",textContent:"",value:"",style:{},dataset:{},
      children:[],parentElement:null,hidden:false,inert:false,disabled:false,isConnected:true,tabIndex:-1,
      scrollHeight:0,scrollTop:0,clientHeight:0,
      setAttribute(key,value){ attributes.set(key,String(value));if(key==="class")this.className=value;if(key==="id")this.id=value; },
      getAttribute(key){return attributes.get(key)??null;},removeAttribute(key){attributes.delete(key);},
      focus(){document.activeElement=this;},click(){if(!this.disabled)this.dispatchEvent({type:"click",target:this});},
      appendChild(child){child.parentElement=this;this.children.push(child);return child;},
      remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(c=>c!==this);this.isConnected=false;},
      contains(child){for(let current=child;current;current=current.parentElement)if(current===this)return true;return false;},
      getClientRects(){return this.hidden?[]:[{}];},
      querySelector(selector){if(!scoped.has(selector))this.appendChild(scoped.set(selector,element(/input/.test(selector)?"input":selector===".x"||selector.startsWith("[data-")?"button":"div")).get(selector));return scoped.get(selector);},
      querySelectorAll(selector){if(!selector.includes("[tabindex]"))return [];const all=[];const walk=n=>{for(const c of n.children){all.push(c);walk(c);}};walk(this);return all.filter(n=>["BUTTON","INPUT","SELECT","TEXTAREA","A"].includes(n.tagName)||n.tabIndex>=0);}
    });
    Object.defineProperty(node,"className",{get(){return [...classes].join(" ");},set(value){classes.clear();String(value).split(/\s+/).filter(Boolean).forEach(c=>classes.add(c));}});
    node.classList={contains:c=>classes.has(c),add:(...items)=>items.forEach(c=>classes.add(c)),remove:(...items)=>items.forEach(c=>classes.delete(c)),
      toggle(c,force){const on=force===undefined?!classes.has(c):force;on?classes.add(c):classes.delete(c);return on;}};
    return node;
  }
  function get(selector){if(!nodes.has(selector))nodes.set(selector,element());return nodes.get(selector);}
  for(const match of html.matchAll(/<(\w+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const node=element(match[1]);nodes.set("#"+match[3],node);
    for(const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g))node.setAttribute(attr[1],attr[2]);
    node.hidden=/\shidden(?:\s|$)/.test(match[2]);
  }
  for(const [id,tab] of [["#membersTab","members"],["#logTab","log"]]){get(id).tagName="BUTTON";get(id).dataset.tab=tab;}
  for(const [parent,children] of [["#groupsPanel",["#groupsClose","#newGroup","#themeBtn"]],
    ["#membersPanel",["#membersTab","#logTab","#membersClose"]],["#mainContent",["#groupsToggle","#membersToggle"]]])
    for(const child of children)get(parent).appendChild(get(child));
  document=events({body:element("body"),documentElement:element("html"),
    querySelector(selector){if(selector===".fsmask.on")return this.body.children.find(n=>n.classList.contains("fsmask")&&n.classList.contains("on"))||null;return get(selector);},
    querySelectorAll(selector){return selector===".seg button"?[get("#membersTab"),get("#logTab")]:[];},createElement:element});
  document.activeElement=document.body;
  const window=events({innerWidth:1440,viewportWidth:1440,confirm(){return true;}});
  window.matchMedia=query=>({matches:window.viewportWidth<Number(query.match(/width\s*<\s*(\d+)/)[1])});
  const context=vm.createContext({document,window,confirm:window.confirm,alert(text){alerts.push(text);},
    localStorage:{getItem:key=>storage.get(key)||"",setItem:(key,value)=>storage.set(key,value)},
    URLSearchParams,setTimeout(fn){fn();return 1;}});
  vm.runInContext(script.slice(0,cutoff),context);
  function run(code){return vm.runInContext(code,context);}
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
  return {html,nodes,context,run,element,form,effortSelect,get,alerts};
};
